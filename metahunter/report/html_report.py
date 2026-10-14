"""report.html: one self-contained page.

No scripts and no external resources; a Content-Security-Policy enforces that, so
values copied from untrusted files can never run anything when the report is opened.
Every value is HTML-escaped. Links are only made for http(s) URLs and our own files.
"""

from html import escape

from .highlights import short_name

CSS = """
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#1d2330;background:#f4f6f9}
main{max-width:1100px;margin:0 auto;padding:24px 20px 60px}
h1{font-size:24px;margin:0 0 4px}
h2{font-size:18px;margin:32px 0 10px;padding-bottom:6px;border-bottom:2px solid #dfe4ec}
a{color:#1f5fbf;word-break:break-all}
.muted{color:#5d6675}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin:16px 0}
.card{background:#fff;border:1px solid #dfe4ec;border-radius:8px;padding:10px 12px}
.card b{display:block;font-size:20px}
table{width:100%;border-collapse:collapse;table-layout:fixed;background:#fff;border:1px solid #dfe4ec;border-radius:8px;overflow:hidden}
th,td{text-align:left;vertical-align:top;padding:6px 10px;border-top:1px solid #edf0f5;overflow-wrap:anywhere;white-space:pre-wrap}
th{background:#f8f9fb;font-weight:600}
td.label{width:32%;color:#3b4454}
tr.category th{background:#eef2f8;color:#244a85;text-transform:capitalize}
details{background:#fff;border:1px solid #dfe4ec;border-radius:8px;margin:8px 0}
summary{cursor:pointer;padding:10px 12px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
summary .name{font-weight:600;overflow-wrap:anywhere;min-width:0}
details .body{padding:0 12px 12px}
.tag{font-size:12px;padding:1px 8px;border-radius:10px;background:#e8edf5;color:#34425a;white-space:nowrap}
.tag.warn{background:#fde8e8;color:#9b1c1c}
.tag.good{background:#e3f4e8;color:#1e6b34}
.alert{background:#fff4e5;border:1px solid #f3c784;border-radius:8px;padding:10px 12px;margin:8px 0}
ul{margin:6px 0;padding-left:20px}
"""
CSP = "default-src 'none'; style-src 'unsafe-inline'"


def _text(value):
    return escape(str(value))


def _web_link(url, text=None):
    if str(url).startswith(("http://", "https://")):
        return f'<a href="{escape(url)}" rel="noreferrer noopener">{_text(text or url)}</a>'
    return _text(text or url)


def _file_link(path, text=None):
    """Link to a file we wrote into the scan folder (relative path, safe characters only)."""
    return f'<a href="{escape(path)}">{_text(text or path)}</a>' if path else ""


def _map_link(latitude, longitude):
    url = f"https://www.openstreetmap.org/?mlat={latitude}&mlon={longitude}#map=16/{latitude}/{longitude}"
    return _web_link(url, f"{latitude}, {longitude}")


def _table(header, rows):
    head = "".join(f"<th>{_text(cell)}</th>" for cell in header)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


def _highlights(report):
    highlights, parts = report["highlights"], []
    titles = {"people": "People, companies and emails", "device": "Devices and serial numbers",
              "software": "Software", "location": "Places"}
    for key, title in titles.items():
        rows = highlights.get(key) or []
        if rows:
            parts.append(f"<h2>{_text(title)}</h2>")
            parts.append(_table(["Value", "Seen as", "Files"], [
                (_text(value), _text(label), _text(", ".join(files[:8]) + (" ..." if len(files) > 8 else "")))
                for value, label, files in rows[:200]]))
    if highlights.get("gps"):
        parts.append("<h2>GPS positions</h2>")
        parts.append(_table(["Position (opens OpenStreetMap)", "Source", "File"], [
            (_map_link(lat, lon), _text(label), _text(name)) for lat, lon, label, name in highlights["gps"]]))
    if highlights.get("leaks"):
        parts.append("<h2>Leaked paths and usernames</h2>")
        parts.append(_table(["Kind", "Value", "Files"], [
            (_text(label), _text(value), _text(", ".join(files[:8]))) for label, value, files in highlights["leaks"]]))
    if highlights.get("page_emails"):
        parts.append("<h2>Email addresses linked from pages</h2><ul>")
        parts += [f"<li>{_text(email)}</li>" for email in highlights["page_emails"]]
        parts.append("</ul>")
    if highlights.get("pdf"):
        parts.append("<h2>PDFs with hidden or recoverable text</h2>")
        for name, check in highlights["pdf"]:
            found = ", ".join(f"{key}: {value}" for key, value in check["counts"].items() if value and key != "revisions")
            parts.append(f'<div class="alert"><b>{_text(name)}</b>: {_text(found)}<br>'
                         f'Details: {_file_link(check["folder"] + "/summary.txt")}</div>')
    return "".join(parts)


