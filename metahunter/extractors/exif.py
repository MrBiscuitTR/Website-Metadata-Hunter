"""EXIF / TIFF reader.

TIFF files, and the EXIF blocks inside JPEG, PNG, WebP and HEIC, share one layout:
a byte-order mark, then chains of tagged directories ("IFDs") holding typed values.
"""

import struct

from .metadata import decode_text

EXIF_IFD_POINTER = 0x8769
GPS_IFD_POINTER = 0x8825
THUMBNAIL_OFFSET = 0x0201
THUMBNAIL_LENGTH = 0x0202
ICC_PROFILE_TAG = 0x8773
XMP_TAG = 0x02BC
PHOTOSHOP_TAG = 0x8649

MAX_ENTRIES_PER_IFD = 1000

# Bytes per value for each TIFF field type.
TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
NUMBER_FORMATS = {3: "H", 4: "I", 8: "h", 9: "i", 11: "f", 12: "d", 13: "I"}


class TiffReader:
    """Reads IFDs out of a TIFF-structured byte string."""

    def __init__(self, data):
        if data[:2] == b"II":
            self.endian = "<"
        elif data[:2] == b"MM":
            self.endian = ">"
        else:
            raise ValueError("missing TIFF byte-order mark")
        self.data = data
        if self.unpack("H", 2)[0] != 42:
            raise ValueError("bad TIFF magic number")

    def unpack(self, fmt, offset):
        return struct.unpack_from(self.endian + fmt, self.data, offset)

    def first_ifd_offset(self):
        return self.unpack("I", 4)[0]

    def read_ifd(self, offset):
        """Return ({tag: value}, offset of the next IFD or 0)."""
        count = self.unpack("H", offset)[0]
        if count > MAX_ENTRIES_PER_IFD:
            raise ValueError(f"IFD claims {count} entries")
        entries = {}
        for index in range(count):
            entry = offset + 2 + index * 12
            tag, field_type, value_count = self.unpack("HHI", entry)
            size = TYPE_SIZES.get(field_type)
            if size is None or size * value_count > len(self.data):
                continue
            total = size * value_count
            value_offset = entry + 8 if total <= 4 else self.unpack("I", entry + 8)[0]
            raw = self.data[value_offset:value_offset + total]
            if len(raw) == total:
                entries[tag] = self._decode(field_type, value_count, raw)
        next_position = offset + 2 + count * 12
        next_offset = self.unpack("I", next_position)[0] if next_position + 4 <= len(self.data) else 0
        return entries, next_offset

    def _decode(self, field_type, count, raw):
        if field_type == 2:
            return raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
        if field_type in (1, 6, 7):
            return raw  # plain bytes; the meaning depends on the tag
        if field_type in (5, 10):
            letter = "I" if field_type == 5 else "i"
            numbers = struct.unpack(f"{self.endian}{2 * count}{letter}", raw)
            values = [(numbers[i], numbers[i + 1]) for i in range(0, len(numbers), 2)]
        else:
            values = list(struct.unpack(f"{self.endian}{count}{NUMBER_FORMATS[field_type]}", raw))
        return values[0] if count == 1 else values


# ---------------------------------------------------------------- formatting

def ratio(value):
    """A (numerator, denominator) pair as a float, or None."""
    if isinstance(value, tuple) and len(value) == 2:
        return value[0] / value[1] if value[1] else None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def format_value(value):
    if isinstance(value, tuple) and len(value) == 2:
        number = ratio(value)
        return "undefined" if number is None else f"{number:.6g}"
    if isinstance(value, list):
        return ", ".join(str(format_value(item)) for item in value[:16])
    if isinstance(value, bytes):
        text = value.rstrip(b"\x00")
        if text and all(32 <= byte < 127 or byte in (9, 10, 13) for byte in text):
            return text.decode("ascii")
        return f"<{len(value)} bytes of binary data>"
    return value


def format_exposure(value):
    seconds = ratio(value)
    if not seconds:
        return format_value(value)
    return f"1/{round(1 / seconds)} s" if seconds < 1 else f"{seconds:g} s"


def format_f_number(value):
    number = ratio(value)
    return f"f/{number:.1f}" if number else format_value(value)


def format_millimetres(value):
    number = ratio(value)
    return f"{number:g} mm" if number is not None else format_value(value)


def format_flash(value):
    if not isinstance(value, int):
        return format_value(value)
    return "fired" if value & 1 else "did not fire"


