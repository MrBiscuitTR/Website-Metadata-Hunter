"""PdfDocument: indexes every object in the file, including superseded ones.

Instead of trusting the cross-reference table we scan the whole file for "N G obj".
That is slower, but it also finds objects left over from earlier saves, which is
exactly where badly redacted content tends to survive.
"""

import re

from .filters import decode_stream
from .objects import Parser, PdfError, Ref, Stream

OBJECT_HEADER = re.compile(rb"(\d{1,10})\s+(\d{1,5})\s+obj\b")
TRAILER = re.compile(rb"trailer\s*<<")
VERSION = re.compile(rb"%PDF-(\d\.\d)")
MAX_RESOLVE_HOPS = 32
MAX_PAGES = 10_000
MAX_OBJECTS_PER_STREAM = 100_000


class PdfDocument:
    def __init__(self, data):
        self.data = data
        self.warnings = []
        header = VERSION.search(data[:1024])
        self.version = header.group(1).decode() if header else None
        # object number -> [(sort key, kind, location)] in file order
        self._definitions = {}
        self._cache = {}
        self._object_streams = {}
        self._scan_objects()
        self.trailers = self._read_trailers()
        self.root = self._last_trailer_value("Root")
        self.info = self._last_trailer_value("Info")
        self.encrypted = any("Encrypt" in trailer for trailer in self.trailers)

    # ------------------------------------------------------------ indexing

    def _scan_objects(self):
        for match in OBJECT_HEADER.finditer(self.data):
            number = int(match.group(1))
            self._definitions.setdefault(number, []).append(((match.start(), 0), "direct", match.end()))
        for number, definitions in list(self._definitions.items()):
            for (position, _), kind, body_start in list(definitions):
                if kind == "direct" and b"/ObjStm" in self.data[body_start:body_start + 400]:
                    self._index_object_stream(number, position, body_start)
        for definitions in self._definitions.values():
            definitions.sort(key=lambda definition: definition[0])

    def _index_object_stream(self, number, position, body_start):
        """Register the objects packed inside an /ObjStm container."""
        try:
            container = self._parse_direct(body_start, number)
            if not isinstance(container, Stream):
                return
            decoded = decode_stream(container, self.resolve)
            first = int(container.get("First", 0))
            count = min(int(container.get("N", 0)), MAX_OBJECTS_PER_STREAM)
            header = Parser(decoded or b"")
            for index in range(count):
                packed_number, offset = header.read_object(), header.read_object()
                if isinstance(packed_number, int) and isinstance(offset, int):
                    self._definitions.setdefault(packed_number, []).append(
                        ((position, index + 1), "packed", (position, first + offset)))
            self._object_streams[position] = decoded
        except (PdfError, ValueError, TypeError) as error:
            self.warnings.append(f"Unreadable object stream {number}: {error}")

    def _read_trailers(self):
        found = []
        for match in TRAILER.finditer(self.data):
            try:
                trailer = Parser(self.data, match.end() - 2).read_object()
                if isinstance(trailer, dict):
                    found.append((match.start(), trailer))
            except PdfError:
                continue
        # PDF 1.5+ files keep the trailer keys in cross-reference streams instead.
        for number, definitions in self._definitions.items():
            for (position, _), kind, location in definitions:
                if kind == "direct" and b"/XRef" in self.data[location:location + 300]:
                    stream = self._load(number, ((position, 0), kind, location))
                    if isinstance(stream, Stream):
                        found.append((position, stream.attributes))
        return [trailer for _, trailer in sorted(found, key=lambda item: item[0])]

    def _last_trailer_value(self, key):
        for trailer in reversed(self.trailers):
            if key in trailer:
                return trailer[key]
        return None

    # ------------------------------------------------------------ loading objects

    def _parse_direct(self, body_start, number):
        parser = Parser(self.data, body_start)
        value = parser.read_object()
        if isinstance(value, dict):
            parser.skip_whitespace()
            if self.data.startswith(b"stream", parser.position):
                return Stream(value, self._stream_bytes(value, parser.position + 6), number)
        return value

    def _stream_bytes(self, attributes, position):
        if self.data[position:position + 2] == b"\r\n":
            position += 2
        elif self.data[position:position + 1] in (b"\n", b"\r"):
            position += 1
        length = attributes.get("Length")
        if isinstance(length, Ref):
            length = self._direct_integer(length.number)
        if isinstance(length, int) and 0 <= length <= len(self.data) - position:
            end = position + length
            if self.data[end:end + 32].lstrip().startswith(b"endstream"):
                return self.data[position:end]
        # Length missing or wrong: fall back to the endstream keyword.
        end = self.data.find(b"endstream", position)
        raw = self.data[position:end if end >= 0 else len(self.data)]
        for line_end in (b"\r\n", b"\n", b"\r"):
            if raw.endswith(line_end):
                return raw[:-len(line_end)]
        return raw

    def _direct_integer(self, number):
        """Stream lengths are sometimes stored as separate integer objects."""
        for _, kind, location in reversed(self._definitions.get(number, [])):
            if kind == "direct":
                try:
                    value = Parser(self.data, location).read_object()
                except PdfError:
                    return None
                return value if isinstance(value, int) else None
        return None

    def _load(self, number, definition):
        key = (number, definition[0])
        if key not in self._cache:
            _, kind, location = definition
            try:
                if kind == "direct":
                    value = self._parse_direct(location, number)
                else:
                    container, offset = location
                    value = Parser(self._object_streams.get(container) or b"", offset).read_object()
            except PdfError as error:
                self.warnings.append(f"Unreadable object {number}: {error}")
                value = None
            self._cache[key] = value
        return self._cache[key]

    def get(self, number):
        """The current (last-defined) version of an object."""
        definitions = self._definitions.get(number)
        return self._load(number, definitions[-1]) if definitions else None

    def all_versions(self, number):
        """Every definition of an object number, oldest first."""
        return [self._load(number, definition) for definition in self._definitions.get(number, [])]

    def object_numbers(self):
        return sorted(self._definitions)

    def resolve(self, value):
        for _ in range(MAX_RESOLVE_HOPS):
            if not isinstance(value, Ref):
                return value
            value = self.get(value.number)
        return None

    def stream_data(self, stream):
        """Decoded stream bytes, or None for image data / undecodable streams."""
        if not isinstance(stream, Stream):
            return None
        try:
            return decode_stream(stream, self.resolve)
        except (PdfError, ValueError) as error:
            self.warnings.append(f"Could not decode stream {stream.number}: {error}")
            return None

    # ------------------------------------------------------------ structure

    def catalog(self):
        catalog = self.resolve(self.root)
        if isinstance(catalog, dict):
            return catalog
        for number in self.object_numbers():
            candidate = self.get(number)
            if isinstance(candidate, dict) and candidate.get("Type") == "Catalog":
                return candidate
        return {}

    def pages(self):
        """Page dictionaries in reading order, with inherited attributes filled in."""
        result = []
        self._collect_pages(self.catalog().get("Pages"), {}, result, set(), 0)
        if not result:   # broken page tree: fall back to anything that calls itself a page
            for number in self.object_numbers():
                candidate = self.get(number)
                if isinstance(candidate, dict) and candidate.get("Type") == "Page":
                    result.append(candidate)
        return result[:MAX_PAGES]

    def _collect_pages(self, node_ref, inherited, result, seen, depth):
        if depth > 64 or len(result) >= MAX_PAGES:
            return
        if isinstance(node_ref, Ref):
            if node_ref.number in seen:
                return
            seen.add(node_ref.number)
        node = self.resolve(node_ref)
        if not isinstance(node, dict):
            return
        inherited = dict(inherited)
        for key in ("Resources", "MediaBox", "CropBox", "Rotate"):
            if key in node:
                inherited[key] = node[key]
        kids = self.resolve(node.get("Kids"))
        if node.get("Type") != "Page" and isinstance(kids, list):
            for kid in kids:
                self._collect_pages(kid, inherited, result, seen, depth + 1)
        else:
            page = dict(inherited)
            page.update(node)
            result.append(page)

    def revision_ends(self):
        """Byte offsets where each saved revision ends (just after its %%EOF)."""
        ends = [match.end() for match in re.finditer(rb"%%EOF[\r\n]{0,2}", self.data)]
        if len(ends) > 1 and b"/Linearized" in self.data[:1024]:
            ends = ends[1:]          # linearised files have an early %%EOF that is not a revision
        if not ends or len(self.data) - ends[-1] > 32:
            ends.append(len(self.data))
        return ends
