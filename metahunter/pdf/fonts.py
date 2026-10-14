"""Turning the bytes inside PDF text operators into characters and glyph widths."""

import re

from .objects import Name, Stream

HEX = re.compile(rb"<([0-9A-Fa-f]+)>")
CODESPACE_BLOCK = re.compile(rb"begincodespacerange(.*?)endcodespacerange", re.S)
CHAR_BLOCK = re.compile(rb"beginbfchar(.*?)endbfchar", re.S)
RANGE_BLOCK = re.compile(rb"beginbfrange(.*?)endbfrange", re.S)
RANGE_ENTRY = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]+>|\[[^\]]*\])")
MAX_MAPPINGS = 200_000
MAX_RANGE = 65_536

GLYPH_NAMES = {
    "space": " ", "exclam": "!", "quotedbl": "\"", "numbersign": "#", "dollar": "$", "percent": "%",
    "ampersand": "&", "quotesingle": "\x27", "quoteright": "\u2019", "quoteleft": "\u2018",
    "quotedblleft": "\u201c", "quotedblright": "\u201d", "parenleft": "(", "parenright": ")",
    "asterisk": "*", "plus": "+", "comma": ",", "hyphen": "-", "period": ".", "slash": "/",
    "colon": ":", "semicolon": ";", "less": "<", "equal": "=", "greater": ">", "question": "?",
    "at": "@", "bracketleft": "[", "backslash": "\x5c", "bracketright": "]", "underscore": "_",
    "bullet": "\u2022", "endash": "\u2013", "emdash": "\u2014", "fi": "fi", "fl": "fl",
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "nbspace": " ", "sterling": "\u00a3", "Euro": "\u20ac",
}


def glyph_to_text(name):
    base = str(name).split(".", 1)[0]
    if base in GLYPH_NAMES:
        return GLYPH_NAMES[base]
    if len(base) == 1:
        return base
    if base.startswith("uni") and len(base) >= 7:
        try:
            return chr(int(base[3:7], 16))
        except ValueError:
            pass
    return "\ufffd"


def _utf16(hex_digits):
    if len(hex_digits) % 4:
        hex_digits = hex_digits.rjust(len(hex_digits) + 4 - len(hex_digits) % 4, b"0")
    return bytes.fromhex(hex_digits.decode()).decode("utf-16-be", "replace")


