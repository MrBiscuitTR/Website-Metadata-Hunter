"""Write a PdfAnalysis to disk as plain, diff-friendly files.

  summary.txt          what was found, in plain words
  findings.json        the same, structured
  text_by_page.txt     all text, rebuilt into lines per page
  text_runs.tsv        every text operation: page, paint order, position, font, colour, render mode
  painted_shapes.tsv   every filled box and image placement, in paint order
  objects.txt          one line per PDF object, with how many saved versions exist
  revisions/           each earlier saved state of the file, plus the lines only it contains
  attachments/         files embedded in the PDF
"""

import json

from ..output import safe_filename

KIND_TITLES = {
    "covered": "Text painted over by a box or image",
    "annotation": "Text under a covering annotation",
    "invisible": "Invisible text",
    "white": "White text",
    "off-page": "Text outside the visible page",
    "tiny": "Microscopic text",
}


def _summary_text(analysis, source):
    lines = [f"PDF redaction and hidden-content check for: {source}",
             f"Pages: {analysis.page_count} (analysed {analysis.analysed_pages})",
             f"Saved revisions in file: {len(analysis.revisions) + 1}", ""]
    if not analysis.findings and not analysis.orphan_streams and not any(r.removed_lines for r in analysis.revisions):
        lines.append("Nothing hidden was found. All text in the file is also visible on the pages.")
    for kind, title in KIND_TITLES.items():
        hits = [finding for finding in analysis.findings if finding.kind == kind]
        if hits:
            lines.append(f"== {title} ({len(hits)}) ==")
            lines.extend(f"  page {hit.page}: {hit.text}    [{hit.detail}]" for hit in hits)
            lines.append("")
    for revision in analysis.revisions:
        if revision.removed_lines:
            lines.append(f"== Text only present in earlier revision {revision.number} (see revisions/) ==")
            lines.extend(f"  {line}" for line in revision.removed_lines)
            lines.append("")
    if analysis.orphan_streams:
        lines.append("== Text in content streams that no page draws ==")
        lines.extend(f"  object {number}: {text[:500]}" for number, text in analysis.orphan_streams)
        lines.append("")
    if analysis.annotations:
        lines.append("== Annotations ==")
        for note in analysis.annotations:
            details = ", ".join(f"{key}: {note[key]}" for key in ("author", "contents", "overlay_text", "modified", "uri") if note[key])
            lines.append(f"  page {note['page']}: {note['subtype']} {details}".rstrip())
        lines.append("")
    if analysis.embedded_files:
        lines.append("== Attached files (saved in attachments/) ==")
        lines.extend(f"  {name} ({len(content)} bytes)" for name, content in analysis.embedded_files)
        lines.append("")
    if analysis.warnings:
        lines.append("== Parser warnings ==")
        lines.extend(f"  {warning}" for warning in analysis.warnings[:50])
    return "\n".join(lines) + "\n"


def _findings_json(analysis, source):
    return json.dumps({
        "source": source,
        "pages": analysis.page_count,
        "counts": analysis.counts(),
        "findings": [vars(finding) for finding in analysis.findings],
        "earlier_revisions": [{"number": r.number, "bytes": r.size, "lines_only_in_this_revision": r.removed_lines}
                              for r in analysis.revisions],
        "unreferenced_text_streams": [{"object": number, "text": text} for number, text in analysis.orphan_streams],
        "annotations": [{key: value for key, value in note.items() if key not in ("interior_colour", "colour")}
                        for note in analysis.annotations],
        "attachments": [{"name": name, "bytes": len(content)} for name, content in analysis.embedded_files],
        "warnings": analysis.warnings,
    }, indent=2, ensure_ascii=False, default=str)


def _tsv(value):
    return str(value).replace("\t", " ").replace("\n", " ").replace("\r", " ")


def summarise(analysis, folder=None):
    """The short version that goes into the main report and the terminal."""
    counts = analysis.counts()
    findings = [f"page {finding.page}: {finding.text}  [{finding.detail}]" for finding in analysis.findings]
    findings += [f"earlier revision {revision.number}: {line}" for revision in analysis.revisions
                 for line in revision.removed_lines]
    # Invisible text alone is normal (OCR layers, "Powered by" tags), so it is listed but does not raise the flag.
    return {"folder": folder, "counts": counts, "findings": findings[:50],
            "hidden_text_found": any(value for key, value in counts.items() if key not in ("revisions", "invisible"))}


def write_analysis(analysis, folder, source, name):
    """Save everything under pdf_analysis/<name>/ and return a short summary for the main report."""
    base = f"pdf_analysis/{safe_filename(name, 'document')}"
    summary_path = folder.write_text(f"{base}/summary.txt", _summary_text(analysis, source))
    base = folder.relative(summary_path.parent)       # the folder name may have gained a suffix
    folder.write_text(f"{base}/findings.json", _findings_json(analysis, source))
    folder.write_text(f"{base}/text_by_page.txt", "\n".join(
        f"--- page {page} ---\n" + "\n".join(lines) for page, lines in analysis.lines_by_page.items()) + "\n")

    runs = ["page\torder\tx\ty\tsize\tfont\trender_mode\tfill\ttext"]
    runs += [f"{r.page}\t{r.order}\t{r.x:.1f}\t{r.y:.1f}\t{r.size:.2f}\t{_tsv(r.font)}\t{r.render_mode}\t"
             f"{_tsv(r.fill)}\t{_tsv(r.text)}" for r in analysis.runs]
    folder.write_text(f"{base}/text_runs.tsv", "\n".join(runs) + "\n")
    shapes = ["page\torder\tkind\tx0\ty0\tx1\ty1\tfill\topaque"]
    shapes += [f"{b.page}\t{b.order}\t{b.kind}\t" + "\t".join(f"{v:.1f}" for v in b.bbox) + f"\t{_tsv(b.fill)}\t{b.opaque}"
               for b in analysis.boxes]
    folder.write_text(f"{base}/painted_shapes.tsv", "\n".join(shapes) + "\n")
    folder.write_text(f"{base}/objects.txt", "\n".join(analysis.object_listing) + "\n")

    for revision in analysis.revisions:
        folder.write_bytes(f"{base}/revisions/revision_{revision.number}.pdf", revision.data)
        folder.write_text(f"{base}/revisions/revision_{revision.number}_lines_not_in_final.txt",
                          "\n".join(revision.removed_lines) + "\n")
    for attachment_name, content in analysis.embedded_files:
        folder.write_bytes(f"{base}/attachments/{safe_filename(attachment_name, 'attachment')}", content)
    return summarise(analysis, base)

