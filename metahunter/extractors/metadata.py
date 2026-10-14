"""The container every format parser fills in."""

import re

MAX_VALUE_LENGTH = 4000

# Control characters other than tab and newline get stripped from values.
CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def decode_text(raw):
    """Best-effort bytes -> str: UTF-8 first, then a Windows code page."""
    raw = bytes(raw)
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


def clean_value(value):
    """Turn a parsed value into a short, printable string."""
    if isinstance(value, (bytes, bytearray)):
        value = decode_text(value)
    if isinstance(value, float):
        value = f"{value:.6g}"
    if isinstance(value, (list, tuple)):
        value = ", ".join(clean_value(item) for item in value)
    text = CONTROL_CHARACTERS.sub("", str(value)).strip()
    if len(text) > MAX_VALUE_LENGTH:
        text = f"{text[:MAX_VALUE_LENGTH]}... ({len(text)} characters in total)"
    return text


class Metadata:
    """Everything found in one file.

    fields     -- (category, label, value) triples, in the order they were found
    gps_points -- (latitude, longitude, label) for anything that carried coordinates
    embedded   -- (label, file extension, bytes) for thumbnails, cover art, attachments
    warnings   -- problems hit while parsing; the file may still have yielded data

    Findings added with deep=True are things a generic metadata tool such as ExifTool
    does not report (reviewer names inside documents, data appended after an image...).
    They are kept even when ExifTool supplies the regular tags.
    """

    def __init__(self, file_format=""):
        self.format = file_format
        self.fields = []
        self.deep_fields = set()
        self.gps_points = []
        self.embedded = []
        self.warnings = []
        self._seen = set()

    def add(self, category, label, value, deep=False):
        if value is None:
            return
        text = clean_value(value)
        if not text:
            return
        key = (category, label, text)
        if deep:
            self.deep_fields.add(key)
        if key in self._seen:
            return
        self._seen.add(key)
        self.fields.append(key)

    def add_deep(self, category, label, value):
        self.add(category, label, value, deep=True)

    def deep_findings(self):
        """A copy holding only the deep findings, embedded payloads and warnings."""
        subset = Metadata(self.format)
        for key in self.fields:
            if key in self.deep_fields:
                subset.add(*key, deep=True)
        subset.gps_points = [point for point in self.gps_points
                             if ("location", f"{point[2]} coordinates", f"{point[0]}, {point[1]}") in self.deep_fields]
        subset.embedded = list(self.embedded)
        subset.warnings = list(self.warnings)
        return subset

    def add_gps(self, latitude, longitude, label, deep=False):
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            self.warnings.append(f"{label}: coordinates out of range ({latitude}, {longitude})")
            return
        point = (round(latitude, 7), round(longitude, 7), label)
        if point not in self.gps_points:
            self.gps_points.append(point)
        self.add("location", f"{label} coordinates", f"{point[0]}, {point[1]}", deep=deep)


    def add_embedded(self, label, extension, data):
        if data:
            self.embedded.append((label, extension, bytes(data)))

    def merge(self, other, prefix, skip_categories=()):
        """Fold in metadata from a file nested inside this one (cover art, embedded photo...).

        Nested files are invisible to a tool that only reads the outer file, so all of it is deep.
        """
        for category, label, value in other.fields:
            if category not in skip_categories:
                self.add(category, f"{prefix}{label}", value, deep=True)
        for latitude, longitude, label in other.gps_points:
            self.add_gps(latitude, longitude, f"{prefix}{label}", deep=True)
        for label, extension, data in other.embedded:
            self.add_embedded(f"{prefix}{label}", extension, data)
        self.warnings.extend(f"{prefix}{warning}" for warning in other.warnings)

    def absorb(self, other):
        """Take over everything from another Metadata as-is (no prefix, deep flags kept)."""
        for key in other.fields:
            self.add(*key, deep=key in other.deep_fields)
        for point in other.gps_points:
            if point not in self.gps_points:
                self.gps_points.append(point)
        self.embedded.extend(other.embedded)
        self.warnings.extend(other.warnings)

    def grouped(self, categories=None):
        """{category: [(label, value), ...]}, optionally limited to some categories."""
        groups = {}
        for category, label, value in self.fields:
            if categories is None or category in categories:
                groups.setdefault(category, []).append((label, value))
        return groups
