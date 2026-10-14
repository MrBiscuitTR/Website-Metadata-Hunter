"""ISO base media files: MP4, MOV, M4A, 3GP, HEIC and AVIF.

Everything is a box: a 4-byte size, a 4-byte type, then the payload. Boxes nest.
Phones put the interesting bits in moov/udta and moov/meta: location, make, model, software.
"""

import re
import struct

from .binary import MAC_EPOCH, seconds_since
from .exif import parse_exif
from .metadata import decode_text
from .xmp import parse_all_packets, parse_xmp

MAX_BOXES = 50_000
MAX_DEPTH = 10
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"udta", b"edts", b"iprp", b"ipco", b"stbl"}
XMP_UUID = bytes.fromhex("BE7ACFCB97A942E89C71999491E3AFAC")
ISO6709 = re.compile(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)?")

# iTunes-style atoms, QuickTime user data and Apple/Android metadata keys: (label, category)
ITEM_FIELDS = {
    "\xa9nam": ("Title", "document"), "\xa9ART": ("Artist", "people"), "aART": ("Album artist", "people"),
    "\xa9alb": ("Album", "document"), "\xa9day": ("Date", "time"), "\xa9too": ("Encoder", "software"),
    "\xa9cmt": ("Comment", "hidden"), "\xa9wrt": ("Composer", "people"), "\xa9gen": ("Genre", "document"),
    "cprt": ("Copyright", "people"), "\xa9cpy": ("Copyright", "people"), "\xa9des": ("Description", "document"),
    "desc": ("Description", "document"), "\xa9lyr": ("Lyrics", "hidden"), "\xa9aut": ("Author", "people"),
    "\xa9mak": ("Device make", "device"), "\xa9mod": ("Device model", "device"), "\xa9swr": ("Software", "software"),
    "\xa9enc": ("Encoded by", "people"), "\xa9inf": ("Information", "document"),
    "com.apple.quicktime.make": ("Device make", "device"),
    "com.apple.quicktime.model": ("Device model", "device"),
    "com.apple.quicktime.software": ("Software", "software"),
    "com.apple.quicktime.creationdate": ("Created", "time"),
    "com.apple.quicktime.author": ("Author", "people"),
    "com.apple.quicktime.artist": ("Artist", "people"),
    "com.apple.quicktime.title": ("Title", "document"),
    "com.apple.quicktime.description": ("Description", "document"),
    "com.apple.quicktime.comment": ("Comment", "hidden"),
    "com.apple.quicktime.displayname": ("Display name", "document"),
    "com.apple.quicktime.location.name": ("Location name", "location"),
    "com.apple.quicktime.location.accuracy.horizontal": ("Location accuracy (m)", "location"),
    "com.apple.quicktime.content.identifier": ("Content identifier", "hidden"),
    "com.android.manufacturer": ("Device make", "device"),
    "com.android.model": ("Device model", "device"),
    "com.android.version": ("Android version", "software"),
    "com.android.capture.fps": ("Capture frame rate", "technical"),
}
LOCATION_KEYS = {"\xa9xyz", "com.apple.quicktime.location.ISO6709"}
THREE_GPP_STRINGS = {b"titl": ("Title", "document"), b"auth": ("Author", "people"), b"cprt": ("Copyright", "people"),
                     b"dscp": ("Description", "document"), b"perf": ("Performer", "people"),
                     b"gnre": ("Genre", "document"), b"albm": ("Album", "document")}


def iterate_boxes(data, start, end):
    """Yield (type, payload start, payload end) for each box between start and end."""
    position = start
    for _ in range(MAX_BOXES):
        if position + 8 > end:
            return
        size, box_type = struct.unpack_from(">I4s", data, position)
        header = 8
        if size == 1 and position + 16 <= end:
            size = struct.unpack_from(">Q", data, position + 8)[0]
            header = 16
        elif size == 0:
            size = end - position
        if size < header:
            return
        yield box_type, position + header, min(position + size, end)
        position += size


def report_item(name, value, meta):
    if name in LOCATION_KEYS:
        _report_iso6709(value, meta, name)
    elif name in ITEM_FIELDS:
        label, category = ITEM_FIELDS[name]
        meta.add(category, label, value)
    else:
        meta.add("hidden", f"Tag {name}", value)


def _report_iso6709(text, meta, source):
    meta.add("location", "Location (ISO 6709)", text)
    match = ISO6709.match(text.strip())
    if match:
        meta.add_gps(float(match.group(1)), float(match.group(2)), "Video GPS")
        if match.group(3):
            meta.add("location", "Video GPS altitude", f"{float(match.group(3)):.1f} m")


