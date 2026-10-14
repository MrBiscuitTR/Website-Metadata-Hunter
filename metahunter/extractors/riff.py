"""RIFF containers: WebP images, WAV audio and AVI video."""

import struct

from .exif import parse_exif
from .icc import parse_icc
from .metadata import decode_text
from .xmp import parse_xmp

MAX_CHUNKS = 10_000
MAX_DEPTH = 4

# LIST/INFO sub-chunks used by WAV and AVI: (label, category)
INFO_TAGS = {
    b"INAM": ("Title", "document"),
    b"IART": ("Artist", "people"),
    b"ICMT": ("Comment", "hidden"),
    b"ICOP": ("Copyright", "people"),
    b"ICRD": ("Created", "time"),
    b"ISFT": ("Software", "software"),
    b"IENG": ("Engineer", "people"),
    b"ITCH": ("Technician", "people"),
    b"ICMS": ("Commissioned by", "people"),
    b"IGNR": ("Genre", "document"),
    b"IKEY": ("Keywords", "document"),
    b"ISBJ": ("Subject", "document"),
    b"IPRD": ("Product / album", "document"),
    b"ISRC": ("Source", "people"),
    b"ISRF": ("Source form", "document"),
    b"IARL": ("Archival location", "location"),
    b"IMED": ("Medium", "document"),
}
WAVE_FORMATS = {1: "PCM", 3: "IEEE float", 6: "A-law", 7: "mu-law", 0x55: "MP3", 0xFFFE: "extensible"}


def iterate_chunks(data, start, end):
    """Yield (chunk id, body start, body end) for chunks between start and end."""
    position = start
    for _ in range(MAX_CHUNKS):
        if position + 8 > end:
            return
        chunk_id, size = struct.unpack_from("<4sI", data, position)
        body_start = position + 8
        body_end = min(body_start + size, end)
        yield chunk_id, body_start, body_end
        position = body_start + size + (size & 1)


def extract(data, meta):
    if len(data) < 12:
        return
    form = data[8:12]
    meta.add("technical", "RIFF form", form.decode("latin-1").strip())
    state = {"byte_rate": 0, "data_size": 0}
    _walk(data, 12, min(len(data), 8 + struct.unpack_from("<I", data, 4)[0]), meta, state, 0)
    if state["byte_rate"] and state["data_size"]:
        meta.add("technical", "Duration", f"{state['data_size'] / state['byte_rate']:.1f} s")


def _walk(data, start, end, meta, state, depth):
    for chunk_id, body_start, body_end in iterate_chunks(data, start, end):
        body = data[body_start:body_end]
        if chunk_id == b"LIST" and depth < MAX_DEPTH and len(body) >= 4:
            list_type = body[:4]
            if list_type == b"INFO":
                _read_info(data, body_start + 4, body_end, meta)
            elif list_type != b"movi":   # skip the actual video frames
                _walk(data, body_start + 4, body_end, meta, state, depth + 1)
        else:
            _handle_chunk(chunk_id, body, meta, state)


def _read_info(data, start, end, meta):
    for chunk_id, body_start, body_end in iterate_chunks(data, start, end):
        value = decode_text(data[body_start:body_end]).rstrip("\x00")
        label, category = INFO_TAGS.get(chunk_id, (f"INFO {chunk_id.decode('latin-1')}", "hidden"))
        meta.add(category, label, value)


def _handle_chunk(chunk_id, body, meta, state):
    # ---- WebP
    if chunk_id == b"VP8X" and len(body) >= 10:
        width = int.from_bytes(body[4:7], "little") + 1
        height = int.from_bytes(body[7:10], "little") + 1
        meta.add("technical", "Dimensions", f"{width}x{height}")
        if body[0] & 0x02:
            meta.add("technical", "Animated", "yes")
    elif chunk_id == b"VP8 " and len(body) >= 10:
        width, height = struct.unpack_from("<HH", body, 6)
        meta.add("technical", "Dimensions", f"{width & 0x3FFF}x{height & 0x3FFF}")
        meta.add("technical", "Compression", "lossy (VP8)")
    elif chunk_id == b"VP8L" and len(body) >= 5:
        bits = int.from_bytes(body[1:5], "little")
        meta.add("technical", "Dimensions", f"{(bits & 0x3FFF) + 1}x{((bits >> 14) & 0x3FFF) + 1}")
        meta.add("technical", "Compression", "lossless (VP8L)")
    elif chunk_id == b"EXIF":
        parse_exif(body[6:] if body.startswith(b"Exif\x00\x00") else body, meta)
    elif chunk_id in (b"XMP ", b"_PMX"):
        parse_xmp(body, meta)
    elif chunk_id == b"ICCP":
        parse_icc(body, meta)
    # ---- WAV
    elif chunk_id == b"fmt " and len(body) >= 16:
        audio_format, channels, sample_rate, byte_rate, _, bits = struct.unpack_from("<HHIIHH", body)
        meta.add("technical", "Audio format", WAVE_FORMATS.get(audio_format, f"code {audio_format}"))
        meta.add("technical", "Channels", channels)
        meta.add("technical", "Sample rate", f"{sample_rate} Hz")
        meta.add("technical", "Bits per sample", bits)
        state["byte_rate"] = byte_rate
    elif chunk_id == b"data":
        state["data_size"] = len(body)
    elif chunk_id == b"bext" and len(body) >= 602:
        _read_broadcast_extension(body, meta)
    elif chunk_id == b"iXML":
        meta.add("hidden", "iXML production data", decode_text(body))
    elif chunk_id in (b"id3 ", b"ID3 "):
        from .audio import parse_id3v2   # imported here to avoid a circular import
        parse_id3v2(body, meta)
    # ---- AVI
    elif chunk_id == b"avih" and len(body) >= 40:
        microseconds_per_frame, _, _, _, total_frames = struct.unpack_from("<IIIII", body)
        width, height = struct.unpack_from("<II", body, 32)
        meta.add("technical", "Dimensions", f"{width}x{height}")
        if microseconds_per_frame:
            meta.add("technical", "Frame rate", f"{1_000_000 / microseconds_per_frame:.2f} fps")
            meta.add("technical", "Duration", f"{total_frames * microseconds_per_frame / 1_000_000:.1f} s")
    elif chunk_id == b"IDIT":
        meta.add("time", "Recorded (IDIT)", decode_text(body).rstrip("\x00"))


def _read_broadcast_extension(body, meta):
    """Broadcast WAV (bext): recorder name, reference and timestamp."""
    def text(start, length):
        return decode_text(body[start:start + length].split(b"\x00", 1)[0])

    meta.add("document", "Broadcast description", text(0, 256))
    meta.add("device", "Originator (recorder)", text(256, 32))
    meta.add("hidden", "Originator reference", text(288, 32))
    date, clock = text(320, 10), text(330, 8)
    if date:
        meta.add("time", "Origination date", f"{date} {clock}".strip())
    meta.add("software", "Coding history", text(602, len(body) - 602))
