"""Audio tags: ID3 (MP3, and inside WAV/AIFF), FLAC, Ogg Vorbis and Opus."""

import base64
import struct

from .metadata import Metadata, decode_text

MAX_FRAMES = 2000

# ID3v2 frame id: (label, category). v2.2 three-letter ids are mapped to these too.
ID3_FRAMES = {
    "TIT1": ("Content group", "document"), "TIT2": ("Title", "document"), "TIT3": ("Subtitle", "document"),
    "TALB": ("Album", "document"), "TCON": ("Genre", "document"), "TRCK": ("Track", "document"),
    "TPOS": ("Disc", "document"), "TLAN": ("Language", "document"), "TKEY": ("Musical key", "document"),
    "TBPM": ("BPM", "technical"), "TLEN": ("Length (ms)", "technical"), "TMED": ("Media type", "technical"),
    "TPE1": ("Artist", "people"), "TPE2": ("Album artist", "people"), "TPE3": ("Conductor", "people"),
    "TPE4": ("Remixed by", "people"), "TCOM": ("Composer", "people"), "TEXT": ("Lyricist", "people"),
    "TOPE": ("Original artist", "people"), "TOWN": ("File owner", "people"), "TPUB": ("Publisher", "people"),
    "TCOP": ("Copyright", "people"), "TENC": ("Encoded by", "people"), "TSSE": ("Encoder settings", "software"),
    "TYER": ("Year", "time"), "TDAT": ("Date (DDMM)", "time"), "TIME": ("Time (HHMM)", "time"),
    "TDRC": ("Recorded", "time"), "TDEN": ("Encoded", "time"), "TDOR": ("Original release", "time"),
    "TDRL": ("Released", "time"), "TDTG": ("Tagged", "time"), "TORY": ("Original year", "time"),
    "TSRC": ("ISRC code", "document"),
}
ID3V22_IDS = {"TT1": "TIT1", "TT2": "TIT2", "TT3": "TIT3", "TAL": "TALB", "TCO": "TCON", "TRK": "TRCK",
              "TPA": "TPOS", "TP1": "TPE1", "TP2": "TPE2", "TP3": "TPE3", "TP4": "TPE4", "TCM": "TCOM",
              "TXT": "TEXT", "TOA": "TOPE", "TPB": "TPUB", "TCR": "TCOP", "TEN": "TENC", "TSS": "TSSE",
              "TYE": "TYER", "TDA": "TDAT", "TIM": "TIME", "TLE": "TLEN", "COM": "COMM", "PIC": "APIC",
              "TXX": "TXXX", "WXX": "WXXX", "GEO": "GEOB"}
ID3_ENCODINGS = {0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}

# Vorbis comment keys (FLAC, Ogg, Opus): (label, category)
VORBIS_FIELDS = {
    "TITLE": ("Title", "document"), "ALBUM": ("Album", "document"), "GENRE": ("Genre", "document"),
    "TRACKNUMBER": ("Track", "document"), "ARTIST": ("Artist", "people"), "PERFORMER": ("Performer", "people"),
    "ALBUMARTIST": ("Album artist", "people"), "COMPOSER": ("Composer", "people"),
    "COPYRIGHT": ("Copyright", "people"), "ORGANIZATION": ("Organisation", "people"),
    "CONTACT": ("Contact", "people"), "ENCODED-BY": ("Encoded by", "people"),
    "DATE": ("Date", "time"), "LOCATION": ("Recording location", "location"),
    "ENCODER": ("Encoder", "software"), "DESCRIPTION": ("Description", "hidden"), "COMMENT": ("Comment", "hidden"),
}


def _split_terminated(data, encoding):
    """Split at the first string terminator (two zero bytes for UTF-16)."""
    terminator = b"\x00\x00" if encoding in (1, 2) else b"\x00"
    index = data.find(terminator)
    while terminator == b"\x00\x00" and index > 0 and index % 2:
        index = data.find(terminator, index + 1)
    if index < 0:
        return data, b""
    return data[:index], data[index + len(terminator):]


