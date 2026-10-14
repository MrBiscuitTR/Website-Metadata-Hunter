"""Breadth-first crawl: fetch pages level by level, collect links to files.

Depth 0 reads only the start page(s), depth 1 also the pages they link to, and so on.
Files embedded or linked by visited pages are collected for download, never executed.
"""

import dataclasses
import re
import threading
from urllib.robotparser import RobotFileParser

from ..control import run_parallel
from ..net.http import FetchError
from ..net.urls import clean_url, extension_of, host_of, in_scope
from ..options import NOT_PAGES, category_for_extension, category_for_mime, known_file_extension
from .html_links import css_urls, parse_page

MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_STYLESHEET_BYTES = 2 * 1024 * 1024
MAX_SITEMAPS = 20
HTML_TYPES = ("text/html", "application/xhtml+xml")
SITEMAP_LOC = re.compile(rb"<loc>\s*([^<\s]+)\s*</loc>")


@dataclasses.dataclass
class FileRef:
    url: str
    category: str = None          # None: decide after download
    found_on: set = dataclasses.field(default_factory=set)
    prefetched: object = None     # a Response we already have (page link that turned out to be a file)
    inline_data: bytes = None     # bytes from a data: URI
    archived_from: str = ""       # original URL when this is a Wayback Machine copy


@dataclasses.dataclass
class PageRecord:
    url: str
    depth: int
    status: str = ""
    title: str = ""
    generator: str = ""
    author: str = ""
    emails: list = dataclasses.field(default_factory=list)
    server: str = ""


