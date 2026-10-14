"""Legacy Microsoft Office files (.doc, .xls, .ppt) and other OLE2 compound documents.

The prize is the SummaryInformation property set: author, last saved by, company,
template, total edit time, last printed date...
"""

import codecs
import struct

from .binary import filetime

SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
LAST_REGULAR_SECTOR = 0xFFFFFFFA
STREAM_ENTRY = 2

SUMMARY_PROPERTIES = {
    2: ("Title", "document"), 3: ("Subject", "document"), 4: ("Author", "people"),
    5: ("Keywords", "document"), 6: ("Comments", "hidden"), 7: ("Template", "document"),
    8: ("Last saved by", "people"), 9: ("Revision number", "document"), 10: ("Total editing time", "document"),
    11: ("Last printed", "time"), 12: ("Created", "time"), 13: ("Last saved", "time"),
    14: ("Pages", "document"), 15: ("Words", "document"), 18: ("Application", "software"),
}
DOCUMENT_SUMMARY_PROPERTIES = {
    2: ("Category", "document"), 7: ("Slides", "document"), 8: ("Notes pages", "document"),
    9: ("Hidden slides", "document"), 14: ("Manager", "people"), 15: ("Company", "people"),
}
TIMESTAMP_PROPERTIES = {11, 12, 13}


