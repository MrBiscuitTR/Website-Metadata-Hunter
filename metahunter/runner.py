"""Runs a whole scan: discover -> download -> extract -> report.

Each phase is a plain function call in run_scan(), so the order is easy to follow.
Local inspection (no network) reuses the same per-file processing.
"""

import dataclasses
import datetime
import hashlib
import threading
from pathlib import Path
from urllib.parse import urlsplit

from .config import APP_FOLDER, load_config
from .control import Cancelled, ScanEvents, run_parallel
from .discovery.crawler import Crawler
from .discovery.subdomains import enumerate_subdomains, resolve_names
from .discovery.wayback import archived_files
from .extractors import extract_metadata
from .extractors.exiftool import ExifTool
from .net.http import DEFAULT_USER_AGENT, FetchError, Fetcher
from .net.urls import base_domain, extension_of, file_name_of, host_of, in_scope, is_same_or_subdomain, normalise_target
from .options import known_file_extension
from .output import OutputFolder, safe_filename, scan_folder_name
from .pdf.redaction import analyse_pdf
from .pdf.report import summarise, write_analysis
from .report.writers import write_reports

__all__ = ["ScanEvents", "run_scan", "inspect_local", "lookup_subdomains", "scans_folder"]

HTTP_HEADERS = {"last-modified": "Last-Modified", "server": "Server", "x-powered-by": "X-Powered-By",
                "content-type": "Content-Type", "content-disposition": "Content-Disposition",
                "etag": "ETag", "x-generator": "X-Generator", "date": "Server date"}


@dataclasses.dataclass
class FileResult:
    source: str                       # URL or local path
    found_on: list
    archived_from: str = ""
    status: str = "ok"
    size: int = 0
    sha256: str = ""
    saved_as: str = ""
    duplicate_of: str = ""
    metadata: object = None           # Metadata
    embedded_saved: list = dataclasses.field(default_factory=list)
    redaction: dict = None


@dataclasses.dataclass
class ScanResult:
    target: str
    folder: OutputFolder
    options: object
    started: datetime.datetime
    finished: datetime.datetime = None
    pages: list = dataclasses.field(default_factory=list)
    files: list = dataclasses.field(default_factory=list)
    subdomains: dict = dataclasses.field(default_factory=dict)   # name -> {"sources": [...], "resolves": bool}
    metadata_engine: str = ""
    stopped: bool = False
    aborted: bool = False
    report_files: dict = dataclasses.field(default_factory=dict)


def scans_folder(options):
    return Path(options.output_dir) if options.output_dir else APP_FOLDER / ".scans"


def load_exiftool(events):
    tool, message = ExifTool.from_setting(load_config()["exiftool_path"], APP_FOLDER)
    events.log(message)
    return tool, message


class FileProcessor:
    """Turns raw file bytes into a FileResult: metadata, saved copies, PDF analysis."""

    def __init__(self, options, folder, events, exiftool):
        self.options = options
        self.folder = folder
        self.events = events
        self.exiftool = exiftool
        self.closed = False             # set once the scan has moved on; late workers then write nothing
        self._hashes = {}
        self._lock = threading.Lock()

    def process(self, result, data, headers, name, host_label):
        result.size = len(data)
        result.sha256 = hashlib.sha256(data).hexdigest()
        with self._lock:
            if result.sha256 in self._hashes:
                result.duplicate_of = self._hashes[result.sha256]
                result.status = "duplicate"
                return result
            self._hashes[result.sha256] = result.source

        meta = extract_metadata(data, name, self.exiftool, self.options.leak_scan)
        if meta.format == "unknown" and headers.get("content-type", "").startswith("text/html"):
            result.status = "skipped: the server answered with a web page, not a file"
            return result
        for header, label in HTTP_HEADERS.items():
            if headers.get(header):
                meta.add("http", label, headers[header])
        result.metadata = meta

        analysis = None
        if self.options.redaction_check and meta.format == "pdf":
            try:
                analysis = analyse_pdf(data)
            except Exception as error:   # a hostile PDF must not end the scan
                meta.warnings.append(f"Redaction check failed: {type(error).__name__}: {error}")
        if self.closed or self.events.aborted():
            raise Cancelled()
        stem = safe_filename(name, "file")
        if self.options.save_files:
            saved = self.folder.write_bytes(f"files/{safe_filename(host_label, 'local')}/{stem}", data)
            result.saved_as = self.folder.relative(saved)
        if self.options.save_embedded:
            for label, extension, blob in meta.embedded:
                saved = self.folder.write_bytes(
                    f"embedded/{stem}__{safe_filename(label)}.{safe_filename(extension, 'bin')}", blob)
                result.embedded_saved.append(self.folder.relative(saved))
        if analysis is not None:
            result.redaction = (write_analysis(analysis, self.folder, result.source, stem)
                                if self.options.save_reports else summarise(analysis))
        return result