class Crawler:
    def __init__(self, options, fetcher, events):
        self.options = options
        self.fetcher = fetcher
        self.events = events
        self.pages = {}
        self.files = {}
        self.hosts_seen = set()
        self._lock = threading.Lock()
        self._robots = {}
        self._stylesheets_seen = set()
        self._inline_count = 0

    # ------------------------------------------------------------ bookkeeping

    def add_file(self, url, category, found_on, **extra):
        with self._lock:
            if url in self.files:
                self.files[url].found_on.add(found_on)
            elif len(self.files) < self.options.max_files:
                self.files[url] = FileRef(url, category, {found_on}, **extra)

    def file_category(self, url, hint):
        """The wanted category for a URL, or None if the user did not ask for this kind of file."""
        extension = extension_of(url)
        if known_file_extension(extension):
            return category_for_extension(extension, self.options.file_types)
        return hint if hint in self.options.file_types else None

    def _allowed_by_robots(self, url):
        if not self.options.respect_robots:
            return True
        origin = re.match(r"^https?://[^/]+", url).group(0)
        if origin not in self._robots:
            parser = RobotFileParser()
            try:
                response = self.fetcher.get(origin + "/robots.txt", 512 * 1024)
                parser.parse(response.body.decode("utf-8", "replace").splitlines())
            except FetchError:
                parser.parse([])      # no robots.txt: everything allowed
            self._robots[origin] = parser
        return self._robots[origin].can_fetch(self.fetcher.user_agent, url)

    # ------------------------------------------------------------ crawling

    def crawl(self, seeds, start_host):
        """Visit pages breadth-first from `seeds`. Returns nothing; results live on the crawler."""
        level = [(url, 0) for url in dict.fromkeys(seeds)]
        queued = set(url for url, _ in level)
        while level and not self.events.stop_requested():
            room = self.options.max_pages - len(self.pages)
            if room <= 0:
                self.events.log(f"Reached the page limit ({self.options.max_pages}).")
                break
            level = level[:room]
            depth = level[0][1]
            self.events.log(f"Depth {depth}: visiting {len(level)} page(s)...")
            results = run_parallel(level, lambda item: self._visit(*item, start_host), self.options.threads, self.events)
            next_level = []
            for links in results:
                for link in links:
                    if depth + 1 <= self.options.depth and link not in queued:
                        queued.add(link)
                        next_level.append((link, depth + 1))
            level = next_level

    def _visit(self, url, depth, start_host):
        """Fetch one page; return the in-scope links that could be pages."""
        if self.events.stop_requested():
            return []
        record = PageRecord(url, depth)
        with self._lock:
            self.pages[url] = record
        if not self._allowed_by_robots(url):
            record.status = "skipped (robots.txt)"
            return []
        try:
            response = self.fetcher.get(url, MAX_PAGE_BYTES, accept="text/html,application/xhtml+xml,*/*;q=0.5")
        except FetchError as error:
            record.status = f"error: {error}"
            self.events.log(f"  could not fetch {url}: {error}")
            return []
        record.status = str(response.status)
        record.server = response.headers.get("server", "")
        if response.content_type not in HTML_TYPES:
            category = category_for_mime(response.content_type, self.options.file_types) or self.file_category(url, None)
            if category:
                self.add_file(url, category, "(linked directly)", prefetched=response)
            return []

        charset = re.search(r"charset=([\w-]+)", response.headers.get("content-type", ""))
        encoding = charset.group(1) if charset and _codec_exists(charset.group(1)) else "utf-8"
        found = parse_page(response.body.decode(encoding, "replace"), response.url)

        record.title, record.generator, record.author = found.title, found.generator, found.author
        record.emails = sorted(found.emails)

        for asset_url, hint in found.assets:
            self._consider_file(asset_url, hint, url, start_host)
        for mime, data in found.inline_files:
            category = category_for_mime(mime, self.options.file_types)
            if category:
                with self._lock:
                    self._inline_count += 1
                    number = self._inline_count
                self.add_file(f"inline:{number}:{mime}", category, url, inline_data=data)
        if self.options.follow_stylesheets and "images" in self.options.file_types:
            for stylesheet in found.stylesheets:
                self._read_stylesheet(stylesheet, url, start_host)

        page_links = []
        for raw_link in found.links:
            link = clean_url(raw_link)
            if not link:
                continue
            with self._lock:
                self.hosts_seen.add(host_of(link))
            extension = extension_of(link)
            if known_file_extension(extension):
                self._consider_file(link, None, url, start_host)
            elif extension not in NOT_PAGES and in_scope(link, start_host, self.options.scope):
                page_links.append(link)
        return page_links

    def _consider_file(self, raw_url, hint, page_url, start_host):
        url = clean_url(raw_url)
        if not url:
            return
        with self._lock:
            self.hosts_seen.add(host_of(url))
        if not (self.options.external_files or in_scope(url, start_host, self.options.scope)):
            return
        category = self.file_category(url, hint)
        if category:
            self.add_file(url, category, page_url)

    def _read_stylesheet(self, raw_url, page_url, start_host):
        url = clean_url(raw_url)
        with self._lock:
            if not url or url in self._stylesheets_seen:
                return
            self._stylesheets_seen.add(url)
        if not (self.options.external_files or in_scope(url, start_host, self.options.scope)):
            return
        try:
            response = self.fetcher.get(url, MAX_STYLESHEET_BYTES, accept="text/css,*/*;q=0.1")
        except FetchError:
            return
        for asset in css_urls(response.body.decode("utf-8", "replace")):
            if not asset.startswith("data:"):
                self._consider_file(clean_url(asset, base=response.url) or "", "images", page_url, start_host)

    # ------------------------------------------------------------ sitemaps

    def sitemap_urls(self, origin):
        """Page URLs listed in robots.txt sitemaps and /sitemap.xml (one level of sitemap indexes)."""
        to_read, seen, pages = [origin + "/sitemap.xml"], set(), []
        try:
            robots = self.fetcher.get(origin + "/robots.txt", 512 * 1024).body.decode("utf-8", "replace")
            to_read += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots)
        except FetchError:
            pass
        while to_read and len(seen) < MAX_SITEMAPS:
            sitemap = to_read.pop(0)
            if sitemap in seen:
                continue
            seen.add(sitemap)
            try:
                body = self.fetcher.get(sitemap, 10 * 1024 * 1024).body
            except FetchError:
                continue
            for location in SITEMAP_LOC.findall(body):
                url = clean_url(location.decode("utf-8", "replace").replace("&amp;", "&"))
                if url and extension_of(url) == "xml":
                    to_read.append(url)
                elif url:
                    pages.append(url)
        return pages


def _codec_exists(name):
    try:
        "".encode(name)
        return True
    except LookupError:
        return False