class CompoundFile:
    """Minimal read-only view of an OLE2 compound file: a FAT-chained mini file system."""

    def __init__(self, data):
        if not data.startswith(SIGNATURE) or len(data) < 512:
            raise ValueError("not an OLE2 compound file")
        self.data = data
        sector_shift, mini_shift = struct.unpack_from("<HH", data, 30)
        if sector_shift not in (9, 12) or mini_shift != 6:
            raise ValueError("unexpected OLE2 sector sizes")
        self.sector_size = 1 << sector_shift
        self.mini_sector_size = 1 << mini_shift
        fat_sector_count, first_directory = struct.unpack_from("<II", data, 44)
        self.mini_cutoff, first_mini_fat, _, first_difat, difat_count = struct.unpack_from("<IIIII", data, 56)

        difat = list(struct.unpack_from("<109I", data, 76))
        sector = first_difat
        for _ in range(min(difat_count, len(data) // self.sector_size)):
            if sector >= LAST_REGULAR_SECTOR:
                break
            entries = self._sector_numbers(sector)
            difat.extend(entries[:-1])
            sector = entries[-1]
        self.fat = []
        for sector in difat[:fat_sector_count]:
            if sector < LAST_REGULAR_SECTOR:
                self.fat.extend(self._sector_numbers(sector))

        self.entries = self._read_directory(first_directory)
        root = self.entries[0] if self.entries else None
        self.mini_stream = self._read_chain(root["start"]) if root else b""
        raw_mini_fat = self._read_chain(first_mini_fat)
        self.mini_fat = list(struct.unpack(f"<{len(raw_mini_fat) // 4}I", raw_mini_fat[:len(raw_mini_fat) // 4 * 4]))

    def _sector(self, number):
        offset = (number + 1) * self.sector_size
        return self.data[offset:offset + self.sector_size]

    def _sector_numbers(self, number):
        sector = self._sector(number)
        return list(struct.unpack(f"<{len(sector) // 4}I", sector))

    @staticmethod
    def _chain(start, table):
        """Follow a sector chain, refusing to loop."""
        seen, number = set(), start
        while number < LAST_REGULAR_SECTOR and number < len(table) and number not in seen:
            seen.add(number)
            yield number
            number = table[number]

    def _read_chain(self, start):
        if start >= LAST_REGULAR_SECTOR:
            return b""
        return b"".join(self._sector(number) for number in self._chain(start, self.fat))

    def _read_directory(self, first_sector):
        raw = self._read_chain(first_sector)
        entries = []
        for offset in range(0, len(raw) - 127, 128):
            name_length = struct.unpack_from("<H", raw, offset + 64)[0]
            name = raw[offset:offset + max(0, min(name_length, 64) - 2)].decode("utf-16-le", "replace")
            created, modified, start, size = struct.unpack_from("<QQIQ", raw, offset + 100)
            if self.sector_size == 512:
                size &= 0xFFFFFFFF      # version 3 files only use the low half
            entries.append({"name": name, "type": raw[offset + 66], "start": start, "size": size,
                            "created": created, "modified": modified})
        return entries

    def read_stream(self, name):
        for entry in self.entries:
            if entry["type"] == STREAM_ENTRY and entry["name"].lower() == name.lower():
                if entry["size"] < self.mini_cutoff:
                    pieces = (self.mini_stream[n * 64:(n + 1) * 64] for n in self._chain(entry["start"], self.mini_fat))
                    return b"".join(pieces)[:entry["size"]]
                return self._read_chain(entry["start"])[:entry["size"]]
        return None


def _codec_for(codepage):
    name = {65001: "utf-8", 1200: "utf-16-le"}.get(codepage, f"cp{codepage}")
    try:
        codecs.lookup(name)
        return name
    except LookupError:
        return "cp1252"


def _read_property(stream, position, codec):
    value_type = struct.unpack_from("<I", stream, position)[0] & 0xFFFF
    body = position + 4
    if value_type == 0x1E:                      # 8-bit string in the document code page
        length = struct.unpack_from("<I", stream, body)[0]
        return stream[body + 4:body + 4 + length].split(b"\x00", 1)[0].decode(codec, "replace")
    if value_type == 0x1F:                      # UTF-16 string
        length = struct.unpack_from("<I", stream, body)[0]
        return stream[body + 4:body + 4 + 2 * length].decode("utf-16-le", "replace").rstrip("\x00")
    if value_type == 0x40:                      # FILETIME
        return struct.unpack_from("<Q", stream, body)[0]
    if value_type in (0x02, 0x03, 0x13, 0x0B):
        fmt = {0x02: "<h", 0x03: "<i", 0x13: "<I", 0x0B: "<H"}[value_type]
        return struct.unpack_from(fmt, stream, body)[0]
    return None


def read_property_set(stream):
    """{property id: value} from the first section of a property-set stream."""
    if not stream or len(stream) < 48:
        return {}
    section = struct.unpack_from("<I", stream, 44)[0]
    _, count = struct.unpack_from("<II", stream, section)
    pairs = [struct.unpack_from("<II", stream, section + 8 + 8 * index) for index in range(min(count, 500))]
    codec = "cp1252"
    for property_id, offset in pairs:
        if property_id == 1:
            codepage = _read_property(stream, section + offset, codec)
            if isinstance(codepage, int):
                codec = _codec_for(codepage & 0xFFFF)
    values = {}
    for property_id, offset in pairs:
        try:
            values[property_id] = _read_property(stream, section + offset, codec)
        except struct.error:
            continue
    return values


def _report(values, table, meta):
    for property_id, value in values.items():
        if property_id not in table or value in (None, ""):
            continue
        label, category = table[property_id]
        if property_id in TIMESTAMP_PROPERTIES and table is SUMMARY_PROPERTIES:
            value = filetime(value)
        elif property_id == 10 and table is SUMMARY_PROPERTIES:
            value = f"{value / 10_000_000 / 60:.0f} minutes" if isinstance(value, int) else value
        meta.add(category, label, value)


def extract(data, meta):
    document = CompoundFile(data)
    _report(read_property_set(document.read_stream("\x05SummaryInformation")), SUMMARY_PROPERTIES, meta)
    _report(read_property_set(document.read_stream("\x05DocumentSummaryInformation")),
            DOCUMENT_SUMMARY_PROPERTIES, meta)

    names = {entry["name"] for entry in document.entries}
    if names & {"VBA", "_VBA_PROJECT", "_VBA_PROJECT_CUR", "Macros"}:
        meta.add_deep("hidden", "Macros", "file contains a VBA project")
    if "ObjectPool" in names:
        meta.add_deep("hidden", "Embedded objects", "file contains embedded OLE objects (ObjectPool)")
    if document.entries:
        root = document.entries[0]
        meta.add("time", "Storage created", filetime(root["created"]))
        meta.add("time", "Storage modified", filetime(root["modified"]))
    meta.add("technical", "Streams", ", ".join(sorted(name for name in names if name.isprintable())[:40]))