def _file_section(entry, category_titles):
    name = short_name(entry["source"])
    tags = [f'<span class="tag">{_text(entry.get("format", "-"))}</span>']
    if entry["status"] != "ok":
        tags.append(f'<span class="tag warn">{_text(entry["status"])}</span>')
    if entry.get("gps"):
        tags.append('<span class="tag warn">GPS</span>')
    if entry.get("pdf_check") and entry["pdf_check"].get("hidden_text_found"):
        tags.append('<span class="tag warn">hidden PDF text</span>')
    details = [f"Source: {_web_link(entry['source'])}"]
    if entry["archived_from"]:
        details.append(f"Archived copy of: {_web_link(entry['archived_from'])}")
    if entry["found_on"]:
        details.append("Found on: " + ", ".join(_web_link(page) for page in entry["found_on"][:5]))
    if entry["saved_as"]:
        details.append(f"Saved as: {_file_link(entry['saved_as'])}")
    if entry["duplicate_of"]:
        details.append(f"Same bytes as: {_web_link(entry['duplicate_of'])}")
    if entry.get("pdf_check"):
        details.append(f"PDF check: {_file_link(entry['pdf_check']['folder'] + '/summary.txt')}")
    if entry["embedded_files_saved"]:
        details.append("Extracted: " + ", ".join(_file_link(path, path.rsplit("/", 1)[-1]) for path in entry["embedded_files_saved"]))
    rows = []
    for category, fields in entry.get("metadata", {}).items():
        rows.append(f'<tr class="category"><th colspan="2">{_text(category)} - {_text(category_titles.get(category, ""))}</th></tr>')
        rows += [f'<tr><td class="label">{_text(label)}</td><td>{_text(value)}</td></tr>' for label, value in fields]
    if entry.get("warnings"):
        rows.append('<tr class="category"><th colspan="2">parser warnings</th></tr>')
        rows += [f'<tr><td class="label">warning</td><td>{_text(warning)}</td></tr>' for warning in entry["warnings"][:20]]
    table = f"<table>{''.join(rows)}</table>" if rows else ""
    return (f'<details><summary><span class="name">{_text(name)}</span>{"".join(tags)}</summary>'
            f'<div class="body"><p class="muted">{"<br>".join(details)}</p>{table}</div></details>')


def render_html(report, category_titles):
    cards = "".join(f'<div class="card"><b>{_text(value)}</b>{_text(name)}</div>' for name, value in report["counts"].items())
    stopped = '<div class="alert">The scan was stopped early; results are partial.</div>' if report["stopped_early"] else ""
    files = sorted(report["files"], key=lambda entry: (entry["status"] != "ok", entry["source"]))
    parts = [
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Metadata report - {_text(report['target'])}</title><style>{CSS}</style></head><body><main>",
        f"<h1>Metadata report</h1><div class=\"muted\">{_web_link(report['target'])}<br>"
        f"{_text(report['started'])} to {_text(report['finished'])} &middot; {_text(report['metadata_engine'])}</div>",
        stopped, f'<div class="cards">{cards}</div>', _highlights(report),
        f"<h2>Files ({len(files)})</h2>",
        "".join(_file_section(entry, category_titles) for entry in files) or '<p class="muted">No files found.</p>',
    ]
    if report["subdomains"]:
        parts.append(f"<h2>Subdomains ({len(report['subdomains'])})</h2>")
        parts.append(_table(["Name", "DNS", "Found by"], [
            (_web_link(f"https://{name}/", name), "resolves" if info["resolves"] else "no record", _text(", ".join(info["sources"])))
            for name, info in report["subdomains"].items()]))
    if report["pages"]:
        parts.append(f"<h2>Pages visited ({len(report['pages'])})</h2>")
        parts.append(_table(["Page", "Status", "Title / generator"], [
            (_web_link(page["url"]), _text(page["status"]),
             _text(" | ".join(part for part in (page["title"], page["generator"], page["author"]) if part)))
            for page in report["pages"]]))
    parts.append("</main></body></html>")
    return "".join(parts)
