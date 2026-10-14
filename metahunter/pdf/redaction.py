"""PDF redaction checker: finds text that is in the file but was not meant to be read.

- text painted over by a later box or image (the classic failed redaction)
- text under Redact, Square or dark Highlight annotations
- invisible text (render mode 3/7), white text, text off the page, microscopic text
- text from earlier saved revisions that the final version no longer contains
- content streams that no page draws any more
"""

import dataclasses
import re

from .content import DocumentInterpreter
from .document import PdfDocument
from .objects import Keyword, Parser, PdfError, Stream
from .structure import annotations, scan_objects

MAX_ANALYSED_PAGES = 2000
MAX_REVISIONS = 20
DARK = 0.25
LIGHT = 0.95
SKIPPED_STREAM_TYPES = {"ObjStm", "XRef", "Metadata", "EmbeddedFile"}


@dataclasses.dataclass
class Finding:
    kind: str        # covered, annotation, invisible, white, off-page, tiny
    page: int
    text: str
    detail: str


@dataclasses.dataclass
class Revision:
    number: int
    size: int
    removed_lines: list
    data: bytes


@dataclasses.dataclass
class PdfAnalysis:
    page_count: int = 0
    analysed_pages: int = 0
    findings: list = dataclasses.field(default_factory=list)
    revisions: list = dataclasses.field(default_factory=list)
    orphan_streams: list = dataclasses.field(default_factory=list)   # (object number, text)
    annotations: list = dataclasses.field(default_factory=list)
    embedded_files: list = dataclasses.field(default_factory=list)
    lines_by_page: dict = dataclasses.field(default_factory=dict)
    runs: list = dataclasses.field(default_factory=list)
    boxes: list = dataclasses.field(default_factory=list)
    object_listing: list = dataclasses.field(default_factory=list)
    warnings: list = dataclasses.field(default_factory=list)

    def counts(self):
        tally = {}
        for finding in self.findings:
            tally[finding.kind] = tally.get(finding.kind, 0) + 1
        tally["revisions"] = len(self.revisions) + 1
        tally["lines only in earlier revisions"] = sum(len(r.removed_lines) for r in self.revisions)
        tally["unreferenced text streams"] = len(self.orphan_streams)
        return tally


def describe_colour(fill):
    if fill is None:
        return "pattern / unknown colour"
    red, green, blue = (max(0.0, min(1.0, channel)) for channel in fill)
    luminance = 0.299 * red + 0.587 * green + 0.114 * blue
    if luminance <= DARK:
        return "black" if luminance < 0.05 else "dark"
    if luminance >= LIGHT:
        return "white"
    return f"coloured (rgb {red:.2f} {green:.2f} {blue:.2f})"


def luminance(fill):
    return None if fill is None else 0.299 * fill[0] + 0.587 * fill[1] + 0.114 * fill[2]


def _inside(box, x, y):
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def _join_pieces(pieces):
    """pieces: (x start, x end, text, size) on one line -> a readable string."""
    pieces.sort(key=lambda piece: piece[0])
    output, previous_end = "", None
    for start, end, text, size in pieces:
        if previous_end is not None and start - previous_end > 0.15 * max(size, 1):
            output += " "
        output += text
        previous_end = end
    return output.strip()


def layout_lines(runs):
    """Rebuild reading-order lines per page from text runs."""
    pages = {}
    for run in runs:
        pages.setdefault(run.page, []).append(run)
    result = {}
    for page, page_runs in sorted(pages.items()):
        page_runs.sort(key=lambda run: (-round(run.y, 1), run.x))
        lines, current, current_y = [], [], None
        for run in page_runs:
            if current_y is not None and abs(run.y - current_y) > max(1.0, 0.4 * run.size):
                lines.append(_join_pieces(current))
                current = []
            if not current:
                current_y = run.y
            current.append((run.x, run.end_x, run.text, run.size))
        if current:
            lines.append(_join_pieces(current))
        result[page] = [line for line in lines if line]
    return result


