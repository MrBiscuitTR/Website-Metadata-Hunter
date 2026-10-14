"""Ways to print scan results in the terminal. Each view is a function: ScanResult -> text."""

from ..extractors import FORMAT_NAMES
from ..net.urls import host_of
from ..options import INFO_CATEGORIES
from ..report.highlights import short_name
from ..report.writers import build_report, render_summary

VIEWS = {
    "summary": "highlights: people, devices, software, GPS, leaks, PDF findings",
    "category": "every value grouped by category (GPS first), with the file it came from",
    "file": "each file with all of its metadata",
    "page": "files grouped by the page they were found on",
    "host": "files grouped by the domain / subdomain hosting them",
    "type": "files grouped by file type",
}
CATEGORY_ORDER = ["location", "device", "time", "people", "software", "camera", "document", "hidden", "http", "technical"]
MAX_VALUE = 160


def _clip(text, limit=MAX_VALUE):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _readable_files(result):
    return [item for item in result.files if item.metadata is not None]


def _categories(result):
    chosen = set(result.options.info_categories)
    return [category for category in CATEGORY_ORDER if category in chosen]


def key_facts(meta, categories):
    """One short line: where, what device, who, which software."""
    facts = []
    if "location" in categories and meta.gps_points:
        latitude, longitude, _ = meta.gps_points[0]
        facts.append(f"GPS {latitude}, {longitude}")
    for category, limit in (("device", 2), ("people", 2), ("software", 1), ("time", 1)):
        if category in categories:
            values = [value for field_category, _, value in meta.fields if field_category == category]
            facts += [_clip(value, 40) for value in dict.fromkeys(values[:limit])]
    return " | ".join(facts) or "no notable metadata"


def _pdf_lines(item, indent="    "):
    if not item.redaction or not item.redaction.get("hidden_text_found"):
        return []
    lines = [f"{indent}PDF check: hidden or recoverable text found"]
    lines += [f"{indent}  {_clip(finding)}" for finding in item.redaction["findings"][:10]]
    if item.redaction.get("folder"):
        lines.append(f"{indent}  details: {item.redaction['folder']}/summary.txt")
    return lines


def view_summary(result):
    return render_summary(build_report(result), result.folder.root if result.folder.created else "(nothing saved)")


def view_category(result):
    lines, files = [], _readable_files(result)
    width = min(40, max((len(short_name(item.source)) for item in files), default=10))
    for category in _categories(result):
        rows = []
        if category == "location":
            rows += [f"  {short_name(item.source):<{width}}  GPS {lat}, {lon}  ({label})"
                     for item in files for lat, lon, label in item.metadata.gps_points]
        for item in files:
            for field_category, label, value in item.metadata.fields:
                if field_category == category and not (category == "location" and "coordinates" in label):
                    rows.append(f"  {short_name(item.source):<{width}}  {label}: {_clip(value)}")
        if rows:
            lines += ["", f"=== {category.upper()} - {INFO_CATEGORIES[category]} ({len(rows)}) ===", *rows]
    pdf = [line for item in result.files for line in _pdf_lines(item, "  ")]
    if pdf:
        lines += ["", "=== PDF REDACTION CHECK ===", *pdf]
    return "\n".join(lines) or "No metadata in the chosen categories."


def view_file(result):
    lines, categories = [], _categories(result)
    for item in sorted(result.files, key=lambda entry: entry.source):
        format_name = FORMAT_NAMES.get(item.metadata.format, "") if item.metadata else ""
        lines += ["", f"--- {short_name(item.source)}  ({format_name or item.status}, {item.size:,} bytes)", f"    {item.source}"]
        if item.metadata is None:
            lines.append(f"    {item.status}" + (f" of {item.duplicate_of}" if item.duplicate_of else ""))
            continue
        groups = item.metadata.grouped(set(categories))
        for category in categories:
            for label, value in groups.get(category, []):
                lines.append(f"    [{category}] {label}: {_clip(value)}")
        lines += _pdf_lines(item)
    return "\n".join(lines) or "No files."


def _grouped_view(result, title, keys_for):
    groups, categories = {}, set(_categories(result))
    for item in _readable_files(result):
        for key in keys_for(item):
            groups.setdefault(key, []).append(item)
    lines = []
    for key in sorted(groups):
        lines += ["", f"=== {title}: {key} ({len(groups[key])} file(s)) ==="]
        for item in groups[key]:
            lines.append(f"  {short_name(item.source)}: {key_facts(item.metadata, categories)}")
            lines += _pdf_lines(item)
    return "\n".join(lines) or "No files with metadata."


def view_page(result):
    return _grouped_view(result, "PAGE", lambda item: item.found_on or ["(local file)"])


def view_host(result):
    return _grouped_view(result, "HOST", lambda item: [host_of(item.archived_from or item.source) or "(local)"])


def view_type(result):
    return _grouped_view(result, "TYPE", lambda item: [FORMAT_NAMES.get(item.metadata.format, item.metadata.format)])


RENDERERS = {"summary": view_summary, "category": view_category, "file": view_file,
             "page": view_page, "host": view_host, "type": view_type}


def render_views(result, views):
    return "\n\n".join(RENDERERS[name](result) for name in views if name in RENDERERS)
