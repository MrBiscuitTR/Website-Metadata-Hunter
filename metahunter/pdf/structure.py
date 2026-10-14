"""Structural facts about a PDF that need no page rendering.

Info dictionaries (including older versions), annotations and their authors,
form field values, JavaScript, attached files, fonts and embedded JPEG photos.
"""

from .objects import Name, Ref, Stream

MAX_FIELDS = 500
MAX_SCRIPTS = 20
MAX_IMAGES = 30


def pdf_text(value):
    """PDF text strings are PDFDocEncoding (about latin-1) or UTF-16 with a byte-order mark."""
    if isinstance(value, bytes):
        if value.startswith(b"\xfe\xff"):
            return value[2:].decode("utf-16-be", "replace")
        if value.startswith(b"\xff\xfe"):
            return value[2:].decode("utf-16-le", "replace")
        if value.startswith(b"\xef\xbb\xbf"):
            return value[3:].decode("utf-8", "replace")
        return value.decode("latin-1")
    if value is None:
        return ""
    return str(value)


def pdf_date(value):
    """D:20210304121500+01'00' -> 2021-03-04 12:15:00 +01:00 (raw text if it does not parse)."""
    text = pdf_text(value).strip()
    digits = text[2:] if text.startswith("D:") else text
    if len(digits) >= 8 and digits[:8].isdigit():
        stamp = f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
        if len(digits) >= 14 and digits[8:14].isdigit():
            stamp += f" {digits[8:10]}:{digits[10:12]}:{digits[12:14]}"
        zone = digits[14:].replace("\x27", "")
        if zone.startswith("Z"):
            stamp += " UTC"
        elif zone[:1] in "+-" and len(zone) >= 3:
            stamp += f" {zone[:3]}:{zone[3:5] or '00'}"
        return stamp
    return text


def info_dictionaries(document):
    """Every version of every Info dictionary, oldest first, as plain {key: text} dicts."""
    numbers = []
    for trailer in document.trailers:
        reference = trailer.get("Info")
        if isinstance(reference, Ref) and reference.number not in numbers:
            numbers.append(reference.number)
    versions = []
    for number in numbers:
        for version in document.all_versions(number):
            if isinstance(version, dict):
                entry = {str(key): pdf_text(document.resolve(value)) for key, value in version.items()}
                if entry not in versions:
                    versions.append(entry)
    return versions


def annotations(document, pages):
    found = []
    for page_number, page in enumerate(pages, start=1):
        listed = document.resolve(page.get("Annots"))
        for reference in listed if isinstance(listed, list) else []:
            annotation = document.resolve(reference)
            if not isinstance(annotation, dict):
                continue
            rect = document.resolve(annotation.get("Rect"))
            numbers = [document.resolve(v) for v in rect] if isinstance(rect, list) else []
            box = None
            if len(numbers) == 4 and all(isinstance(v, (int, float)) for v in numbers):
                box = (min(numbers[0], numbers[2]), min(numbers[1], numbers[3]),
                       max(numbers[0], numbers[2]), max(numbers[1], numbers[3]))
            action = document.resolve(annotation.get("A"))
            found.append({
                "page": page_number,
                "subtype": str(annotation.get("Subtype", "")),
                "rect": box,
                "author": pdf_text(document.resolve(annotation.get("T"))),
                "contents": pdf_text(document.resolve(annotation.get("Contents"))),
                "modified": pdf_date(document.resolve(annotation.get("M"))) if annotation.get("M") else "",
                "overlay_text": pdf_text(document.resolve(annotation.get("OverlayText"))),
                "interior_colour": document.resolve(annotation.get("IC")),
                "colour": document.resolve(annotation.get("C")),
                "uri": pdf_text(document.resolve(action.get("URI"))) if isinstance(action, dict) else "",
            })
    return found


def form_fields(document):
    acro_form = document.resolve(document.catalog().get("AcroForm"))
    roots = document.resolve(acro_form.get("Fields")) if isinstance(acro_form, dict) else None
    fields, seen = [], set()

    def walk(reference, parent_name, depth):
        if depth > 20 or len(fields) >= MAX_FIELDS:
            return
        if isinstance(reference, Ref):
            if reference.number in seen:
                return
            seen.add(reference.number)
        field = document.resolve(reference)
        if not isinstance(field, dict):
            return
        name = pdf_text(document.resolve(field.get("T")))
        full_name = f"{parent_name}.{name}" if parent_name and name else (name or parent_name)
        value = document.resolve(field.get("V"))
        if value is not None:
            fields.append((full_name or "(unnamed)", pdf_text(value) if not isinstance(value, Stream) else "<stream>"))
        kids = document.resolve(field.get("Kids"))
        for kid in kids if isinstance(kids, list) else []:
            walk(kid, full_name, depth + 1)

    for root in roots if isinstance(roots, list) else []:
        walk(root, "", 0)
    return fields


def scan_objects(document):
    """One pass over every current object, collecting the interesting kinds."""
    facts = {"javascript": [], "launch_actions": [], "embedded_files": [], "fonts": set(),
             "jpeg_images": [], "image_count": 0, "xmp": []}
    attachment_names = {}
    for number in document.object_numbers():
        value = document.get(number)
        attributes = value.attributes if isinstance(value, Stream) else value
        if not isinstance(attributes, dict):
            continue
        kind, subtype = attributes.get("Type"), attributes.get("Subtype")

        if attributes.get("S") == "JavaScript" and len(facts["javascript"]) < MAX_SCRIPTS:
            script = document.resolve(attributes.get("JS"))
            if isinstance(script, Stream):
                script = document.stream_data(script) or b""
            facts["javascript"].append(pdf_text(script)[:2000])
        elif attributes.get("S") == "Launch":
            facts["launch_actions"].append(pdf_text(document.resolve(attributes.get("F"))))
        elif kind == "Filespec" or ("EF" in attributes and isinstance(document.resolve(attributes.get("EF")), dict)):
            name = pdf_text(document.resolve(attributes.get("UF") or attributes.get("F"))) or f"attachment_{number}"
            streams = document.resolve(attributes.get("EF"))
            for reference in streams.values() if isinstance(streams, dict) else []:
                if isinstance(reference, Ref):
                    attachment_names[reference.number] = name
        elif kind in ("Font", "FontDescriptor") or "BaseFont" in attributes or "FontName" in attributes:
            font_name = attributes.get("BaseFont") or attributes.get("FontName")
            if isinstance(font_name, Name):
                facts["fonts"].add(str(font_name))
        elif isinstance(value, Stream) and subtype == "Image":
            facts["image_count"] += 1
            filters = document.resolve(value.get("Filter"))
            filters = filters if isinstance(filters, list) else [filters]
            if filters and filters[-1] in ("DCTDecode", "DCT") and len(facts["jpeg_images"]) < MAX_IMAGES:
                photo = value.raw if len(filters) == 1 else None
                if photo:
                    facts["jpeg_images"].append((number, photo))
        elif isinstance(value, Stream) and (kind == "Metadata" or subtype == "XML"):
            packet = document.stream_data(value)
            if packet:
                facts["xmp"].append(packet)

    for number, name in attachment_names.items():
        stream = document.get(number)
        content = document.stream_data(stream) if isinstance(stream, Stream) else None
        if content is not None:
            facts["embedded_files"].append((name, content))
    return facts