def format_windows_xp_text(value):
    """XPAuthor and friends: UTF-16LE text stored as a BYTE array."""
    if isinstance(value, bytes):
        return value.decode("utf-16-le", "replace").rstrip("\x00")
    return format_value(value)


def format_user_comment(value):
    """UserComment: 8-byte character-set code, then the text."""
    if not isinstance(value, bytes) or len(value) < 8:
        return format_value(value)
    code, body = value[:8], value[8:]
    if code.startswith(b"UNICODE"):
        text = body.decode("utf-16-be" if body[:1] == b"\x00" else "utf-16-le", "replace")
    else:
        text = decode_text(body)
    return text.strip("\x00 ")


def format_lens_specification(value):
    numbers = [ratio(item) for item in value] if isinstance(value, list) else []
    if len(numbers) != 4 or None in numbers:
        return format_value(value)
    shortest, longest, widest_short, widest_long = numbers
    focal = f"{shortest:g}" if shortest == longest else f"{shortest:g}-{longest:g}"
    aperture = f"{widest_short:g}" if widest_short == widest_long else f"{widest_short:g}-{widest_long:g}"
    return f"{focal} mm f/{aperture}"


ORIENTATIONS = {1: "normal", 2: "mirrored", 3: "rotated 180", 4: "flipped", 5: "mirrored, rotated 270",
                6: "rotated 90 CW", 7: "mirrored, rotated 90", 8: "rotated 270 CW"}


def format_orientation(value):
    return ORIENTATIONS.get(value, format_value(value))


# tag: (label, category, formatter)
IMAGE_TAGS = {
    0x0100: ("Image width", "technical", format_value),
    0x0101: ("Image height", "technical", format_value),
    0x010D: ("Document name", "document", format_value),
    0x010E: ("Image description", "document", format_value),
    0x010F: ("Camera make", "device", format_value),
    0x0110: ("Camera model", "device", format_value),
    0x0112: ("Orientation", "technical", format_orientation),
    0x011A: ("X resolution", "technical", format_value),
    0x011B: ("Y resolution", "technical", format_value),
    0x0131: ("Software", "software", format_value),
    0x0132: ("Modified", "time", format_value),
    0x013B: ("Artist", "people", format_value),
    0x013C: ("Host computer", "device", format_value),
    0x8298: ("Copyright", "people", format_value),
    0x9C9B: ("Windows title", "document", format_windows_xp_text),
    0x9C9C: ("Windows comment", "hidden", format_windows_xp_text),
    0x9C9D: ("Windows author", "people", format_windows_xp_text),
    0x9C9E: ("Windows keywords", "document", format_windows_xp_text),
    0x9C9F: ("Windows subject", "document", format_windows_xp_text),
    0xC614: ("Unique camera model", "device", format_value),
}

PHOTO_TAGS = {
    0x829A: ("Exposure time", "camera", format_exposure),
    0x829D: ("Aperture", "camera", format_f_number),
    0x8822: ("Exposure program", "camera", format_value),
    0x8827: ("ISO", "camera", format_value),
    0x9000: ("EXIF version", "technical", format_value),
    0x9003: ("Taken", "time", format_value),
    0x9004: ("Digitized", "time", format_value),
    0x9010: ("Time zone offset", "time", format_value),
    0x9011: ("Time zone offset (taken)", "time", format_value),
    0x9204: ("Exposure bias", "camera", format_value),
    0x9207: ("Metering mode", "camera", format_value),
    0x9209: ("Flash", "camera", format_flash),
    0x920A: ("Focal length", "camera", format_millimetres),
    0x9286: ("User comment", "hidden", format_user_comment),
    0x9291: ("Sub-second (taken)", "time", format_value),
    0xA002: ("Pixel width", "technical", format_value),
    0xA003: ("Pixel height", "technical", format_value),
    0xA405: ("Focal length (35mm equivalent)", "camera", format_millimetres),
    0xA406: ("Scene type", "camera", format_value),
    0xA420: ("Image unique ID", "hidden", format_value),
    0xA430: ("Camera owner", "people", format_value),
    0xA431: ("Camera serial number", "device", format_value),
    0xA432: ("Lens", "device", format_lens_specification),
    0xA433: ("Lens make", "device", format_value),
    0xA434: ("Lens model", "device", format_value),
    0xA435: ("Lens serial number", "device", format_value),
}


def report_tags(entries, table, meta):
    for tag, value in entries.items():
        if tag in table:
            label, category, formatter = table[tag]
            meta.add(category, label, formatter(value))


# ---------------------------------------------------------------- GPS

