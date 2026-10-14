"""PDF metadata: Info dictionary (every saved version), XMP, structure and risky features."""

from ..pdf.document import PdfDocument
from ..pdf.structure import annotations, form_fields, info_dictionaries, pdf_date, scan_objects
from .metadata import Metadata
from .xmp import parse_xmp

INFO_KEYS = {
    "Title": ("Title", "document"), "Subject": ("Subject", "document"), "Keywords": ("Keywords", "document"),
    "Author": ("Author", "people"), "Creator": ("Creator application", "software"),
    "Producer": ("Producer", "software"), "CreationDate": ("Created", "time"), "ModDate": ("Modified", "time"),
    "Company": ("Company", "people"), "SourceModified": ("Source modified", "time"),
}
DATE_KEYS = {"CreationDate", "ModDate", "SourceModified"}


def _report_info(info, meta, prefix="", deep=False):
    for key, value in info.items():
        label, category = INFO_KEYS.get(key, (f"Info {key}", "hidden"))
        meta.add(category, f"{prefix}{label}", pdf_date(value) if key in DATE_KEYS else value, deep=deep)


def extract(data, meta):
    document = PdfDocument(data)
    meta.add("technical", "PDF version", document.version)
    pages = document.pages()
    meta.add("document", "Pages", len(pages))
    if document.encrypted:
        meta.add("technical", "Encrypted", "yes (strings and streams may be unreadable)")

    versions = info_dictionaries(document)
    if versions:
        _report_info(versions[-1], meta)
        for number, older in enumerate(versions[:-1], start=1):
            changed = {key: value for key, value in older.items() if versions[-1].get(key) != value}
            _report_info(changed, meta, prefix=f"Earlier save {number}: ", deep=True)

    revisions = len(document.revision_ends())
    if revisions > 1:
        meta.add_deep("hidden", "Saved revisions", f"{revisions} (earlier versions of the content may be recoverable)")
    identifiers = next((trailer.get("ID") for trailer in reversed(document.trailers) if trailer.get("ID")), None)
    if isinstance(identifiers, list) and len(identifiers) == 2:
        first, second = (value.hex() if isinstance(value, bytes) else str(value) for value in identifiers)
        meta.add("hidden", "Document ID", first)
        if first != second:
            meta.add("hidden", "Document ID (this save)", f"{second} (differs: file was modified after creation)")

    facts = scan_objects(document)
    for packet in facts["xmp"]:
        parse_xmp(packet, meta)
    if facts["fonts"]:
        meta.add("technical", "Fonts", ", ".join(sorted(facts["fonts"])[:40]))
    meta.add("technical", "Images", facts["image_count"])
    for script in facts["javascript"]:
        meta.add_deep("hidden", "JavaScript", script)
    for target in facts["launch_actions"]:
        meta.add_deep("hidden", "Launch action (opens a program or file)", target or "(unnamed)")
    for name, content in facts["embedded_files"]:
        meta.add_deep("hidden", "Attached file", f"{name} ({len(content)} bytes)")
        meta.add_embedded(f"Attachment {name}", name.rsplit(".", 1)[-1] if "." in name else "bin", content)

    for note in annotations(document, pages):
        if note["author"]:
            meta.add_deep("people", "Annotation author", note["author"])
        if note["contents"]:
            meta.add_deep("hidden", f"{note['subtype']} annotation (page {note['page']})", note["contents"])
        if note["uri"]:
            meta.add_deep("hidden", "Link", note["uri"])
    for name, value in form_fields(document):
        meta.add_deep("hidden", f"Form field {name}", value)

    from . import extract_nested   # imported here to avoid a circular import
    for number, photo in facts["jpeg_images"]:
        inner = Metadata("jpeg")
        extract_nested(photo, inner, "")
        # Only bring over what identifies the photo, not every pixel setting.
        meta.merge(inner, f"Embedded photo {number}: ", skip_categories=("technical", "camera"))
    meta.warnings.extend(document.warnings[:20])
