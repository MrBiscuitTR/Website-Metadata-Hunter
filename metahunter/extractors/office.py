"""Office Open XML (docx/xlsx/pptx), OpenDocument (odt/ods/odp) and other ZIP-based files.

Besides the document properties, these often leak reviewer names (tracked changes,
comments), the folder a spreadsheet was saved from, printer names and hidden sheets.
"""

import html
import io
import re
import zipfile

from .metadata import decode_text

MAX_MEMBERS = 5000
MAX_MEMBER_SIZE = 32 * 1024 * 1024
MAX_NESTED_MEDIA = 30

CORE_PROPERTIES = {
    "dc:creator": ("Author", "people"),
    "cp:lastModifiedBy": ("Last modified by", "people"),
    "dcterms:created": ("Created", "time"),
    "dcterms:modified": ("Modified", "time"),
    "cp:lastPrinted": ("Last printed", "time"),
    "cp:revision": ("Revision", "document"),
    "cp:version": ("Version", "document"),
    "dc:title": ("Title", "document"),
    "dc:subject": ("Subject", "document"),
    "cp:keywords": ("Keywords", "document"),
    "dc:description": ("Description", "document"),
    "cp:category": ("Category", "document"),
    "cp:contentStatus": ("Status", "document"),
    "dc:language": ("Language", "document"),
}
APP_PROPERTIES = {
    "Application": ("Application", "software"),
    "AppVersion": ("Application version", "software"),
    "Company": ("Company", "people"),
    "Manager": ("Manager", "people"),
    "Template": ("Template", "document"),
    "TotalTime": ("Total editing time (minutes)", "document"),
    "Pages": ("Pages", "document"),
    "Words": ("Words", "document"),
    "Slides": ("Slides", "document"),
    "Notes": ("Slides with notes", "document"),
    "HiddenSlides": ("Hidden slides", "document"),
    "HyperlinkBase": ("Hyperlink base", "hidden"),
}
ODF_PROPERTIES = {
    "meta:initial-creator": ("Author", "people"),
    "dc:creator": ("Last modified by", "people"),
    "meta:printed-by": ("Last printed by", "people"),
    "meta:creation-date": ("Created", "time"),
    "dc:date": ("Modified", "time"),
    "meta:print-date": ("Last printed", "time"),
    "meta:generator": ("Generator", "software"),
    "meta:editing-cycles": ("Revisions", "document"),
    "meta:editing-duration": ("Editing time", "document"),
    "dc:title": ("Title", "document"),
    "dc:subject": ("Subject", "document"),
    "dc:description": ("Description", "document"),
    "meta:keyword": ("Keywords", "document"),
    "dc:language": ("Language", "document"),
}
MEDIA_FOLDERS = ("word/media/", "xl/media/", "ppt/media/", "Pictures/")
TAGS = re.compile(r"<[^>]+>")


def element_texts(xml, tag):
    """Inner text of every <tag>...</tag>, with markup stripped and entities decoded."""
    pattern = rf"<{re.escape(tag)}(?:\s[^>]*)?>(.*?)</{re.escape(tag)}>"
    return [html.unescape(TAGS.sub("", value)).strip() for value in re.findall(pattern, xml, re.S)]


def attribute_values(xml, attribute):
    return [html.unescape(value) for value in re.findall(rf"\b{re.escape(attribute)}=\"([^\"]*)\"", xml)]


class SafeZip:
    """Reads ZIP members with limits on count and size."""

    def __init__(self, data):
        self.archive = zipfile.ZipFile(io.BytesIO(data))
        self.members = self.archive.infolist()[:MAX_MEMBERS]
        self.names = [member.filename for member in self.members]

    def read(self, name):
        try:
            member = self.archive.getinfo(name)
        except KeyError:
            return None
        if member.file_size > MAX_MEMBER_SIZE:
            return None
        return self.archive.read(member)

    def read_text(self, name):
        raw = self.read(name)
        return decode_text(raw) if raw is not None else ""


def _report_properties(xml, table, meta):
    for tag, (label, category) in table.items():
        for value in element_texts(xml, tag):
            meta.add(category, label, value)


def _report_people(package, meta):
    """Reviewer names hiding in tracked changes and comments."""
    for name in package.names:
        if name.startswith("word/") and name.endswith(".xml"):
            for author in attribute_values(package.read_text(name), "w:author"):
                meta.add_deep("people", "Tracked change / comment author", author)
        elif re.match(r"xl/comments\d*\.xml$", name):
            for author in element_texts(package.read_text(name), "author"):
                meta.add_deep("people", "Comment author", author)
        elif name.startswith("xl/persons/"):
            for person in attribute_values(package.read_text(name), "displayName"):
                meta.add_deep("people", "Comment author", person)
        elif name in ("ppt/commentAuthors.xml", "ppt/authors.xml"):
            for author in attribute_values(package.read_text(name), "name"):
                meta.add_deep("people", "Comment author", author)


