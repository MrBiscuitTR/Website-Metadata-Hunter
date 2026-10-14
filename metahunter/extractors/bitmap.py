"""BMP images and ICO / CUR icon files."""

import struct

from .metadata import Metadata
from . import png

BMP_COMPRESSION = {0: "none", 1: "RLE8", 2: "RLE4", 3: "bitfields", 4: "JPEG", 5: "PNG"}


def extract_bmp(data, meta):
    if len(data) < 26:
        return
    header_size = struct.unpack_from("<I", data, 14)[0]
    if header_size == 12:     # old OS/2 header
        width, height, _, bits = struct.unpack_from("<HHHH", data, 18)
        compression = 0
    else:
        width, height, _, bits, compression = struct.unpack_from("<iiHHI", data, 18)
    meta.add("technical", "Dimensions", f"{width}x{abs(height)}")
    meta.add("technical", "Bits per pixel", bits)
    meta.add("technical", "Compression", BMP_COMPRESSION.get(compression, compression))
    meta.add("technical", "Header version", {12: "OS/2", 40: "V3", 108: "V4", 124: "V5"}.get(header_size, header_size))
    if header_size >= 40 and len(data) >= 46:
        x_ppm, y_ppm = struct.unpack_from("<ii", data, 38)
        if x_ppm > 0:
            meta.add("technical", "Resolution", f"{round(x_ppm * 0.0254)}x{round(y_ppm * 0.0254)} dpi")


def extract_ico(data, meta):
    kind, count = struct.unpack_from("<HH", data, 2)
    meta.add("technical", "Icon type", "cursor" if kind == 2 else "icon")
    meta.add("technical", "Images in file", count)
    for index in range(min(count, 64)):
        entry = 6 + index * 16
        if entry + 16 > len(data):
            break
        width, height, _, _, _, bits, size, offset = struct.unpack_from("<BBBBHHII", data, entry)
        image = data[offset:offset + size]
        format_name = "PNG" if image.startswith(png.SIGNATURE) else "BMP"
        meta.add("technical", f"Image {index + 1}", f"{width or 256}x{height or 256}, {bits} bpp, {format_name}")
        if format_name == "PNG":
            # PNG frames can carry their own text chunks (software, author...).
            inner = Metadata("png")
            png.extract(image, inner)
            meta.merge(inner, f"Image {index + 1} ", skip_categories=("technical",))
