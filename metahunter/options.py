"""Every choice a scan can make, in one place. The CLI, wizard and GUI all fill in ScanOptions."""

import dataclasses

# File categories the user can pick, and the extensions that belong to them.
FILE_TYPES = {
    "images": ("Images", ("jpg", "jpeg", "jpe", "jfif", "png", "apng", "gif", "webp", "bmp", "ico", "cur", "tif",
                          "tiff", "heic", "heif", "avif", "svg", "dng", "cr2", "nef", "arw")),
    "pdf": ("PDF documents", ("pdf",)),
    "documents": ("Office documents", ("docx", "docm", "xlsx", "xlsm", "pptx", "pptm", "odt", "ods", "odp",
                                       "doc", "xls", "ppt", "rtf")),
    "audio": ("Audio", ("mp3", "wav", "flac", "ogg", "oga", "opus", "m4a")),
    "video": ("Video", ("mp4", "m4v", "mov", "3gp", "avi", "webm", "mkv", "ogv")),
}
# MIME type prefixes used when a link has no telling extension.
MIME_CATEGORIES = {
    "image/": "images", "application/pdf": "pdf", "audio/": "audio", "video/": "video",
    "application/msword": "documents", "application/vnd.ms-": "documents",
    "application/vnd.openxmlformats": "documents", "application/vnd.oasis.opendocument": "documents",
}
# Extensions we never treat as pages to crawl.
NOT_PAGES = {"css", "js", "json", "xml", "txt", "zip", "gz", "tgz", "rar", "7z", "exe", "msi", "dmg", "apk",
             "iso", "bin", "woff", "woff2", "ttf", "otf", "eot", "csv", "rss", "atom", "map", "wasm"}

INFO_CATEGORIES = {
    "location": "GPS coordinates and place names",
    "time": "Created, modified and taken timestamps",
    "device": "Camera / phone make, model, serial numbers, lens, printer",
    "camera": "Exposure, aperture, ISO, focal length, flash",
    "people": "Authors, editors, reviewers, owners, companies, emails",
    "software": "Editing software, producers, encoders, AI prompts",
    "document": "Title, subject, keywords, page counts, revisions",
    "technical": "Dimensions, codecs, durations, colour profiles, hashes",
    "hidden": "Comments, thumbnails, leaked paths, macros, scripts, attachments",
    "http": "Server headers: Last-Modified, Server, Content-Disposition...",
}

SUBDOMAIN_SOURCES = {
    "links": "hosts seen in links while crawling (no third party involved)",
    "crtsh": "crt.sh certificate transparency logs",
    "certspotter": "Cert Spotter certificate transparency API",
    "hackertarget": "HackerTarget host search (rate limited)",
    "alienvault": "AlienVault OTX passive DNS",
    "wayback": "Wayback Machine archived URLs",
}
THIRD_PARTY_SOURCES = [name for name in SUBDOMAIN_SOURCES if name != "links"]


@dataclasses.dataclass
class ScanOptions:
    target: str = ""

    # How far to go
    depth: int = 0                    # 0 = only the given page, 1 = plus pages it links to, ...
    scope: str = "host"               # host / domain / any
    max_pages: int = 100
    external_files: bool = True       # download files hosted elsewhere (CDNs) that in-scope pages embed
    use_sitemap: bool = False
    follow_stylesheets: bool = True   # read linked CSS for background images
    respect_robots: bool = True
    wayback_files: bool = False       # also fetch archived copies of files from the Wayback Machine

    # Subdomains
    subdomain_sources: list = dataclasses.field(default_factory=list)
    crawl_subdomains: bool = False    # crawl live subdomains too, instead of only listing them

    # What to collect and report
    file_types: list = dataclasses.field(default_factory=lambda: list(FILE_TYPES))
    info_categories: list = dataclasses.field(default_factory=lambda: list(INFO_CATEGORIES))
    max_files: int = 500
    max_file_mb: float = 50.0
    redaction_check: bool = True
    save_reports: bool = True         # report.html / report.json / summary.txt and the PDF analysis folders
    save_files: bool = True

    save_embedded: bool = True        # thumbnails, cover art, attachments found inside files
    leak_scan: bool = True

    # Network behaviour
    threads: int = 4
    delay: float = 0.3
    timeout: float = 20.0
    user_agent: str = ""
    verify_tls: bool = True
    allow_private: bool = False

    output_dir: str = ""

    def problems(self):
        """Human-readable reasons these options cannot be used, or an empty list."""
        issues = []
        if self.depth < 0:
            issues.append("depth cannot be negative")
        if self.scope not in ("host", "domain", "any"):
            issues.append(f"unknown scope {self.scope!r}")
        for name in self.file_types:
            if name not in FILE_TYPES:
                issues.append(f"unknown file type {name!r} (choose from {', '.join(FILE_TYPES)})")
        for name in self.info_categories:
            if name not in INFO_CATEGORIES:
                issues.append(f"unknown information category {name!r} (choose from {', '.join(INFO_CATEGORIES)})")
        for name in self.subdomain_sources:
            if name not in SUBDOMAIN_SOURCES:
                issues.append(f"unknown subdomain source {name!r} (choose from {', '.join(SUBDOMAIN_SOURCES)})")
        if not self.file_types:
            issues.append("pick at least one file type")
        if self.max_pages < 1 or self.max_files < 1 or self.max_file_mb <= 0:
            issues.append("limits must be positive")
        if not 1 <= self.threads <= 32:
            issues.append("threads must be between 1 and 32")
        return issues


def category_for_extension(extension, wanted):
    for name in wanted:
        if extension in FILE_TYPES[name][1]:
            return name
    return None


def known_file_extension(extension):
    return any(extension in extensions for _, extensions in FILE_TYPES.values())


def category_for_mime(mime, wanted):
    for prefix, name in MIME_CATEGORIES.items():
        if mime.startswith(prefix) and name in wanted:
            return name
    return None