class _FindingCollector:
    """Groups per-run hits into one finding per line, so output reads like sentences."""

    def __init__(self):
        self.groups = {}

    def add(self, kind, run, detail, chars):
        if not chars or not "".join(char[0] for char in chars).strip():
            return
        key = (run.page, kind, detail, round(run.y / max(run.size * 0.4, 1.0)))
        piece = (chars[0][1] - chars[0][3], chars[-1][1] + chars[-1][3], "".join(char[0] for char in chars), run.size)
        self.groups.setdefault(key, []).append(piece)

    def findings(self):
        result = []
        for (page, kind, detail, _), pieces in sorted(self.groups.items(), key=lambda item: (item[0][0], -item[0][3])):
            result.append(Finding(kind, page, _join_pieces(pieces), detail))
        return result


def _find_covered_text(runs, boxes, collector):
    boxes_by_page = {}
    for box in boxes:
        boxes_by_page.setdefault(box.page, []).append(box)
    for run in runs:
        later_boxes = [box for box in boxes_by_page.get(run.page, []) if box.order > run.order and box.opaque]
        if not later_boxes:
            continue
        covered, covers = [], set()
        for char in run.chars:
            for box in later_boxes:
                if _inside(box.bbox, char[1], char[2]):
                    covered.append(char)
                    covers.add(f"{describe_colour(box.fill)} box" if box.kind == "fill" else "an image")
                    break
        collector.add("covered", run, "painted over by " + " / ".join(sorted(covers)), covered)


def _covering_annotation(annotation):
    """Annotations that hide what is under them, with a description, else None."""
    subtype = annotation["subtype"]
    if subtype == "Redact":
        return "Redact annotation that was never applied"
    interior = annotation["interior_colour"]
    if subtype in ("Square", "Polygon") and isinstance(interior, list) and len(interior) in (1, 3, 4):
        values = [float(v) for v in interior if isinstance(v, (int, float))]
        colour = describe_colour((values * 3)[:3] if len(values) == 1 else values[:3])
        return f"filled {subtype} annotation ({colour})"
    colour = annotation["colour"]
    if subtype == "Highlight" and isinstance(colour, list) and len(colour) == 3:
        values = [float(v) for v in colour if isinstance(v, (int, float))]
        if len(values) == 3 and luminance(values) <= DARK:
            return "dark Highlight annotation"
    return None


def _find_annotation_cover(runs, page_annotations, collector):
    for annotation in page_annotations:
        description = _covering_annotation(annotation)
        if not description or not annotation["rect"]:
            continue
        for run in runs:
            if run.page == annotation["page"]:
                chars = [char for char in run.chars if _inside(annotation["rect"], char[1], char[2])]
                collector.add("annotation", run, description, chars)


def _find_hidden_text(runs, media_boxes, collector):
    for run in runs:
        if run.render_mode in (3, 7):
            collector.add("invisible", run, f"render mode {run.render_mode} (normal for OCR layers on scans)", run.chars)
        elif run.render_mode in (0, 2, 4, 6) and run.fill is not None and luminance(run.fill) >= LIGHT:
            collector.add("white", run, "white text (invisible on a white background)", run.chars)
        if 0 < run.size < 1.0:
            collector.add("tiny", run, f"font size {run.size:.2f} pt", run.chars)
        media_box = media_boxes.get(run.page)
        if media_box:
            outside = [char for char in run.chars if not _inside(media_box, char[1], char[2])]
            collector.add("off-page", run, "positioned outside the visible page", outside)


def _media_box(document, page):
    box = document.resolve(page.get("CropBox") or page.get("MediaBox"))
    values = [document.resolve(v) for v in box] if isinstance(box, list) else []
    if len(values) == 4 and all(isinstance(v, (int, float)) for v in values):
        return (min(values[0], values[2]), min(values[1], values[3]), max(values[0], values[2]), max(values[1], values[3]))
    return None


def _quick_text(content):
    """Text of Tj/TJ operands without font information (for streams no page uses)."""
    pieces, operands, parser = [], [], Parser(content)
    try:
        while not parser.at_end() and len(pieces) < 5000:
            token = parser.read_object()
            if isinstance(token, Keyword):
                if token in ("Tj", "TJ", "\x27", "\""):
                    items = operands[-1] if operands and isinstance(operands[-1], list) else operands[-1:]
                    pieces.append("".join(item.decode("cp1252", "replace") for item in items if isinstance(item, bytes)))
                operands = []
            else:
                operands.append(token)
    except PdfError:
        pass
    return " ".join(piece for piece in pieces if piece.strip())


