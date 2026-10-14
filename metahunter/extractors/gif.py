"""GIF: header, then a stream of image and extension blocks."""

import struct

from .xmp import parse_all_packets

MAX_BLOCKS = 100_000


def _skip_sub_blocks(data, position):
    """Sub-blocks are length-prefixed and end with a zero-length block. Returns (payload, next position)."""
    payload = bytearray()
    while position < len(data):
        size = data[position]
        position += 1
        if size == 0:
            break
        payload += data[position:position + size]
        position += size
    return bytes(payload), position


def extract(data, meta):
    if len(data) < 13:
        return
    meta.add("technical", "GIF version", data[3:6].decode("latin-1"))
    width, height, flags = struct.unpack_from("<HHB", data, 6)
    meta.add("technical", "Dimensions", f"{width}x{height}")
    position = 13
    if flags & 0x80:
        position += 3 * (2 ** ((flags & 7) + 1))

    frames = 0
    for _ in range(MAX_BLOCKS):
        if position >= len(data):
            break
        block = data[position]
        if block == 0x3B:                      # trailer
            break
        if block == 0x2C:                      # image descriptor
            frames += 1
            if position + 10 > len(data):
                break
            local_flags = data[position + 9]
            position += 10
            if local_flags & 0x80:
                position += 3 * (2 ** ((local_flags & 7) + 1))
            position += 1                      # LZW minimum code size
            _, position = _skip_sub_blocks(data, position)
        elif block == 0x21:                    # extension
            if position + 2 > len(data):
                break
            label = data[position + 1]
            payload, position = _skip_sub_blocks(data, position + 2)
            if label == 0xFE:
                meta.add("hidden", "GIF comment", payload)
            elif label == 0xFF:
                application = payload[:11].decode("latin-1", "replace")
                if application.startswith("NETSCAPE"):
                    meta.add("technical", "Looping animation", "yes")
                else:
                    meta.add("hidden", "Application extension", application)
        else:
            meta.warnings.append(f"Unknown GIF block 0x{block:02X} at byte {position}")
            break

    meta.add("technical", "Frames", frames)
    if b"XMP DataXMP" in data:
        parse_all_packets(data, meta)
