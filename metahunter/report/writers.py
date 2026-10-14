"""Write report.json, summary.txt, report.html and scan.log into the scan folder."""

import dataclasses
import json

from .. import __version__
from ..options import INFO_CATEGORIES
from .highlights import HIGHLIGHT_CATEGORIES, collect_highlights, counts, short_name
from .html_report import render_html


def file_entry(item, categories):
    meta = item.metadata
    entry = {
        "source": item.source, "archived_from": item.archived_from, "found_on": item.found_on,
        "status": item.status, "size": item.size, "sha256": item.sha256, "saved_as": item.saved_as,
        "duplicate_of": item.duplicate_of, "embedded_files_saved": item.embedded_saved, "pdf_check": item.redaction,
    }
    if meta is not None:
        entry["format"] = meta.format
        entry["metadata"] = {category: [[label, value] for label, value in fields]
                             for category, fields in meta.grouped(categories).items()}
        entry["gps"] = [{"latitude": lat, "longitude": lon, "source": label} for lat, lon, label in meta.gps_points] \
            if "location" in categories else []
        entry["warnings"] = meta.warnings
    return entry


def build_report(result):
    categories = set(result.options.info_categories)
    return {
        "tool": {"name": "Website Metadata Hunter", "version": __version__},
        "target": result.target,
        "started": result.started.isoformat(timespec="seconds"),
        "finished": result.finished.isoformat(timespec="seconds") if result.finished else None,
        "stopped_early": result.stopped,
        "metadata_engine": result.metadata_engine,
        "options": dataclasses.asdict(result.options),
        "counts": counts(result),
        "highlights": collect_highlights(result, categories),
        "subdomains": result.subdomains,
        "pages": [dataclasses.asdict(page) for page in result.pages],
        "files": [file_entry(item, categories) for item in result.files],
    }


def render_summary(report, folder):
    lines = [f"Website Metadata Hunter {report['tool']['version']}", f"Target:   {report['target']}",
             f"Started:  {report['started']}   Finished: {report['finished']}",
             f"Engine:   {report['metadata_engine']}", f"Folder:   {folder}", ""]
    if report["stopped_early"]:
        lines += ["NOTE: the scan was stopped early; results are partial.", ""]
    lines += [f"{name:>16}: {value}" for name, value in report["counts"].items()] + [""]
    highlights = report["highlights"]
    for key, title in HIGHLIGHT_CATEGORIES:
        rows = highlights.get(key) or []
        if rows:
            lines.append(f"== {title} ==")
            lines += [f"  {value}   [{label}; {len(files)} file(s): {', '.join(files[:5])}]" for value, label, files in rows[:60]]
            lines.append("")
    if highlights.get("gps"):
        lines.append("== GPS positions ==")
        lines += [f"  {lat}, {lon}   {name} ({label})" for lat, lon, label, name in highlights["gps"]]
        lines.append("")
    if highlights.get("leaks"):
        lines.append("== Leaked paths and usernames ==")
        lines += [f"  {label}: {value}   [{', '.join(files[:5])}]" for label, value, files in highlights["leaks"]]
        lines.append("")
    if highlights.get("page_emails"):
        lines += ["== Email addresses linked from pages ==", *(f"  {email}" for email in highlights["page_emails"]), ""]
    if highlights.get("pdf"):
        lines.append("== PDFs with hidden or recoverable text ==")
        for name, check in highlights["pdf"]:
            found = ", ".join(f"{key}: {value}" for key, value in check["counts"].items() if value and key != "revisions")
            lines.append(f"  {name}: {found}   -> {check['folder']}/summary.txt")
        lines.append("")
    if report["subdomains"]:
        lines.append("== Subdomains ==")
        lines += [f"  {name}  ({'resolves' if info['resolves'] else 'no DNS record'}; {', '.join(info['sources'])})"
                  for name, info in report["subdomains"].items()]
        lines.append("")
    lines.append("Full details: report.html (open in a browser) and report.json.")
    return "\n".join(lines) + "\n"


def write_reports(result, events):
    report = build_report(result)
    folder = result.folder
    paths = {
        "json": folder.write_text("report.json", json.dumps(report, indent=2, ensure_ascii=False, default=str)),
        "summary": folder.write_text("summary.txt", render_summary(report, folder.root)),
        "html": folder.write_text("report.html", render_html(report, INFO_CATEGORIES)),
    }
    if result.subdomains:
        folder.write_text("subdomains.txt", "\n".join(result.subdomains) + "\n")
    events.log(f"Reports written to {folder.root}")
    paths["log"] = folder.write_text("scan.log", "\n".join(events.lines) + "\n")
    return paths


__all__ = ["write_reports", "build_report", "short_name"]
