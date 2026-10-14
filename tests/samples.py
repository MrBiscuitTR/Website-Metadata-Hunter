"""Builders for small, hand-made test files. No binary fixtures needed in the repo."""

import struct
import zlib


def stream_object(data, extra=b""):
    return b"<< /Length %d %s >>\nstream\n" % (len(data), extra) + data + b"\nendstream"


def build_pdf(objects, root=1, info=None, previous=b""):
    """Serialise {number: body bytes} as a PDF. With `previous`, append an incremental update."""
    output = bytearray(previous or b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(output)
        output += b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n"
    xref_position = len(output)
    output += b"xref\n"
    for number in sorted(offsets):
        output += b"%d 1\n%010d 00000 n \n" % (number, offsets[number])
    trailer = b"/Size %d /Root %d 0 R" % (max(offsets) + 1, root)
    if previous:
        trailer += b" /Prev " + previous.rsplit(b"startxref", 1)[1].split()[0]

    if info:
        trailer += b" /Info %d 0 R" % info
    output += b"trailer\n<< " + trailer + b" >>\nstartxref\n%d\n%%%%EOF\n" % xref_position
    return bytes(output)


REDACTED_PAGE = (
    b"BT /F1 12 Tf 72 700 Td (Public line) Tj ET\n"
    b"BT /F1 12 Tf 72 680 Td (SECRET 1234) Tj ET\n"
    b"0 0 0 rg 70 676 100 16 re f\n"
    b"BT /F1 12 Tf 3 Tr 72 660 Td (Invisible words) Tj ET\n"
)


def badly_redacted_pdf():
    """One page: visible text, text under a black box, invisible text, and an earlier revision."""
    first = build_pdf({
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
           b"/Resources << /Font << /F1 5 0 R >> >> /Annots [7 0 R] >>",
        4: stream_object(zlib.compress(b"BT /F1 12 Tf 72 640 Td (Old draft text) Tj ET\n"), b"/Filter /FlateDecode"),
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        6: b"<< /Author (Jane Original) /Producer (Writer 1.0) /CreationDate (D:20200101120000Z) >>",
        7: b"<< /Type /Annot /Subtype /Redact /Rect [70 696 200 712] /T (Reviewer Bob) >>",
    }, info=6)
    return build_pdf({
        4: stream_object(zlib.compress(REDACTED_PAGE), b"/Filter /FlateDecode"),
        6: b"<< /Author (Jane Editor) /Producer (Redactor 2.0) /ModDate (D:20210101120000Z) >>",
    }, info=6, previous=first)


def jpeg_with_exif():
    """A tiny JPEG header carrying EXIF make/model, a GPS position and an artist."""
    entries_ifd0 = []
    data_area = bytearray()

    def ascii_entry(tag, text, base):
        raw = text.encode() + b"\x00"
        offset = base + len(data_area)
        data_area.extend(raw)
        return struct.pack("<HHII", tag, 2, len(raw), offset)

    # Layout: header(8) + IFD0 (2 + 5*12 + 4) + GPS IFD (2 + 4*12 + 4) + data
    ifd0_offset, ifd0_size = 8, 2 + 5 * 12 + 4
    gps_offset = ifd0_offset + ifd0_size
    gps_size = 2 + 4 * 12 + 4
    data_base = gps_offset + gps_size
    entries_ifd0.append(ascii_entry(0x010F, "Canon", data_base))
    entries_ifd0.append(ascii_entry(0x0110, "Canon EOS 5D", data_base))
    entries_ifd0.append(ascii_entry(0x0131, "GIMP 2.10", data_base))
    entries_ifd0.append(ascii_entry(0x013B, "Alice Photographer", data_base))
    entries_ifd0.append(struct.pack("<HHII", 0x8825, 4, 1, gps_offset))

    latitude_offset = data_base + len(data_area)
    data_area.extend(struct.pack("<6I", 48, 1, 51, 1, 2400, 100))     # 48 51 24.00
    longitude_offset = data_base + len(data_area)
    data_area.extend(struct.pack("<6I", 2, 1, 21, 1, 900, 100))       # 2 21 9.00
    gps_entries = [
        struct.pack("<HHI4s", 1, 2, 2, b"N\x00\x00\x00"),
        struct.pack("<HHII", 2, 5, 3, latitude_offset),
        struct.pack("<HHI4s", 3, 2, 2, b"E\x00\x00\x00"),
        struct.pack("<HHII", 4, 5, 3, longitude_offset),
    ]
    tiff = bytearray(b"II*\x00" + struct.pack("<I", ifd0_offset))
    tiff += struct.pack("<H", len(entries_ifd0)) + b"".join(entries_ifd0) + struct.pack("<I", 0)
    tiff += struct.pack("<H", len(gps_entries)) + b"".join(gps_entries) + struct.pack("<I", 0)
    tiff += data_area
    app1 = b"Exif\x00\x00" + bytes(tiff)
    frame = struct.pack(">BHHB", 8, 10, 20, 3) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    return (b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1
            + b"\xff\xc0" + struct.pack(">H", len(frame) + 2) + frame
            + b"\xff\xfe" + struct.pack(">H", 14) + b"hello comment"[:12]
            + b"\xff\xda\x00\x02" + b"\x00" * 8 + b"\xff\xd9")