def dms_to_degrees(dms, reference):
    """Degrees/minutes/seconds rationals plus N/S/E/W -> signed decimal degrees."""
    if not isinstance(dms, list) or len(dms) != 3:
        return None
    parts = [ratio(part) for part in dms]
    if None in parts:
        return None
    degrees = parts[0] + parts[1] / 60 + parts[2] / 3600
    return -degrees if reference in ("S", "W") else degrees


def report_gps(gps, meta, label):
    latitude = dms_to_degrees(gps.get(2), gps.get(1))
    longitude = dms_to_degrees(gps.get(4), gps.get(3))
    if latitude is not None and longitude is not None:
        if latitude == 0 and longitude == 0:
            meta.add("location", f"{label} coordinates", "0, 0 (blanked out)")
        else:
            meta.add_gps(latitude, longitude, label)
    altitude = ratio(gps.get(6))
    if altitude is not None:
        below_sea_level = gps.get(5) in (b"\x01", 1)
        meta.add("location", f"{label} altitude", f"{-altitude if below_sea_level else altitude:.1f} m")
    if 29 in gps or 7 in gps:
        date = gps.get(29, "")
        clock = gps.get(7)
        clock_text = ""
        if isinstance(clock, list) and len(clock) == 3 and None not in [ratio(c) for c in clock]:
            hours, minutes, seconds = (ratio(c) for c in clock)
            clock_text = f" {int(hours):02d}:{int(minutes):02d}:{seconds:05.2f}"
        meta.add("time", f"{label} timestamp", f"{date}{clock_text} UTC".strip())
    if 17 in gps:
        meta.add("location", f"{label} camera direction", f"{format_value(gps[17])} degrees")
    if 13 in gps:
        unit = gps.get(12, "")
        meta.add("location", f"{label} speed", f"{format_value(gps[13])} {unit}".strip())
    if 18 in gps:
        meta.add("location", f"{label} map datum", gps[18])
    if 27 in gps:
        meta.add("location", f"{label} positioning method", format_user_comment(gps[27]))
    if 31 in gps:
        meta.add("location", f"{label} accuracy", f"{format_value(gps[31])} m")


# ---------------------------------------------------------------- entry point

def parse_exif(data, meta, label="EXIF GPS"):
    """Read a TIFF-structured EXIF block into `meta`. Returns the first directory."""
    reader = TiffReader(data)
    image, thumbnail_ifd = reader.read_ifd(reader.first_ifd_offset())
    report_tags(image, IMAGE_TAGS, meta)

    for pointer, kind in ((EXIF_IFD_POINTER, "photo"), (GPS_IFD_POINTER, "GPS")):
        offset = image.get(pointer)
        if not isinstance(offset, int):
            continue
        try:
            entries, _ = reader.read_ifd(offset)
        except (struct.error, ValueError) as error:
            meta.warnings.append(f"Unreadable {kind} directory: {error}")
            continue
        if kind == "photo":
            report_tags(entries, PHOTO_TAGS, meta)
            if 0x927C in entries:
                meta.add("hidden", "Maker note (vendor-private data)", f"{len(entries[0x927C])} bytes")
        else:
            report_gps(entries, meta, label)

    if thumbnail_ifd and thumbnail_ifd < len(data):
        try:
            thumbnail, _ = reader.read_ifd(thumbnail_ifd)
            start, length = thumbnail.get(THUMBNAIL_OFFSET), thumbnail.get(THUMBNAIL_LENGTH)
            if isinstance(start, int) and isinstance(length, int) and 0 < length < len(data):
                meta.add_embedded("EXIF thumbnail", "jpg", data[start:start + length])
                meta.add("hidden", "Embedded thumbnail", f"{length} bytes (may show the uncropped original)")
        except (struct.error, ValueError) as error:
            meta.warnings.append(f"Unreadable thumbnail directory: {error}")
    return image


def extract_tiff(data, meta):
    """TIFF files (and TIFF-based camera raw files) carry XMP, IPTC and ICC as tags too."""
    from .icc import parse_icc
    from .iptc import parse_iim
    from .xmp import parse_xmp

    image = parse_exif(data, meta, label="GPS")
    if isinstance(image.get(XMP_TAG), bytes):
        parse_xmp(image[XMP_TAG], meta)
    if isinstance(image.get(ICC_PROFILE_TAG), bytes):
        parse_icc(image[ICC_PROFILE_TAG], meta)
    if isinstance(image.get(0x83BB), bytes):
        parse_iim(image[0x83BB], meta)
