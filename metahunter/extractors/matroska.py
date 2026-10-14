"""Matroska / WebM (EBML): title, muxing and writing apps, creation date, tags, attachments."""

import struct

from .binary import MATROSKA_EPOCH, seconds_since
from .metadata import decode_text

MAX_ELEMENTS = 50_000
MAX_DEPTH = 8

# Master elements we descend into.
MASTERS = {0x1A45DFA3, 0x18538067, 0x1549A966, 0x1654AE6B, 0xAE, 0xE0, 0x1254C367, 0x7373, 0x67C8,
           0x1941A469, 0x61A7}
CLUSTER = 0x1F43B675

TEXT_ELEMENTS = {
    0x4282: ("Container type", "technical"),
    0x7BA9: ("Title", "document"),
    0x4D80: ("Muxing application", "software"),
    0x5741: ("Writing application", "software"),
    0x86: ("Codec", "technical"),
    0x536E: ("Track name", "document"),
    0x466E: ("Attached file", "hidden"),
}


def read_vint(data, position):
    """EBML variable-length integer. Returns (value, length, all value bits set)."""
    first = data[position]
    if first == 0:
        raise ValueError("invalid EBML length")
    length = 9 - first.bit_length()
    raw = int.from_bytes(data[position:position + length], "big")
    value = raw & ((1 << (7 * length)) - 1)
    return value, length, value == (1 << (7 * length)) - 1


def read_element_id(data, position):
    first = data[position]
    if first == 0:
        raise ValueError("invalid EBML id")
    length = 9 - first.bit_length()
    return int.from_bytes(data[position:position + length], "big"), length


def extract(data, meta):
    state = {"scale": 1_000_000, "tag_name": None, "elements": 0}
    _walk(data, 0, len(data), meta, state, 0)


def _walk(data, start, end, meta, state, depth):
    position = start
    while position + 2 <= end and state["elements"] < MAX_ELEMENTS:
        state["elements"] += 1
        element_id, id_length = read_element_id(data, position)
        size, size_length, unknown_size = read_vint(data, position + id_length)
        body_start = position + id_length + size_length
        body_end = end if unknown_size else min(body_start + size, end)
        if element_id == CLUSTER:
            if unknown_size:
                return                     # live-streamed file: no way to skip the frames
        elif element_id in MASTERS and depth < MAX_DEPTH:
            _walk(data, body_start, body_end, meta, state, depth + 1)
        else:
            _handle_element(element_id, data[body_start:body_end], meta, state)
        position = body_end


def _handle_element(element_id, body, meta, state):
    if element_id in TEXT_ELEMENTS:
        label, category = TEXT_ELEMENTS[element_id]
        meta.add(category, label, decode_text(body))
    elif element_id == 0x2AD7B1:
        state["scale"] = int.from_bytes(body, "big") or 1_000_000
    elif element_id == 0x4489 and len(body) in (4, 8):
        duration = struct.unpack(">f" if len(body) == 4 else ">d", body)[0]
        meta.add("technical", "Duration", f"{duration * state['scale'] / 1e9:.1f} s")
    elif element_id == 0x4461 and len(body) == 8:
        nanoseconds = int.from_bytes(body, "big", signed=True)
        meta.add("time", "Created", seconds_since(MATROSKA_EPOCH, nanoseconds / 1e9))
    elif element_id in (0xB0, 0xBA):
        meta.add("technical", "Video width" if element_id == 0xB0 else "Video height", int.from_bytes(body, "big"))
    elif element_id == 0x45A3:
        state["tag_name"] = decode_text(body)
    elif element_id == 0x4487 and state["tag_name"]:
        name = state["tag_name"]
        category = {"ARTIST": "people", "COPYRIGHT": "people", "ENCODER": "software",
                    "DATE_RECORDED": "time", "RECORDING_LOCATION": "location", "COMMENT": "hidden"}.get(name, "hidden")
        meta.add(category, f"Tag {name}", decode_text(body))