def _report_workbook(package, meta):
    workbook = package.read_text("xl/workbook.xml")
    for path in attribute_values(workbook, "url"):
        meta.add_deep("hidden", "Saved from folder", path)
    for sheet in re.findall(r"<sheet\b[^>]*>", workbook):
        if re.search(r"\bstate=\"(hidden|veryHidden)\"", sheet):
            names = attribute_values(sheet, "name")
            meta.add_deep("hidden", "Hidden sheet", names[0] if names else sheet)


def _report_relationships(package, meta):
    for name in package.names:
        if not name.endswith(".rels"):
            continue
        for relation in re.findall(r"<Relationship\b[^>]*>", package.read_text(name)):
            if "TargetMode=\"External\"" in relation:
                for target in attribute_values(relation, "Target"):
                    meta.add_deep("hidden", "External link / template", target)


def _report_package_contents(package, meta):
    names = package.names
    if any(name.endswith("vbaProject.bin") for name in names):
        meta.add_deep("hidden", "Macros", "file contains a VBA project")
    notes = [name for name in names if name.startswith("ppt/notesSlides/") and name.endswith(".xml")]
    if notes:
        meta.add_deep("hidden", "Speaker notes", f"{len(notes)} slides have notes")
    for name in names:
        if "printerSettings" in name and name.endswith(".bin"):
            raw = package.read(name) or b""
            printer = raw[:64].decode("utf-16-le", "replace").split("\x00", 1)[0].strip()
            meta.add_deep("device", "Printer", printer)
    for name in ("docProps/thumbnail.jpeg", "docProps/thumbnail.png", "Thumbnails/thumbnail.png"):
        thumbnail = package.read(name)
        if thumbnail:
            meta.add_deep("hidden", "Document thumbnail", f"{len(thumbnail)} bytes")
            meta.add_embedded("Document thumbnail", name.rsplit(".", 1)[-1], thumbnail)

    media = [name for name in names if name.startswith(MEDIA_FOLDERS) and not name.endswith("/")]
    if media:
        meta.add_deep("hidden", "Embedded media files", f"{len(media)}: " + ", ".join(media[:20]))
    from . import extract_nested   # imported here to avoid a circular import
    for name in media[:MAX_NESTED_MEDIA]:
        content = package.read(name)
        if content:
            extract_nested(content, meta, f"{name.rsplit('/', 1)[-1]}: ", skip_technical=True)


def extract(data, meta):
    package = SafeZip(data)
    names = set(package.names)
    if "[Content_Types].xml" in names:
        meta.add("technical", "Container", "Office Open XML")
        _report_properties(package.read_text("docProps/core.xml"), CORE_PROPERTIES, meta)
        _report_properties(package.read_text("docProps/app.xml"), APP_PROPERTIES, meta)
        custom = package.read_text("docProps/custom.xml")
        for name, value in re.findall(r"<property\b[^>]*\bname=\"([^\"]*)\"[^>]*>(.*?)</property>", custom, re.S):
            meta.add("hidden", f"Custom property: {html.unescape(name)}", html.unescape(TAGS.sub("", value)))
        _report_people(package, meta)
        _report_workbook(package, meta)
        _report_relationships(package, meta)
        _report_package_contents(package, meta)
    elif "mimetype" in names:
        meta.add("technical", "Container", package.read_text("mimetype").strip())
        odf_meta = package.read_text("meta.xml")
        _report_properties(odf_meta, ODF_PROPERTIES, meta)
        for name, value in re.findall(r"<meta:user-defined\b[^>]*meta:name=\"([^\"]*)\"[^>]*>(.*?)</meta:user-defined>",
                                      odf_meta, re.S):
            meta.add("hidden", f"Custom property: {html.unescape(name)}", html.unescape(value))
        _report_package_contents(package, meta)
    else:
        meta.add("technical", "Container", "ZIP archive")
        meta.add("technical", "Entries", len(package.members))
        if package.archive.comment:
            meta.add("hidden", "Archive comment", package.archive.comment)
        dates = sorted(member.date_time for member in package.members if member.date_time[0] > 1980)
        if dates:
            first, last = dates[0], dates[-1]
            meta.add("time", "Oldest entry (local time of the zipping machine)", "%04d-%02d-%02d %02d:%02d:%02d" % first)
            meta.add("time", "Newest entry (local time of the zipping machine)", "%04d-%02d-%02d %02d:%02d:%02d" % last)
