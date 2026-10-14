"""JPEG: walk the marker segments that sit in front of the compressed image data."""

import struct

from .exif import parse_exif
from .iptc import parse_photoshop_resources
from .icc import parse_icc
from .xmp import parse_xmp

EXIF_HEADER = b"Exif\x00\x00"
XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
EXTENDED_XMP_HEADER = b"http://ns.adobe.com/xmp/extension/\x00"
ICC_HEADER = b"ICC_PROFILE\x00"
PHOTOSHOP_HEADER = b"Photoshop 3.0\x00"

START_OF_SCAN = 0xDA
END_OF_IMAGE = 0xD9
# SOF markers carry the frame size; C4, C8 and CC share the range but mean something else.
START_OF_FRAME = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def extract(data, meta):
    icc_chunks = []
    position = 2
    scan_start = None
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            meta.warnings.append(f"Lost marker sync at byte {position}")
            break
        marker = data[position + 1]
        if marker == 0xFF:            # padding
            position += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            position += 2
            continue
        if marker == END_OF_IMAGE:
            break
        length = struct.unpack_from(">H", data, position + 2)[0]
        segment = data[position + 4:position + 2 + length]
        _handle_segment(marker, segment, meta, icc_chunks)
        position += 2 + length
        if marker == START_OF_SCAN:
            scan_start = position
            break

    if icc_chunks:
        icc_chunks.sort()
        parse_icc(b"".join(chunk for _, chunk in icc_chunks), meta)
    if scan_start is not None:
        end = data.find(b"\xff\xd9", scan_start)
        if end >= 0 and len(data) - (end + 2) > 16:
            trailing = len(data) - (end + 2)
            meta.add_deep("hidden", "Data after end of image", f"{trailing} bytes (appended file or second image)")

            meta.add_embedded("Data after end of image", "bin", data[end + 2:])


def _handle_segment(marker, segment, meta, icc_chunks):
    if marker == 0xE0 and segment.startswith(b"JFIF\x00") and len(segment) >= 12:
        units, x_density, y_density = struct.unpack_from(">BHH", segment, 7)
        unit_name = {1: "dpi", 2: "dots/cm"}.get(units, "(aspect ratio)")
        meta.add("technical", "JFIF version", f"{segment[5]}.{segment[6]:02d}")
        meta.add("technical", "Density", f"{x_density}x{y_density} {unit_name}")
    elif marker == 0xE1 and segment.startswith(EXIF_HEADER):
        parse_exif(segment[len(EXIF_HEADER):], meta)
    elif marker == 0xE1 and segment.startswith(XMP_HEADER):
        parse_xmp(segment[len(XMP_HEADER):], meta)
    elif marker == 0xE1 and segment.startswith(EXTENDED_XMP_HEADER):
        meta.add("hidden", "Extended XMP block", f"{len(segment)} bytes")
    elif marker == 0xE2 and segment.startswith(ICC_HEADER) and len(segment) > 14:
        icc_chunks.append((segment[12], segment[14:]))
    elif marker == 0xE2 and segment.startswith(b"MPF\x00"):
        meta.add("hidden", "Multi-picture data (MPF)", "file holds extra images, e.g. depth map or preview")
    elif marker == 0xEC and segment.startswith(b"Ducky"):
        meta.add("software", "Saved with", "Photoshop Save for Web (Ducky segment)")
    elif marker == 0xED and segment.startswith(PHOTOSHOP_HEADER):
        parse_photoshop_resources(segment[len(PHOTOSHOP_HEADER):], meta, parse_xmp)
    elif marker == 0xEE and segment.startswith(b"Adobe"):
        meta.add("software", "Saved with", "Adobe software (APP14 segment)")
    elif marker == 0xEB and (b"jumb" in segment[:32] or b"c2pa" in segment[:64]):
        meta.add("hidden", "Content Credentials (C2PA)", "signed provenance manifest present")
    elif marker == 0xFE:
        meta.add("hidden", "JPEG comment", segment)
    elif marker in START_OF_FRAME and len(segment) >= 6:
        bits, height, width, components = struct.unpack_from(">BHHB", segment, 0)
        meta.add("technical", "Dimensions", f"{width}x{height}")
        meta.add("technical", "Encoding", "progressive" if marker in (0xC2, 0xC6, 0xCA, 0xCE) else "baseline")
        meta.add("technical", "Colour components", components)
    elif 0xE0 <= marker <= 0xEF and segment[:1].isalpha():
        name = segment.split(b"\x00", 1)[0][:40].decode("latin-1")
        meta.add("hidden", f"APP{marker - 0xE0} segment", f"{name} ({len(segment)} bytes)")
