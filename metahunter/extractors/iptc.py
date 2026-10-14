"""Photoshop image resources (JPEG APP13) and the IPTC-IIM records inside them.

News agencies and photographers fill these in: by-line, city, country, caption...
"""

import struct

from .metadata import decode_text

# IIM record 2 dataset number: (label, category)
IIM_FIELDS = {
    5: ("Object name", "document"),
    25: ("Keywords", "document"),
    40: ("Special instructions", "document"),
    55: ("Date created", "time"),
    60: ("Time created", "time"),
    62: ("Digitised date", "time"),
    65: ("Originating program", "software"),
    70: ("Program version", "software"),
    80: ("By-line (author)", "people"),
    85: ("By-line title", "people"),
    90: ("City", "location"),
    92: ("Sublocation", "location"),
    95: ("Province / state", "location"),
    100: ("Country code", "location"),
    101: ("Country", "location"),
    103: ("Original transmission reference", "hidden"),
    105: ("Headline", "document"),
    110: ("Credit", "people"),
    115: ("Source", "people"),
    116: ("Copyright notice", "people"),
    118: ("Contact", "people"),
    120: ("Caption", "document"),
    122: ("Caption writer", "people"),
}


def parse_iim(data, meta):
    position = 0
    while position + 5 <= len(data) and data[position] == 0x1C:
        record, dataset = data[position + 1], data[position + 2]
        size = struct.unpack_from(">H", data, position + 3)[0]
        if size & 0x8000:  # "extended" sizes are never used for text fields
            break
        value = data[position + 5:position + 5 + size]
        if record == 2 and dataset in IIM_FIELDS:
            label, category = IIM_FIELDS[dataset]
            meta.add(category, f"IPTC {label}", decode_text(value))
        position += 5 + size


def parse_photoshop_resources(data, meta, xmp_parser=None):
    """Walk the 8BIM blocks. `data` starts right after the Photoshop 3.0 header."""
    position = 0
    while position + 12 <= len(data) and data[position:position + 4] == b"8BIM":
        resource_id = struct.unpack_from(">H", data, position + 4)[0]
        name_length = data[position + 6]
        name_field = 1 + name_length + ((1 + name_length) & 1)  # Pascal string padded to even
        size_position = position + 6 + name_field
        if size_position + 4 > len(data):
            break
        size = struct.unpack_from(">I", data, size_position)[0]
        body = data[size_position + 4:size_position + 4 + size]
        if resource_id == 0x0404:
            parse_iim(body, meta)
        elif resource_id in (0x0409, 0x040C) and len(body) > 28:
            meta.add_embedded("Photoshop thumbnail", "jpg", body[28:])
            meta.add("hidden", "Photoshop thumbnail", f"{len(body) - 28} bytes")
        elif resource_id == 0x0424 and xmp_parser:
            xmp_parser(body, meta)
        position = size_position + 4 + size + (size & 1)
