"""The "what did we learn" overview: unique people, devices, software, places and leaks across all files."""

import posixpath
from urllib.parse import unquote, urlsplit

HIGHLIGHT_CATEGORIES = (
    ("people", "People, companies and emails"),
    ("device", "Devices and serial numbers"),
    ("software", "Software"),
    ("location", "Places"),
)
LEAK_LABELS = {"Windows user folder", "Unix / macOS home folder", "Network share", "Username (from a file path)",
               "Saved from folder", "External link / template", "Export path", "Absolute reference path",
               "Document folder", "Link to local file"}
# Labels whose values say nothing about who or what made the file.
UNINTERESTING_LABELS = ("Detected format", "Metadata read by", "File size", "SHA-256")


def short_name(source):
    path = urlsplit(source).path if "://" in source else source.replace("\x5c", "/")
    return posixpath.basename(unquote(path)) or source


def visible_files(result):
    return [item for item in result.files if item.metadata is not None]


def collect_highlights(result, categories):
    """{key: [(value, label, [file names])]} for the chosen categories, plus GPS, leaks and PDF findings."""
    highlights = {}
    for category, _ in HIGHLIGHT_CATEGORIES:
        if category not in categories:
            continue
        values = {}
        for item in visible_files(result):
            for field_category, label, value in item.metadata.fields:
                if field_category != category or label in UNINTERESTING_LABELS:
                    continue
                if category == "location" and "GPS" in label:     # positions get their own table
                    continue
                entry = values.setdefault(value, (label, set()))
                entry[1].add(short_name(item.source))

        highlights[category] = sorted(((value, label, sorted(files)) for value, (label, files) in values.items()),
                                      key=lambda row: (-len(row[2]), row[0].lower()))
    if "location" in categories:
        highlights["gps"] = [(latitude, longitude, label, short_name(item.source))
                             for item in visible_files(result) for latitude, longitude, label in item.metadata.gps_points]
    if "hidden" in categories or "people" in categories:
        leaks = {}
        for item in visible_files(result):
            for _, label, value in item.metadata.fields:
                if label in LEAK_LABELS:
                    leaks.setdefault((label, value), set()).add(short_name(item.source))
        highlights["leaks"] = sorted((label, value, sorted(files)) for (label, value), files in leaks.items())
    emails = {email for page in result.pages for email in page.emails}
    if emails and "people" in categories:
        highlights["page_emails"] = sorted(emails)
    highlights["pdf"] = [(short_name(item.source), item.redaction) for item in result.files
                         if item.redaction and item.redaction.get("hidden_text_found")]
    return highlights


def counts(result):
    statuses = {}
    for item in result.files:
        key = item.status.split(":", 1)[0]
        statuses[key] = statuses.get(key, 0) + 1
    return {
        "pages visited": len(result.pages),
        "files found": len(result.files),
        "files read": statuses.get("ok", 0),
        "duplicates": statuses.get("duplicate", 0),
        "failed": statuses.get("error", 0),
        "skipped": statuses.get("skipped", 0),
        "files with GPS": sum(1 for item in visible_files(result) if item.metadata.gps_points),
        "subdomains": len(result.subdomains),
    }