def _id3_text(data, encoding):
    return data.decode(ID3_ENCODINGS.get(encoding, "latin-1"), "replace").replace("\x00", " / ").strip(" /")


def _unsynchronise(data):
    return data.replace(b"\xff\x00", b"\xff")


def _syncsafe(raw):
    return (raw[0] << 21) | (raw[1] << 14) | (raw[2] << 7) | raw[3]


# ---------------------------------------------------------------- ID3

def parse_id3v2(data, meta):
    """Read an ID3v2 tag that starts at data[0]. Returns the tag size in bytes."""
    if len(data) < 10 or not data.startswith(b"ID3"):
        return 0
    major, flags = data[3], data[5]
    tag_size = _syncsafe(data[6:10])
    meta.add("technical", "ID3 version", f"2.{major}")
    body = data[10:10 + tag_size]
    if flags & 0x80 and major < 4:
        body = _unsynchronise(body)
    position = 0
    if flags & 0x40 and major >= 3 and len(body) >= 4:   # skip the extended header
        position = _syncsafe(body[:4]) if major == 4 else struct.unpack_from(">I", body)[0] + 4

    id_length, header_length = (3, 6) if major == 2 else (4, 10)
    for _ in range(MAX_FRAMES):
        if position + header_length > len(body):
            break
        frame_id = body[position:position + id_length]
        if not frame_id.isalnum():           # reached the padding
            break
        if major == 2:
            size = int.from_bytes(body[position + 3:position + 6], "big")
            frame_flags = 0
        else:
            size_bytes = body[position + 4:position + 8]
            size = _syncsafe(size_bytes) if major == 4 else struct.unpack(">I", size_bytes)[0]
            frame_flags = struct.unpack_from(">H", body, position + 8)[0]
        frame = body[position + header_length:position + header_length + size]
        position += header_length + size

        name = frame_id.decode("latin-1")
        name = ID3V22_IDS.get(name, name) if major == 2 else name
        if major == 4:
            if frame_flags & 0x000C:          # compressed or encrypted
                continue
            if frame_flags & 0x0002:
                frame = _unsynchronise(frame)
            if frame_flags & 0x0001:
                frame = frame[4:]
        elif major == 3 and frame_flags & 0x00C0:
            continue
        _handle_id3_frame(name, frame, meta, major)
    return 10 + tag_size


def _handle_id3_frame(name, frame, meta, major):
    if not frame:
        return
    encoding = frame[0]
    if name == "TXXX":
        description, value = _split_terminated(frame[1:], encoding)
        meta.add("hidden", f"User text: {_id3_text(description, encoding)}", _id3_text(value, encoding))
    elif name.startswith("T"):
        label, category = ID3_FRAMES.get(name, (f"ID3 {name}", "hidden"))
        meta.add(category, label, _id3_text(frame[1:], encoding))
    elif name in ("COMM", "USLT"):
        description, text = _split_terminated(frame[4:], encoding)
        description = _id3_text(description, encoding)
        label = "Comment" if name == "COMM" else "Lyrics"
        meta.add("hidden", f"{label} ({description})" if description else label, _id3_text(text, encoding))
    elif name == "APIC":
        if major == 2:
            rest = frame[4:]
        else:
            _mime, _, rest = frame[1:].partition(b"\x00")
        _description, image = _split_terminated(rest[1:], encoding)
        meta.add("hidden", "Cover art", f"{len(image)} bytes")
        from . import extract_nested   # imported here to avoid a circular import
        extract_nested(image, meta, "Cover art ")
    elif name == "WXXX":
        _description, url = _split_terminated(frame[1:], encoding)
        meta.add("hidden", "URL", url.decode("latin-1"))
    elif name.startswith("W"):
        meta.add("hidden", f"URL ({name})", frame.decode("latin-1"))
    elif name in ("PRIV", "UFID"):
        owner, _, payload = frame.partition(b"\x00")
        meta.add("hidden", f"{name} frame", f"{owner.decode('latin-1')} ({len(payload)} bytes)")
    elif name == "GEOB":
        _mime, _, rest = frame[1:].partition(b"\x00")
        filename, _ = _split_terminated(rest, encoding)
        meta.add("hidden", "Embedded object", _id3_text(filename, encoding) or "(no name)")
    elif name == "POPM":
        email, _, _ = frame.partition(b"\x00")
        meta.add("people", "Rated by", email.decode("latin-1"))