def extract(data, meta):
    state = {"items": {}, "locations": {}, "idat": None, "tracks": []}
    _walk(data, 0, len(data), meta, state, 0, parent=b"")
    if state["tracks"]:
        meta.add("technical", "Tracks", ", ".join(state["tracks"]))
    _read_heif_exif(data, meta, state)
    parse_all_packets(data, meta)


def _walk(data, start, end, meta, state, depth, parent):
    for box_type, box_start, box_end in iterate_boxes(data, start, end):
        payload = data[box_start:box_end] if box_type != b"mdat" else b""
        if box_type == b"ftyp" and len(payload) >= 8:
            brands = [payload[i:i + 4].decode("latin-1").strip() for i in range(8, len(payload), 4)]
            meta.add("technical", "Brand", payload[:4].decode("latin-1").strip())
            meta.add("technical", "Compatible brands", ", ".join(brand for brand in brands if brand))
        elif box_type in CONTAINERS and depth < MAX_DEPTH:
            _walk(data, box_start, box_end, meta, state, depth + 1, box_type)
        elif box_type == b"meta" and depth < MAX_DEPTH:
            _read_meta(data, box_start, box_end, meta, state, depth)
        elif box_type == b"mvhd":
            _read_movie_header(payload, meta)
        elif box_type == b"tkhd" and len(payload) >= 84:
            offset = 88 if payload[0] == 1 else 76
            width, height = struct.unpack_from(">II", payload, offset) if len(payload) >= offset + 8 else (0, 0)
            if width and height:
                meta.add("technical", "Video dimensions", f"{width >> 16}x{height >> 16}")
        elif box_type == b"hdlr" and parent == b"mdia" and len(payload) >= 12:
            state["tracks"].append({b"vide": "video", b"soun": "audio", b"text": "text",
                                    b"meta": "timed metadata"}.get(payload[8:12], payload[8:12].decode("latin-1")))
        elif box_type == b"ispe" and len(payload) >= 12:
            width, height = struct.unpack_from(">II", payload, 4)
            meta.add("technical", "Image dimensions", f"{width}x{height}")
        elif box_type == b"uuid" and payload[:16] == XMP_UUID:
            parse_xmp(payload[16:], meta)
        elif parent == b"udta" and box_type[:1] == b"\xa9":
            _read_user_data_text(box_type.decode("latin-1"), payload, meta)
        elif parent == b"udta" and box_type in THREE_GPP_STRINGS and len(payload) > 6:
            label, category = THREE_GPP_STRINGS[box_type]
            meta.add(category, label, decode_text(payload[6:]).rstrip("\x00"))
        elif parent == b"udta" and box_type == b"loci":
            _read_3gpp_location(payload, meta)


def _read_movie_header(payload, meta):
    if len(payload) < 20:
        return
    if payload[0] == 1 and len(payload) >= 32:
        created, modified, timescale, duration = struct.unpack_from(">QQIQ", payload, 4)
    else:
        created, modified, timescale, duration = struct.unpack_from(">IIII", payload, 4)
    meta.add("time", "Created (movie header)", seconds_since(MAC_EPOCH, created))
    meta.add("time", "Modified (movie header)", seconds_since(MAC_EPOCH, modified))
    if timescale:
        meta.add("technical", "Duration", f"{duration / timescale:.1f} s")


def _read_user_data_text(name, payload, meta):
    """QuickTime udta text: either a 2-byte length + 2-byte language, or nested data boxes."""
    if payload[4:8] == b"data":
        _read_data_boxes(name, payload, 0, len(payload), meta)
        return
    if len(payload) < 4:
        return
    length = struct.unpack_from(">H", payload)[0]
    report_item(name, decode_text(payload[4:4 + length]), meta)


def _read_3gpp_location(payload, meta):
    name, _, rest = payload[6:].partition(b"\x00")
    meta.add("location", "Location name", decode_text(name))
    if len(rest) >= 13:
        longitude, latitude = struct.unpack_from(">ii", rest, 1)
        meta.add_gps(latitude / 65536, longitude / 65536, "3GPP location")


def _read_meta(data, start, end, meta, state, depth):
    # ISO "meta" is a full box (4 bytes of version/flags); the QuickTime one is not.
    if data[start + 4:start + 8] != b"hdlr":
        start += 4
    handler, keys = None, []
    for box_type, box_start, box_end in iterate_boxes(data, start, end):
        if box_type == b"hdlr":
            handler = data[box_start + 8:box_start + 12]
        elif box_type == b"keys":
            keys = _read_keys(data, box_start, box_end)
        elif box_type == b"ilst":
            _read_item_list(data, box_start, box_end, meta, keys if handler == b"mdta" else None)
        elif box_type == b"iinf":
            _read_item_info(data, box_start, box_end, state)
        elif box_type == b"iloc":
            _read_item_locations(data[box_start:box_end], state)
        elif box_type == b"idat":
            state["idat"] = box_start
        elif box_type == b"iprp" and depth < MAX_DEPTH:
            _walk(data, box_start, box_end, meta, state, depth + 1, box_type)