class ToUnicodeMap:
    """A font ToUnicode CMap: character code bytes -> text."""

    def __init__(self, data):
        self.mapping = {}
        self.code_lengths = set()
        for block in CODESPACE_BLOCK.findall(data):
            for low in HEX.findall(block)[::2]:
                self.code_lengths.add(max(1, len(low) // 2))
        for block in CHAR_BLOCK.findall(data):
            codes = HEX.findall(block)
            for source, target in zip(codes[::2], codes[1::2]):
                self._add(bytes.fromhex(source.decode().rjust(len(source) + len(source) % 2, "0")), _utf16(target))
        for block in RANGE_BLOCK.findall(data):
            for low, high, target in RANGE_ENTRY.findall(block):
                self._add_range(low, high, target)
        if not self.code_lengths:
            self.code_lengths = {len(code) for code in self.mapping} or {1}

    def _add(self, code, text):
        if len(self.mapping) < MAX_MAPPINGS:
            self.mapping[code] = text

    def _add_range(self, low, high, target):
        first, last = int(low, 16), int(high, 16)
        if last < first or last - first > MAX_RANGE:
            return
        width = max(1, len(low) // 2)
        if target.startswith(b"["):
            for index, item in enumerate(HEX.findall(target)[:last - first + 1]):
                self._add((first + index).to_bytes(width, "big"), _utf16(item))
            return
        digits = target[1:-1]
        start = int(digits, 16)
        for index in range(last - first + 1):
            text_digits = format(start + index, "X").rjust(len(digits), "0").encode()
            self._add((first + index).to_bytes(width, "big"), _utf16(text_digits))


class PdfFont:
    """What we need from a font: how to decode its strings and how wide its glyphs are."""

    def __init__(self, document, font):
        resolve = document.resolve
        font = font if isinstance(font, dict) else {}
        self.name = str(resolve(font.get("BaseFont")) or "unknown")
        self.composite = font.get("Subtype") == "Type0"
        self.to_unicode = None
        cmap = resolve(font.get("ToUnicode"))
        if isinstance(cmap, Stream):
            cmap_data = document.stream_data(cmap)
            if cmap_data:
                self.to_unicode = ToUnicodeMap(cmap_data)
        self.widths = {}
        self.differences = {}
        self.codec = "cp1252"
        if self.composite:
            self.default_width = 1000
            descendants = resolve(font.get("DescendantFonts"))
            descendant = resolve(descendants[0]) if isinstance(descendants, list) and descendants else None
            if isinstance(descendant, dict):
                default = resolve(descendant.get("DW"))
                self.default_width = default if isinstance(default, (int, float)) else 1000
                self._read_cid_widths(resolve(descendant.get("W")), resolve)
        else:
            self.default_width = 600 if "Courier" in self.name else 500
            self._read_simple_widths(font, resolve)
            self._read_encoding(resolve(font.get("Encoding")), resolve)

    def _read_simple_widths(self, font, resolve):
        first = resolve(font.get("FirstChar"))
        widths = resolve(font.get("Widths"))
        if isinstance(first, int) and isinstance(widths, list):
            for index, width in enumerate(widths[:256]):
                width = resolve(width)
                if isinstance(width, (int, float)):
                    self.widths[first + index] = width
        descriptor = resolve(font.get("FontDescriptor"))
        if isinstance(descriptor, dict) and isinstance(descriptor.get("MissingWidth"), (int, float)):
            self.default_width = descriptor["MissingWidth"] or self.default_width

    def _read_encoding(self, encoding, resolve):
        base = encoding.get("BaseEncoding") if isinstance(encoding, dict) else encoding
        if base == "MacRomanEncoding":
            self.codec = "mac_roman"
        if isinstance(encoding, dict):
            differences = resolve(encoding.get("Differences"))
            code = 0
            for item in differences if isinstance(differences, list) else []:
                if isinstance(item, int):
                    code = item
                elif isinstance(item, Name):
                    self.differences[code] = glyph_to_text(item)
                    code += 1

    def _read_cid_widths(self, widths, resolve):
        """The W array mixes "first [w1 w2 ...]" and "first last w" entries."""
        if not isinstance(widths, list):
            return
        index = 0
        while index + 1 < len(widths) and len(self.widths) < MAX_MAPPINGS:
            first, following = resolve(widths[index]), resolve(widths[index + 1])
            if not isinstance(first, int):
                break
            if isinstance(following, list):
                for offset, width in enumerate(following):
                    width = resolve(width)
                    if isinstance(width, (int, float)):
                        self.widths[first + offset] = width
                index += 2
            elif isinstance(following, int) and index + 2 < len(widths):
                width = resolve(widths[index + 2])
                if isinstance(width, (int, float)):
                    for code in range(first, min(following, first + MAX_RANGE) + 1):
                        self.widths[code] = width
                index += 3
            else:
                break

    def _code_length(self, raw, position):
        if self.to_unicode:
            for length in sorted(self.to_unicode.code_lengths):
                if raw[position:position + length] in self.to_unicode.mapping:
                    return length
        return 2 if self.composite else 1

    def decode(self, raw):
        """Split string bytes into glyphs: [(text, width in 1/1000 em, is a single-byte space)]."""
        glyphs, position = [], 0
        while position < len(raw):
            length = self._code_length(raw, position)
            code_bytes = raw[position:position + length]
            position += length
            code = int.from_bytes(code_bytes, "big")
            if self.to_unicode and code_bytes in self.to_unicode.mapping:
                text = self.to_unicode.mapping[code_bytes]
            elif code in self.differences:
                text = self.differences[code]
            elif self.composite:
                text = "\ufffd"
            else:
                text = code_bytes.decode(self.codec, "replace")
            width = self.widths.get(code, self.default_width)
            glyphs.append((text, width, length == 1 and code == 32))
        return glyphs


FALLBACK_FONT_NAME = "(no font selected)"


def fallback_font(document):
    font = PdfFont(document, {})
    font.name = FALLBACK_FONT_NAME
    return font
