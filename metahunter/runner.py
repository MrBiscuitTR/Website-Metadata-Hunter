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
        if self.closed:
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