def _find_orphan_streams(document, referenced, analysis):
    for number in document.object_numbers():
        stream = document.get(number)
        if number in referenced or not isinstance(stream, Stream):
            continue
        if stream.get("Type") in SKIPPED_STREAM_TYPES or stream.get("Subtype") == "Image":
            continue
        if any(key in stream.attributes for key in ("Length1", "Length2", "Length3")):   # embedded fonts
            continue
        content = document.stream_data(stream)
        if not content or b"BT" not in content or not re.search(rb"T[jJ]", content) or b"begincmap" in content:
            continue
        text = _quick_text(content)
        if text.strip():
            analysis.orphan_streams.append((number, text))


def _normalise(line):
    return " ".join(line.split())


def _compare_revisions(document, data, final_lines, analysis):
    final = {_normalise(line) for lines in final_lines.values() for line in lines}
    for index, end in enumerate(document.revision_ends()[:-1][:MAX_REVISIONS], start=1):
        revision_data = data[:end]
        try:
            earlier = PdfDocument(revision_data)
            pages = earlier.pages()[:MAX_ANALYSED_PAGES]
            lines = layout_lines(DocumentInterpreter(earlier).run(pages).runs)
        except (PdfError, ValueError, RecursionError) as error:
            analysis.warnings.append(f"Could not read revision {index}: {error}")
            continue
        removed = [f"page {page}: {line}" for page, page_lines in lines.items()
                   for line in page_lines if _normalise(line) not in final]
        analysis.revisions.append(Revision(index, end, removed, revision_data))


def _list_objects(document):
    """One line per object: number, how many versions exist, type and stream details."""
    listing = []
    for number in document.object_numbers():
        versions = document.all_versions(number)
        current = versions[-1] if versions else None
        attributes = current.attributes if isinstance(current, Stream) else current
        if isinstance(attributes, dict):
            kind = " ".join(str(attributes.get(key)) for key in ("Type", "Subtype", "S") if attributes.get(key))
            description = f"dictionary {kind}".strip() + f" keys: {', '.join(sorted(map(str, attributes)))[:200]}"
        else:
            description = f"{type(current).__name__}: {str(current)[:120]}"
        if isinstance(current, Stream):
            decoded = document.stream_data(current)
            filters = document.resolve(current.get("Filter"))
            description += f" | stream {len(current.raw)} bytes, filter {filters}, decoded {len(decoded) if decoded is not None else 'n/a'}"
        listing.append(f"obj {number:>6}  versions {len(versions)}  {description}")
    return listing


def analyse_pdf(data):
    """Run every check. Raises PdfError only if the file is not a usable PDF at all."""
    document = PdfDocument(data)
    if not document.object_numbers():
        raise PdfError("no PDF objects found")
    analysis = PdfAnalysis()
    pages = document.pages()
    analysis.page_count = len(pages)
    analysed = pages[:MAX_ANALYSED_PAGES]
    analysis.analysed_pages = len(analysed)

    interpreter = DocumentInterpreter(document).run(analysed)
    analysis.runs, analysis.boxes = interpreter.runs, interpreter.boxes
    analysis.annotations = annotations(document, analysed)
    media_boxes = {number: _media_box(document, page) for number, page in enumerate(analysed, start=1)}

    collector = _FindingCollector()
    _find_covered_text(interpreter.runs, interpreter.boxes, collector)
    _find_annotation_cover(interpreter.runs, analysis.annotations, collector)
    _find_hidden_text(interpreter.runs, media_boxes, collector)
    analysis.findings = collector.findings()

    analysis.lines_by_page = layout_lines(interpreter.runs)
    _compare_revisions(document, data, analysis.lines_by_page, analysis)
    if len(analysed) == len(pages):     # otherwise unread pages would look orphaned
        _find_orphan_streams(document, interpreter.referenced_streams, analysis)
    analysis.embedded_files = scan_objects(document)["embedded_files"]
    analysis.object_listing = _list_objects(document)
    analysis.warnings.extend(document.warnings)
    return analysis
