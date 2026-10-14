"""Stream decoding: Flate, LZW, ASCII85, ASCIIHex, RunLength, plus PNG predictors.

Image codecs (DCT, JPX, JBIG2, CCITT) are left alone: we never need their pixels.
Every decoder stops at MAX_DECODED_SIZE so a crafted stream cannot exhaust memory.
"""

import base64
import re
import zlib

from .objects import Name, PdfError

MAX_DECODED_SIZE = 64 * 1024 * 1024
IMAGE_FILTERS = {"DCTDecode", "DCT", "JPXDecode", "JBIG2Decode", "CCITTFaxDecode", "CCF"}
WHITESPACE = re.compile(rb"\s+")


def _as_list(value):
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def flate(data):
    """Inflate, salvaging whatever decodes before any corruption."""
    decompressor = zlib.decompressobj()
    output = bytearray()
    for start in range(0, len(data), 65536):
        try:
            output += decompressor.decompress(data[start:start + 65536], MAX_DECODED_SIZE - len(output))
        except zlib.error:
            break
        if decompressor.unconsumed_tail or len(output) >= MAX_DECODED_SIZE:
            raise PdfError("decoded stream is too large")
        if decompressor.eof:
            break
    return bytes(output)


def lzw(data, early_change=1):
    output = bytearray()
    table = [bytes([value]) for value in range(256)] + [b"", b""]
    code_length, buffer, buffered_bits, previous = 9, 0, 0, None
    for byte in data:
        buffer = (buffer << 8) | byte
        buffered_bits += 8
        while buffered_bits >= code_length:
            buffered_bits -= code_length
            code = (buffer >> buffered_bits) & ((1 << code_length) - 1)
            buffer &= (1 << buffered_bits) - 1
            if code == 256:                            # clear table
                table, code_length, previous = table[:258], 9, None
                continue
            if code == 257:                            # end of data
                return bytes(output)
            if code < len(table):
                entry = table[code]
                if previous is not None:
                    table.append(previous + entry[:1])
            elif code == len(table) and previous is not None:
                entry = previous + previous[:1]
                table.append(entry)
            else:
                raise PdfError("corrupt LZW data")
            output += entry
            previous = entry
            if len(table) + early_change >= (1 << code_length) and code_length < 12:
                code_length += 1
            if len(output) > MAX_DECODED_SIZE:
                raise PdfError("decoded stream is too large")
    return bytes(output)


def ascii85(data):
    data = WHITESPACE.sub(b"", data)
    if data.startswith(b"<~"):
        data = data[2:]
    end = data.find(b"~>")
    if end >= 0:
        data = data[:end]
    return base64.a85decode(data)


def ascii_hex(data):
    data = WHITESPACE.sub(b"", data).split(b">", 1)[0]
    if len(data) % 2:
        data += b"0"
    return bytes.fromhex(data.decode("ascii", "ignore"))


def run_length(data):
    output, position = bytearray(), 0
    while position < len(data):
        length = data[position]
        position += 1
        if length == 128:
            break
        if length < 128:
            output += data[position:position + length + 1]
            position += length + 1
        else:
            output += data[position:position + 1] * (257 - length)
            position += 1
        if len(output) > MAX_DECODED_SIZE:
            raise PdfError("decoded stream is too large")
    return bytes(output)


def undo_png_predictor(data, columns, colours, bits):
    """Reverse the per-row PNG filters some producers apply before compressing."""
    pixel_bytes = max(1, colours * bits // 8)
    row_length = (columns * colours * bits + 7) // 8
    output, previous = bytearray(), bytearray(row_length)
    for start in range(0, len(data) - row_length, row_length + 1):
        kind = data[start]
        row = bytearray(data[start + 1:start + 1 + row_length])
        for index in range(row_length):
            left = row[index - pixel_bytes] if index >= pixel_bytes else 0
            up = previous[index]
            up_left = previous[index - pixel_bytes] if index >= pixel_bytes else 0
            if kind == 1:
                row[index] = (row[index] + left) & 0xFF
            elif kind == 2:
                row[index] = (row[index] + up) & 0xFF
            elif kind == 3:
                row[index] = (row[index] + (left + up) // 2) & 0xFF
            elif kind == 4:
                estimate = left + up - up_left
                distances = (abs(estimate - left), abs(estimate - up), abs(estimate - up_left))
                nearest = (left, up, up_left)[distances.index(min(distances))]
                row[index] = (row[index] + nearest) & 0xFF
        output += row
        previous = row
    return bytes(output)


def decode_stream(stream, resolve):
    """Decoded bytes of a stream, or None if it is image data we do not decode."""
    filters = [resolve(item) for item in _as_list(resolve(stream.get("Filter")))]
    parameters = [resolve(item) for item in _as_list(resolve(stream.get("DecodeParms")))]
    data = stream.raw
    for index, name in enumerate(filters):
        settings = parameters[index] if index < len(parameters) and isinstance(parameters[index], dict) else {}
        if name in ("FlateDecode", "Fl"):
            data = flate(data)
        elif name in ("LZWDecode", "LZW"):
            data = lzw(data, settings.get("EarlyChange", 1))
        elif name in ("ASCII85Decode", "A85"):
            data = ascii85(data)
        elif name in ("ASCIIHexDecode", "AHx"):
            data = ascii_hex(data)
        elif name in ("RunLengthDecode", "RL"):
            data = run_length(data)
        elif name in IMAGE_FILTERS:
            return None
        elif name == "Crypt" or isinstance(name, Name):
            raise PdfError(f"unsupported filter {name}")
        if name in ("FlateDecode", "Fl", "LZWDecode", "LZW") and settings.get("Predictor", 1) >= 10:
            data = undo_png_predictor(data, settings.get("Columns", 1), settings.get("Colors", 1),
                                      settings.get("BitsPerComponent", 8))
    return data