def parse_id3v1(data, meta):
    tag = data[-128:]
    if len(data) < 128 or not tag.startswith(b"TAG"):
        return
    for label, category, start, length in (("Title", "document", 3, 30), ("Artist", "people", 33, 30),
                                           ("Album", "document", 63, 30), ("Year", "time", 93, 4),
                                           ("Comment", "hidden", 97, 30)):
        value = decode_text(tag[start:start + length].split(b"\x00", 1)[0]).strip()
        meta.add(category, f"ID3v1 {label}", value)


# ---------------------------------------------------------------- MP3 audio frames

MPEG_VERSIONS = {0: "2.5", 2: "2", 3: "1"}
LAYER3_BITRATES = {"1": (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
                   "2": (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)}
SAMPLE_RATES = {"1": (44100, 48000, 32000), "2": (22050, 24000, 16000), "2.5": (11025, 12000, 8000)}
CHANNEL_MODES = ("stereo", "joint stereo", "dual channel", "mono")


def _read_mpeg_header(data, start, meta):
    """Find the first MPEG Layer III frame header after the ID3 tag."""
    position = data.find(b"\xff", start)
    for _ in range(4096):
        if position < 0 or position + 4 > len(data):
            return
        second, third = data[position + 1], data[position + 2]
        version = MPEG_VERSIONS.get((second >> 3) & 3)
        is_layer3 = (second & 0xE0) == 0xE0 and (second >> 1) & 3 == 1
        bitrate_index, rate_index = third >> 4, (third >> 2) & 3
        if version and is_layer3 and bitrate_index not in (0, 15) and rate_index != 3:
            table = LAYER3_BITRATES["1" if version == "1" else "2"]
            meta.add("technical", "Audio", f"MPEG-{version} Layer III")
            meta.add("technical", "Bitrate (first frame)", f"{table[bitrate_index]} kbps")
            meta.add("technical", "Sample rate", f"{SAMPLE_RATES[version][rate_index]} Hz")
            meta.add("technical", "Channels", CHANNEL_MODES[data[position + 3] >> 6])
            header_area = data[position:position + 200]
            if b"Xing" in header_area:
                meta.add("technical", "Bitrate mode", "variable (Xing header)")
            lame_at = header_area.find(b"LAME")
            if lame_at >= 0:
                meta.add("software", "Encoder", header_area[lame_at:lame_at + 9].decode("latin-1").strip("\x00 "))
            return
        position = data.find(b"\xff", position + 1)


def extract_mp3(data, meta):
    tag_size = parse_id3v2(data, meta) if data.startswith(b"ID3") else 0
    _read_mpeg_header(data, tag_size, meta)
    parse_id3v1(data, meta)


# ---------------------------------------------------------------- Vorbis comments

def parse_vorbis_comment(data, meta):
    vendor_length = struct.unpack_from("<I", data, 0)[0]
    meta.add("software", "Encoder library", decode_text(data[4:4 + vendor_length]))
    position = 4 + vendor_length
    count = struct.unpack_from("<I", data, position)[0]
    position += 4
    for _ in range(min(count, 1000)):
        if position + 4 > len(data):
            break
        length = struct.unpack_from("<I", data, position)[0]
        comment = decode_text(data[position + 4:position + 4 + length])
        position += 4 + length
        key, _, value = comment.partition("=")
        key = key.upper()
        if key == "METADATA_BLOCK_PICTURE":
            _read_flac_picture(base64.b64decode(value + "=="), meta)
            continue
        label, category = VORBIS_FIELDS.get(key, (f"Tag {key}", "hidden"))
        meta.add(category, label, value)


def _read_flac_picture(block, meta):
    mime_length = struct.unpack_from(">I", block, 4)[0]
    position = 8 + mime_length
    description_length = struct.unpack_from(">I", block, position)[0]
    position += 4 + description_length + 16
    image_length = struct.unpack_from(">I", block, position)[0]
    image = block[position + 4:position + 4 + image_length]
    meta.add("hidden", "Cover art", f"{len(image)} bytes")
    from . import extract_nested   # imported here to avoid a circular import
    extract_nested(image, meta, "Cover art ")


# ---------------------------------------------------------------- FLAC and Ogg

def extract_flac(data, meta):
    position = 4
    for _ in range(256):
        if position + 4 > len(data):
            break
        header = data[position]
        length = int.from_bytes(data[position + 1:position + 4], "big")
        block = data[position + 4:position + 4 + length]
        block_type = header & 0x7F
        if block_type == 0 and len(block) >= 18:
            packed = int.from_bytes(block[10:18], "big")
            sample_rate = packed >> 44
            total_samples = packed & 0xFFFFFFFFF
            meta.add("technical", "Sample rate", f"{sample_rate} Hz")
            meta.add("technical", "Channels", ((packed >> 41) & 7) + 1)
            meta.add("technical", "Bits per sample", ((packed >> 36) & 0x1F) + 1)
            if sample_rate:
                meta.add("technical", "Duration", f"{total_samples / sample_rate:.1f} s")
        elif block_type == 4:
            parse_vorbis_comment(block, meta)
        elif block_type == 6:
            _read_flac_picture(block, meta)
        position += 4 + length
        if header & 0x80:   # last metadata block
            break


def _first_ogg_packets(data, wanted):
    """Reassemble the first few packets of the first logical stream."""
    packets, current, serial = [], bytearray(), None
    position = 0
    while position + 27 <= len(data) and len(packets) < wanted and data[position:position + 4] == b"OggS":
        page_serial = struct.unpack_from("<I", data, position + 14)[0]
        segment_count = data[position + 26]
        lacing = data[position + 27:position + 27 + segment_count]
        offset = position + 27 + segment_count
        if serial is None:
            serial = page_serial
        if page_serial == serial:
            for lace in lacing:
                current += data[offset:offset + lace]
                offset += lace
                if lace < 255:
                    packets.append(bytes(current))
                    current = bytearray()
        position += 27 + segment_count + sum(lacing)
    return packets


def extract_ogg(data, meta):
    packets = _first_ogg_packets(data, 3)
    if not packets:
        return
    head = packets[0]
    if head.startswith(b"\x01vorbis") and len(head) >= 16:
        meta.add("technical", "Codec", "Vorbis")
        meta.add("technical", "Channels", head[11])
        meta.add("technical", "Sample rate", f"{struct.unpack_from('<I', head, 12)[0]} Hz")
    elif head.startswith(b"OpusHead") and len(head) >= 16:
        meta.add("technical", "Codec", "Opus")
        meta.add("technical", "Channels", head[9])
        meta.add("technical", "Original sample rate", f"{struct.unpack_from('<I', head, 12)[0]} Hz")
    elif head.startswith(b"\x80theora"):
        meta.add("technical", "Codec", "Theora video")
    for packet in packets[1:]:
        for prefix in (b"\x03vorbis", b"OpusTags", b"\x81theora"):
            if packet.startswith(prefix):
                parse_vorbis_comment(packet[len(prefix):], meta)
