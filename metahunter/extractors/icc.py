"""ICC colour profiles. Mostly technical, but the profile name can hint at the device or OS."""

import struct

from .binary import ascii_or_none

PROFILE_TEXT_TAGS = {b"desc": "ICC profile", b"dmnd": "ICC device maker", b"dmdd": "ICC device model"}


def profile_text(tag_data):
    """Decode the desc, mluc and text tag types."""
    kind = tag_data[:4]
    if kind == b"desc" and len(tag_data) >= 12:
        length = struct.unpack_from(">I", tag_data, 8)[0]
        return tag_data[12:12 + length].split(b"\x00", 1)[0].decode("latin-1")
    if kind == b"mluc" and len(tag_data) >= 28:
        length, offset = struct.unpack_from(">II", tag_data, 20)
        return tag_data[offset:offset + length].decode("utf-16-be", "replace")
    if kind == b"text":
        return tag_data[8:].split(b"\x00", 1)[0].decode("latin-1")
    return None


def parse_icc(profile, meta):
    if len(profile) < 132:
        return
    tag_count = struct.unpack_from(">I", profile, 128)[0]
    for index in range(min(tag_count, 100)):
        position = 132 + index * 12
        if position + 12 > len(profile):
            break
        signature, offset, length = struct.unpack_from(">4sII", profile, position)
        if signature in PROFILE_TEXT_TAGS:
            meta.add("technical", PROFILE_TEXT_TAGS[signature], profile_text(profile[offset:offset + length]))

    year, month, day, hour, minute, second = struct.unpack_from(">6H", profile, 24)
    if 1990 < year < 2200:
        meta.add("technical", "ICC profile date",
                 f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}")
    platform = ascii_or_none(profile[40:44])
    if platform:
        meta.add("technical", "ICC platform", {"APPL": "Apple", "MSFT": "Microsoft"}.get(platform, platform))
    creator = ascii_or_none(profile[80:84])
    if creator:
        meta.add("technical", "ICC profile creator", creator)