def _download_and_process(reference, fetcher, processor, options):
    result = FileResult(reference.url, sorted(reference.found_on), reference.archived_from)
    source = reference.archived_from or reference.url
    try:
        if reference.inline_data is not None:
            data, headers = reference.inline_data, {}
            name = f"inline_{reference.url.split(':')[1]}.{reference.url.rsplit('/', 1)[-1]}"
        elif reference.prefetched is not None:
            data, headers = reference.prefetched.body, reference.prefetched.headers
            name = file_name_of(source)
        else:
            response = fetcher.get(reference.url, int(options.max_file_mb * 1024 * 1024))
            data, headers, name = response.body, response.headers, file_name_of(source)
    except FetchError as error:
        result.status = f"error: {error}"
        return result
    return processor.process(result, data, headers, name, host_of(source) or "inline")


def _process_files(jobs, worker, processor, events, threads):
    def report(result, done, total):
        events.progress(done, total, result.source)
        if result.status not in ("ok", "duplicate"):
            events.log(f"  {result.source}: {result.status}")

    try:
        return run_parallel(jobs, worker, threads, events, on_result=report)
    finally:
        processor.closed = True


def _finish(result, events):
    result.stopped = events.stop_requested()
    result.aborted = events.aborted()
    result.finished = datetime.datetime.now()
    result.pages = list(result.pages)            # may still be a live view if the crawl was cut short
    if result.stopped and not result.aborted:
        events.log("Stopped early; writing what was collected so far.")
    if result.options.save_reports and not result.aborted:
        result.report_files = write_reports(result, events)
    return result


def run_scan(options, events):
    options = dataclasses.replace(options)     # the scan may widen the scope; keep the caller's copy intact
    started = datetime.datetime.now()
    target = normalise_target(options.target)
    start_host = host_of(target)
    domain = base_domain(start_host)
    fetcher = Fetcher(options.user_agent or DEFAULT_USER_AGENT, options.timeout, options.delay,
                      options.verify_tls, options.allow_private, control=events)
    fetcher.check_allowed(target)        # fail early on private addresses or DNS problems

    folder = OutputFolder(scans_folder(options), scan_folder_name(target, started))
    result = ScanResult(target, folder, options, started)
    events.log(f"Scan folder: {folder.root}")
    exiftool, result.metadata_engine = load_exiftool(events)
    try:
        _scan_phases(result, options, target, start_host, domain, fetcher, folder, exiftool, events)
    except Cancelled:
        pass
    return _finish(result, events)


