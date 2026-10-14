"""PNG: a signature followed by length-prefixed chunks."""

import struct

from .binary import inflate
from .exif import parse_exif
from .icc import parse_icc
from .xmp import parse_xmp


SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_CHUNKS = 10_000

# Text chunk keywords from the PNG spec and common tools: (label, category)
TEXT_KEYWORDS = {
    "Title": ("Title", "document"),
    "Author": ("Author", "people"),
    "Description": ("Description", "document"),
    "Copyright": ("Copyright", "people"),
    "Creation Time": ("Created", "time"),
    "Software": ("Software", "software"),
    "Disclaimer": ("Disclaimer", "document"),
    "Warning": ("Warning", "document"),
    "Source": ("Source device", "device"),
    "Comment": ("Comment", "hidden"),
    "date:create": ("Created (ImageMagick)", "time"),
    "date:modify": ("Modified (ImageMagick)", "time"),
    "date:timestamp": ("Timestamp (ImageMagick)", "time"),
    "parameters": ("AI generation parameters", "software"),
    "prompt": ("AI generation prompt (ComfyUI)", "software"),
    "workflow": ("AI workflow (ComfyUI)", "software"),
}
COLOUR_TYPES = {0: "greyscale", 2: "RGB", 3: "palette", 4: "greyscale + alpha", 6: "RGBA"}


def extract(data, meta):
    position = len(SIGNATURE)
    for _ in range(MAX_CHUNKS):
        if position + 8 > len(data):
            break
        length, chunk_type = struct.unpack_from(">I4s", data, position)
        body = data[position + 8:position + 8 + length]
        _handle_chunk(chunk_type, body, meta)
        position += 12 + length
        if chunk_type == b"IEND":
            break
    trailing = len(data) - position
    if trailing > 16:
        meta.add_deep("hidden", "Data after end of image", f"{trailing} bytes (appended file?)")
        meta.add_embedded("Data after end of image", "bin", data[position:])


def _handle_chunk(chunk_type, body, meta):
    if chunk_type == b"IHDR" and len(body) >= 13:
        width, height, bit_depth, colour_type, _, _, interlace = struct.unpack_from(">IIBBBBB", body)
        meta.add("technical", "Dimensions", f"{width}x{height}")
        meta.add("technical", "Colour", f"{COLOUR_TYPES.get(colour_type, colour_type)}, {bit_depth}-bit")
        if interlace:
            meta.add("technical", "Interlaced", "yes (Adam7)")
    elif chunk_type in (b"tEXt", b"zTXt", b"iTXt"):
        keyword, text = _read_text_chunk(chunk_type, body, meta)
        _report_text(keyword, text, meta)
    elif chunk_type == b"eXIf":
        parse_exif(body, meta)
    elif chunk_type == b"tIME" and len(body) >= 7:
        year, month, day, hour, minute, second = struct.unpack_from(">HBBBBB", body)
        meta.add("time", "Last modified (tIME)", f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d} UTC")
    elif chunk_type == b"pHYs" and len(body) >= 9:
        x_density, y_density, unit = struct.unpack_from(">IIB", body)
        if unit == 1:
            meta.add("technical", "Resolution", f"{round(x_density * 0.0254)}x{round(y_density * 0.0254)} dpi")
    elif chunk_type == b"iCCP":
        name, _, rest = body.partition(b"\x00")
        meta.add("technical", "ICC profile name", name.decode("latin-1"))
        try:
            parse_icc(inflate(rest[1:]), meta)
        except Exception as error:  # zlib.error, TooLarge or a truncated profile
            meta.warnings.append(f"Unreadable ICC profile: {error}")
    elif chunk_type == b"acTL" and len(body) >= 8:
        meta.add("technical", "Animated (APNG) frames", struct.unpack_from(">I", body)[0])
    elif chunk_type == b"caBX":
        meta.add("hidden", "Content Credentials (C2PA)", "signed provenance manifest present")


def _read_text_chunk(chunk_type, body, meta):
    keyword, _, rest = body.partition(b"\x00")
    keyword = keyword.decode("latin-1")
    try:
        if chunk_type == b"tEXt":
            return keyword, rest.decode("latin-1")
        if chunk_type == b"zTXt":
            return keyword, inflate(rest[1:]).decode("latin-1")
        # iTXt: compression flag, method, language tag, translated keyword, text
        compressed = rest[:1] == b"\x01"
        _language, _, rest = rest[2:].partition(b"\x00")
        _translated, _, text = rest.partition(b"\x00")
        if compressed:
            text = inflate(text)
        return keyword, text.decode("utf-8", "replace")
    except Exception as error:  # damaged compressed text should not stop the rest of the file
        meta.warnings.append(f"Unreadable {chunk_type.decode()} chunk {keyword!r}: {error}")
        return keyword, ""


def _report_text(keyword, text, meta):
    if not text:
        return
    if keyword == "XML:com.adobe.xmp":
        parse_xmp(text, meta)
    elif keyword in ("Raw profile type exif", "Raw profile type APP1"):
        _read_imagemagick_exif(text, meta)
    elif keyword in TEXT_KEYWORDS:
        label, category = TEXT_KEYWORDS[keyword]
        meta.add(category, label, text)
    else:
        meta.add("hidden", f"Text chunk: {keyword}", text)


def _read_imagemagick_exif(text, meta):
    """ImageMagick stores EXIF as hex text: a name line, a length line, then hex digits."""
    lines = text.strip().split("\n", 2)
    if len(lines) < 3:
        return
    raw = bytes.fromhex("".join(lines[2].split()))
    if raw.startswith(b"Exif\x00\x00"):
        raw = raw[6:]
    parse_exif(raw, meta)
