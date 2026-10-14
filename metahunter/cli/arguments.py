"""Command-line arguments, and turning them into ScanOptions (and back, for the wizard)."""

import argparse

from ..options import FILE_TYPES, INFO_CATEGORIES, SUBDOMAIN_SOURCES, ScanOptions
from ..net.urls import SCOPES


def name_list(choices):
    """argparse type for comma-separated names; also accepts "all" and "none"."""
    def parse(text):
        names = [part.strip().lower() for part in text.split(",") if part.strip()]
        if names == ["all"]:
            return list(choices)
        if names == ["none"]:
            return []
        unknown = [name for name in names if name not in choices]
        if unknown:
            raise argparse.ArgumentTypeError(f"unknown: {', '.join(unknown)} (choose from {', '.join(choices)})")
        return names
    return parse


def add_report_arguments(parser, defaults):
    group = parser.add_argument_group("what to collect and report")
    group.add_argument("--info", type=name_list(INFO_CATEGORIES), default=list(INFO_CATEGORIES), metavar="LIST",
                       help=f"information to include: {','.join(INFO_CATEGORIES)}, or all (default)")
    group.add_argument("--max-files", type=int, default=defaults.max_files, metavar="N", help="stop after N files (default %(default)s)")
    group.add_argument("--max-size", type=float, default=defaults.max_file_mb, metavar="MB", dest="max_file_mb",
                       help="skip files larger than this (default %(default)s MB)")
    group.add_argument("--no-redaction-check", dest="redaction_check", action="store_false",
                       help="skip the PDF redaction / hidden-text check")
    group.add_argument("--no-embedded", dest="save_embedded", action="store_false",
                       help="do not save thumbnails, cover art and attachments found inside files")
    group.add_argument("--no-leak-scan", dest="leak_scan", action="store_false",
                       help="do not search file bytes for user folders, network shares and emails")
    group.add_argument("--threads", type=int, default=defaults.threads, metavar="N", help="parallel workers (default %(default)s)")
    group.add_argument("--output", dest="output_dir", default="", metavar="DIR", help="where scan folders go (default .scans)")


def add_scan_arguments(parser):
    defaults = ScanOptions()
    parser.add_argument("target", help="domain, subdomain or page URL, e.g. example.com or https://example.com/about")
    crawl = parser.add_argument_group("how far to go")
    crawl.add_argument("-d", "--depth", type=int, default=defaults.depth, metavar="N",
                       help="link hops from the start page; 0 = only that page (default 0)")
    crawl.add_argument("--scope", choices=list(SCOPES), default=defaults.scope,
                       help="; ".join(f"{name}: {text}" for name, text in SCOPES.items()) + " (default host)")
    crawl.add_argument("--max-pages", type=int, default=defaults.max_pages, metavar="N", help="page limit (default %(default)s)")
    crawl.add_argument("--no-external-files", dest="external_files", action="store_false",
                       help="ignore files hosted on other sites (CDNs) even if in-scope pages embed them")
    crawl.add_argument("--sitemap", dest="use_sitemap", action="store_true", help="also visit pages listed in sitemaps")
    crawl.add_argument("--no-css", dest="follow_stylesheets", action="store_false", help="do not read stylesheets for images")
    crawl.add_argument("--ignore-robots", dest="respect_robots", action="store_false", help="do not obey robots.txt")
    crawl.add_argument("--wayback-files", action="store_true", help="also download archived copies from the Wayback Machine")

    subdomains = parser.add_argument_group("subdomains")
    subdomains.add_argument("--subdomains", dest="subdomain_sources", type=name_list(SUBDOMAIN_SOURCES), default=[],
                            metavar="LIST", help=f"sources: {','.join(SUBDOMAIN_SOURCES)}, or all (default none)")
    subdomains.add_argument("--crawl-subdomains", action="store_true",
                            help="crawl subdomains that resolve, instead of only listing them")

    files = parser.add_argument_group("files")
    files.add_argument("--types", dest="file_types", type=name_list(FILE_TYPES), default=list(FILE_TYPES), metavar="LIST",
                       help=f"file types: {','.join(FILE_TYPES)}, or all (default)")
    files.add_argument("--no-save-files", dest="save_files", action="store_false", help="read files in memory only")
    add_report_arguments(parser, defaults)

    network = parser.add_argument_group("network")
    network.add_argument("--delay", type=float, default=defaults.delay, metavar="SECONDS", help="pause between requests (default %(default)s)")
    network.add_argument("--timeout", type=float, default=defaults.timeout, metavar="SECONDS", help="per request (default %(default)s)")
    network.add_argument("--user-agent", default="", metavar="TEXT", help="custom User-Agent header")
    network.add_argument("--insecure", dest="verify_tls", action="store_false", help="accept invalid TLS certificates")
    network.add_argument("--allow-private", action="store_true", help="allow hosts on private / local networks")


def build_parser():
    parser = argparse.ArgumentParser(prog="hunter.py", description="Website Metadata Hunter")
    commands = parser.add_subparsers(dest="command")
    add_scan_arguments(commands.add_parser("scan", help="scan a website"))
    commands.add_parser("wizard", help="guided, question-by-question setup")
    commands.add_parser("gui", help="graphical interface")
    inspect = commands.add_parser("inspect", help="read metadata of local files or folders (no network)")
    inspect.add_argument("paths", nargs="+", help="files or folders (folders are read recursively)")
    inspect.add_argument("--save-copies", dest="save_files", action="store_true", help="also copy the files into the scan folder")
    add_report_arguments(inspect, ScanOptions())
    pdf = commands.add_parser("pdf", help="redaction / hidden-text check of local PDFs (no network)")
    pdf.add_argument("paths", nargs="+", help="PDF files or folders")
    pdf.add_argument("--output", dest="output_dir", default="", metavar="DIR", help="where scan folders go (default .scans)")
    subdomains = commands.add_parser("subdomains", help="list subdomains from public sources")
    subdomains.add_argument("domain")
    subdomains.add_argument("--sources", type=name_list(SUBDOMAIN_SOURCES), metavar="LIST",
                            default=[name for name in SUBDOMAIN_SOURCES if name != "links"],
                            help="crtsh,certspotter,hackertarget,alienvault,wayback (default all of them)")
    subdomains.add_argument("--output", dest="output_dir", default="", metavar="DIR", help="where scan folders go (default .scans)")
    helper = commands.add_parser("help", help="show the options of a command")
    helper.add_argument("topic", nargs="?", help="scan, inspect, pdf or subdomains")
    return parser, commands


def options_from_arguments(arguments):
    """Copy every parsed argument that matches a ScanOptions field."""
    options = ScanOptions()
    for field_name in vars(options):
        if hasattr(arguments, field_name):
            setattr(options, field_name, getattr(arguments, field_name))
    if hasattr(arguments, "info"):
        options.info_categories = arguments.info
    return options