def _scan_phases(result, options, target, start_host, domain, fetcher, folder, exiftool, events):
    # 1. Subdomains from third-party sources (opt-in)
    third_party = [source for source in options.subdomain_sources if source != "links"]
    found = enumerate_subdomains(domain, third_party, fetcher, events) if third_party else {}

    # 2. Crawl
    crawler = Crawler(options, fetcher, events)
    result.pages = crawler.pages.values()
    seeds = [target]
    if options.use_sitemap:
        parts = urlsplit(target)
        for url in crawler.sitemap_urls(f"{parts.scheme}://{parts.netloc}"):
            if not in_scope(url, start_host, options.scope):
                continue
            if not known_file_extension(extension_of(url)):
                seeds.append(url)
            elif crawler.file_category(url, None):
                crawler.add_file(url, crawler.file_category(url, None), "(sitemap)")
        events.log(f"Sitemaps added {len(seeds) - 1} page(s).")
    if options.crawl_subdomains and found:
        live = [name for name, alive in resolve_names(found, events).items() if alive]
        seeds += [f"https://{name}/" for name in live]
        if options.scope == "host":
            options.scope = "domain"
            events.log("Crawling subdomains: scope widened to this domain and its subdomains.")
    crawler.crawl(seeds, start_host)
    result.pages = list(crawler.pages.values())
    events.log(f"Visited {len(result.pages)} page(s), found {len(crawler.files)} file(s).")

    # 3. Subdomains seen in links, and which names resolve
    if "links" in options.subdomain_sources:
        for host in crawler.hosts_seen:
            if host != domain and is_same_or_subdomain(host, domain):
                found.setdefault(host, set()).add("links")
    if found:
        alive = resolve_names(found, events)
        result.subdomains = {name: {"sources": sorted(sources), "resolves": alive.get(name)}
                             for name, sources in sorted(found.items())}

    # 4. Archived copies from the Wayback Machine (opt-in)
    if options.wayback_files and not events.stop_requested():
        for archive_url, original, category in archived_files(domain, options.scope != "host", options.file_types,
                                                              fetcher, events, limit=options.max_files):
            crawler.add_file(archive_url, category, "web.archive.org", archived_from=original)

    # 5. Download and read every file
    processor = FileProcessor(options, folder, events, exiftool)
    references = list(crawler.files.values())[:options.max_files]
    events.log(f"Downloading and reading {len(references)} file(s)...")
    result.files = _process_files(references, lambda ref: _download_and_process(ref, fetcher, processor, options),
                                  processor, events, options.threads)


def _local_files(paths, limit, extensions=None):
    """Expand files and folders (recursively) into a list of files, up to `limit`.

    Folders are filtered by `extensions` when given; files named explicitly are always kept.
    """
    found = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            candidates = sorted(p for p in path.rglob("*") if p.is_file()
                                and (not extensions or p.suffix.lower().lstrip(".") in extensions))
        else:
            candidates = [path]
        for candidate in candidates:
            if candidate.is_file() and len(found) < limit:
                found.append(candidate.resolve())
    return found


def inspect_local(paths, options, events, extensions=None):
    """Same per-file processing as a scan, for files already on disk. No network at all."""
    started = datetime.datetime.now()
    files = _local_files(paths, options.max_files, extensions)
    if not files:
        raise FileNotFoundError("no files found at: " + ", ".join(map(str, paths)))
    label = f"local_{files[0].stem}" if len(files) == 1 else f"local_{Path(paths[0]).name or 'files'}"
    folder = OutputFolder(scans_folder(options), scan_folder_name(label, started))
    result = ScanResult("local files: " + ", ".join(map(str, paths)), folder, options, started)
    events.log(f"Scan folder: {folder.root}")
    exiftool, result.metadata_engine = load_exiftool(events)
    processor = FileProcessor(options, folder, events, exiftool)
    limit = int(options.max_file_mb * 1024 * 1024)

    def read_one(path):
        file_result = FileResult(str(path), [])
        if path.stat().st_size > limit:
            file_result.status = f"skipped: larger than {options.max_file_mb:g} MB"
            return file_result
        return processor.process(file_result, path.read_bytes(), {}, path.name, "local")

    events.log(f"Reading {len(files)} local file(s)...")
    result.files = _process_files(files, read_one, processor, events, options.threads)
    return _finish(result, events)


def lookup_subdomains(domain, sources, options, events):
    """Only enumerate subdomains: {name: {"sources": [...], "resolves": bool}}."""
    fetcher = Fetcher(options.user_agent or DEFAULT_USER_AGENT, options.timeout, 0.0, options.verify_tls,
                      control=events)
    domain = base_domain(host_of(normalise_target(domain)))
    try:
        found = enumerate_subdomains(domain, sources, fetcher, events)
    except Cancelled:
        return {}
    alive = resolve_names(found, events) if found else {}
    return {name: {"sources": sorted(found[name]), "resolves": alive.get(name)} for name in sorted(found)}
