"""PDF object syntax: numbers, strings, names, arrays, dictionaries, references.

Used for both file-level objects and content-stream operands. Never trusts lengths
or nesting from the file: depth is capped and every loop advances.
"""

from collections import namedtuple

WHITESPACE = b"\x00\t\n\x0c\r "
DELIMITERS = b"()<>[]{}/%"
NUMBER_START = b"+-.0123456789"
MAX_NESTING = 64

BACKSLASH = 0x5C
LITERAL_ESCAPES = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12,
                   ord("("): 40, ord(")"): 41, BACKSLASH: BACKSLASH}

Ref = namedtuple("Ref", "number generation")


class PdfError(Exception):
    """The PDF is malformed in a way we cannot step around."""


class Name(str):
    """A PDF name such as /Type, stored without the slash."""


class Keyword(str):
    """A bare word: content-stream operators, obj, stream, R..."""


class Stream:
    """A stream object: its dictionary plus the still-encoded bytes."""

    def __init__(self, attributes, raw, number=None):
        self.attributes = attributes
        self.raw = raw
        self.number = number

    def get(self, key, default=None):
        return self.attributes.get(key, default)


class Parser:
    def __init__(self, data, position=0):
        self.data = data
        self.position = position

    def at_end(self):
        self.skip_whitespace()
        return self.position >= len(self.data)

    def skip_whitespace(self):
        data, length = self.data, len(self.data)
        while self.position < length:
            byte = data[self.position]
            if byte in WHITESPACE:
                self.position += 1
            elif byte == 0x25:                       # % comment runs to end of line
                while self.position < length and data[self.position] not in b"\r\n":
                    self.position += 1
            else:
                break

    def read_object(self, depth=0):
        if depth > MAX_NESTING:
            raise PdfError("objects nested too deeply")
        self.skip_whitespace()
        if self.position >= len(self.data):
            raise PdfError("unexpected end of data")
        byte = self.data[self.position]
        if byte == 0x2F:                              # /
            return self._read_name()
        if byte == 0x28:                              # (
            return self._read_literal_string()
        if byte == 0x3C:                              # <
            if self.data[self.position + 1:self.position + 2] == b"<":
                return self._read_dictionary(depth)
            return self._read_hex_string()
        if byte == 0x5B:                              # [
            return self._read_array(depth)
        if byte in NUMBER_START:
            return self._read_number_or_reference()
        return self._read_keyword()

    def _read_token(self):
        start = self.position
        data, length = self.data, len(self.data)
        while self.position < length and data[self.position] not in WHITESPACE and data[self.position] not in DELIMITERS:
            self.position += 1
        return data[start:self.position]

    def _read_name(self):
        self.position += 1
        raw = self._read_token()
        if b"#" in raw:                               # #xx hex escapes
            parts = raw.split(b"#")
            decoded = bytearray(parts[0])
            for part in parts[1:]:
                try:
                    decoded.append(int(part[:2], 16))
                    decoded += part[2:]
                except ValueError:
                    decoded += b"#" + part
            raw = bytes(decoded)
        return Name(raw.decode("latin-1"))

    def _read_literal_string(self):
        """(...) strings: balanced parentheses, backslash escapes, octal codes."""
        data, length = self.data, len(self.data)
        self.position += 1
        depth, output = 1, bytearray()
        while self.position < length:
            byte = data[self.position]
            self.position += 1
            if byte == BACKSLASH:
                if self.position >= length:
                    break
                escaped = data[self.position]
                self.position += 1
                if escaped in LITERAL_ESCAPES:
                    output.append(LITERAL_ESCAPES[escaped])
                elif 0x30 <= escaped <= 0x37:          # up to three octal digits
                    digits = bytes([escaped])
                    while len(digits) < 3 and self.position < length and 0x30 <= data[self.position] <= 0x37:
                        digits += data[self.position:self.position + 1]
                        self.position += 1
                    output.append(int(digits, 8) & 0xFF)
                elif escaped == 13:                    # line continuation
                    if data[self.position:self.position + 1] == b"\n":
                        self.position += 1
                elif escaped != 10:
                    output.append(escaped)
            elif byte == 0x28:
                depth += 1
                output.append(byte)
            elif byte == 0x29:
                depth -= 1
                if depth == 0:
                    break
                output.append(byte)
            else:
                output.append(byte)
        return bytes(output)

    def _read_hex_string(self):
        end = self.data.find(b">", self.position)
        if end < 0:
            end = len(self.data)
        digits = bytes(byte for byte in self.data[self.position + 1:end] if chr(byte) in "0123456789abcdefABCDEF")
        self.position = end + 1
        if len(digits) % 2:
            digits += b"0"
        return bytes.fromhex(digits.decode("ascii"))

    def _read_array(self, depth):
        self.position += 1
        items = []
        while True:
            self.skip_whitespace()
            if self.position >= len(self.data):
                raise PdfError("unterminated array")
            if self.data[self.position] == 0x5D:      # ]
                self.position += 1
                return items
            items.append(self.read_object(depth + 1))

    def _read_dictionary(self, depth):
        self.position += 2
        result = {}
        while True:
            self.skip_whitespace()
            if self.position >= len(self.data):
                raise PdfError("unterminated dictionary")
            if self.data.startswith(b">>", self.position):
                self.position += 2
                return result
            key = self.read_object(depth + 1)
            if self.data.startswith(b">>", self.position):
                return result
            value = self.read_object(depth + 1)
            if isinstance(key, Name):
                result[key] = value

    def _read_number_or_reference(self):
        token = self._read_token()
        try:
            number = int(token)
        except ValueError:
            try:
                return float(token)
            except ValueError:
                return Keyword(token.decode("latin-1"))
        # "12 0 R" is a reference; look ahead without committing.
        saved = self.position
        self.skip_whitespace()
        generation = self._read_token()
        if generation.isdigit():
            self.skip_whitespace()
            if self.data[self.position:self.position + 1] == b"R":
                after = self.data[self.position + 1:self.position + 2]
                if not after or after in WHITESPACE or after in DELIMITERS:
                    self.position += 1
                    return Ref(number, int(generation))
        self.position = saved
        return number

    def _read_keyword(self):
        token = self._read_token()
        if not token:                                 # stray delimiter such as ) or }
            token = self.data[self.position:self.position + 1]
            self.position += 1
        word = token.decode("latin-1")
        if word == "true":
            return True
        if word == "false":
            return False
        if word == "null":
            return None
        return Keyword(word)