def _read_keys(data, start, end):
    """Apple mdta "keys" box: the names that ilst entries refer to by index."""
    count = struct.unpack_from(">I", data, start + 4)[0]
    position, keys = start + 8, []
    for _ in range(min(count, 1000)):
        if position + 8 > end:
            break
        size = struct.unpack_from(">I", data, position)[0]
        if size < 8:
            break
        keys.append(data[position + 8:position + size].decode("utf-8", "replace"))
        position += size
    return keys


def _read_item_list(data, start, end, meta, keys):
    for item_type, item_start, item_end in iterate_boxes(data, start, end):
        if keys is not None:
            index = int.from_bytes(item_type, "big")
            name = keys[index - 1] if 0 < index <= len(keys) else f"key {index}"
        else:
            name = item_type.decode("latin-1")
        _read_data_boxes(name, data, item_start, item_end, meta)


def _read_data_boxes(name, data, start, end, meta):
    for box_type, box_start, box_end in iterate_boxes(data, start, end):
        if box_type == b"name":                    # freeform "----" atoms carry their own name
            name = decode_text(data[box_start + 4:box_end])
        elif box_type == b"data" and box_end - box_start >= 8:
            value_type = struct.unpack_from(">I", data, box_start)[0] & 0xFFFFFF
            value = data[box_start + 8:box_end]
            if value_type in (13, 14, 27):        # JPEG, PNG or BMP cover art
                meta.add("hidden", "Cover art", f"{len(value)} bytes")
                from . import extract_nested       # imported here to avoid a circular import
                extract_nested(value, meta, "Cover art ")
            elif value_type in (1, 2, 4, 5):
                report_item(name, decode_text(value), meta)
            elif value_type in (21, 22) and len(value) in (1, 2, 4, 8):
                report_item(name, int.from_bytes(value, "big", signed=value_type == 21), meta)


def _read_item_info(data, start, end, state):
    """HEIF iinf: which item id holds what (Exif, XMP, image tiles...)."""
    version = data[start]
    position = start + (6 if version == 0 else 8)
    for box_type, box_start, _ in iterate_boxes(data, position, end):
        if box_type != b"infe" or data[box_start] < 2:
            continue
        if data[box_start] == 2:
            item_id = struct.unpack_from(">H", data, box_start + 4)[0]
            type_at = box_start + 8
        else:
            item_id = struct.unpack_from(">I", data, box_start + 4)[0]
            type_at = box_start + 10
        state["items"][item_id] = data[type_at:type_at + 4]


def _read_item_locations(box, state):
    """HEIF iloc: where each item's bytes live in the file."""
    version = box[0]
    offset_size, length_size = box[4] >> 4, box[4] & 15
    base_offset_size = box[5] >> 4
    index_size = box[5] & 15 if version in (1, 2) else 0
    position = 6

    def read_number(size):
        nonlocal position
        value = int.from_bytes(box[position:position + size], "big") if size else 0
        position += size
        return value

    count = read_number(2 if version < 2 else 4)
    for _ in range(min(count, 10_000)):
        if position >= len(box):
            break
        item_id = read_number(2 if version < 2 else 4)
        method = read_number(2) & 0xF if version in (1, 2) else 0
        read_number(2)                               # data reference index
        base = read_number(base_offset_size)
        extents = []
        for _ in range(min(read_number(2), 1000)):
            read_number(index_size)
            offset = read_number(offset_size)
            extents.append((base + offset, read_number(length_size)))
        state["locations"][item_id] = (method, extents)


def _read_heif_exif(data, meta, state):
    for item_id, item_type in state["items"].items():
        if item_type != b"Exif" or item_id not in state["locations"]:
            continue
        method, extents = state["locations"][item_id]
        if method == 0:
            base = 0
        elif method == 1 and state["idat"] is not None:
            base = state["idat"]
        else:
            continue
        blob = b"".join(data[base + offset:base + offset + length] for offset, length in extents)
        if len(blob) < 8:
            continue
        tiff = blob[4 + struct.unpack_from(">I", blob)[0]:]
        if tiff.startswith(b"Exif\x00\x00"):
            tiff = tiff[6:]
        parse_exif(tiff, meta)
