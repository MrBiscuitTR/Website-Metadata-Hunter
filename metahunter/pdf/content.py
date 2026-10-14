"""Content-stream interpreter: where every piece of text and every filled shape lands.

We do not draw anything. We only track enough of the graphics state to know each
glyph's position on the page and the order things were painted in, which is what
the redaction checker needs to tell whether a box was painted over some text.
"""

import dataclasses
import math
import re

from .fonts import PdfFont, fallback_font
from .objects import Keyword, Parser, PdfError, Ref, Stream

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
MAX_FORM_DEPTH = 8
MAX_OPERATIONS_PER_DOCUMENT = 3_000_000
MAX_STATE_STACK = 256
INLINE_IMAGE_END = re.compile(rb"\sEI(?=\s|$)")
FILL_OPERATORS = {"f", "F", "f*", "B", "B*", "b", "b*"}
OPAQUE = 0.9          # fill opacity from which paint is treated as hiding what is underneath


def multiply(first, second):
    a1, b1, c1, d1, e1, f1 = first
    a2, b2, c2, d2, e2, f2 = second
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2, c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)


def apply(matrix, x, y):
    return (matrix[0] * x + matrix[2] * y + matrix[4], matrix[1] * x + matrix[3] * y + matrix[5])


def translate(x, y):
    return (1.0, 0.0, 0.0, 1.0, x, y)


@dataclasses.dataclass
class TextRun:
    """One text-showing operation. `chars` holds (text, centre x, centre y, half width) per glyph."""
    page: int
    order: int
    text: str
    x: float
    y: float
    end_x: float
    size: float
    font: str
    render_mode: int
    fill: tuple
    chars: list


@dataclasses.dataclass
class PaintedBox:
    """A filled shape or an image, as a page-space bounding box (x0, y0, x1, y1).

    `opaque` is False for see-through paint: zero or low fill opacity, or images with a
    transparency mask (signature scans, logos). Those cannot hide what is under them.
    """
    page: int
    order: int
    bbox: tuple
    kind: str
    fill: tuple
    opaque: bool = True


@dataclasses.dataclass
class GraphicsState:
    ctm: tuple = IDENTITY
    fill: tuple = (0.0, 0.0, 0.0)
    font: object = None
    font_size: float = 0.0
    char_spacing: float = 0.0
    word_spacing: float = 0.0
    horizontal_scale: float = 1.0
    leading: float = 0.0
    rise: float = 0.0
    render_mode: int = 0
    fill_alpha: float = 1.0
    clip: tuple = None          # bounding box of the clipping area; None means the whole page

    def copy(self):
        return dataclasses.replace(self)


def _numbers(operands, count):
    values = operands[-count:] if len(operands) >= count else None
    if values is None or not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
        return None
    return [float(value) for value in values]


def _colour_from(values):
    """Approximate RGB for gray, RGB or CMYK operands; None for patterns and the like."""
    if values is None:
        return None
    if len(values) == 1:
        return (values[0],) * 3
    if len(values) == 3:
        return tuple(values)
    if len(values) == 4:
        cyan, magenta, yellow, black = values
        return ((1 - cyan) * (1 - black), (1 - magenta) * (1 - black), (1 - yellow) * (1 - black))
    return None


def _bounding_box(points):
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _intersect(box, clip):
    """The visible part of a box inside the clipping area, or None if nothing is visible."""
    if clip is None:
        return box
    x0, y0 = max(box[0], clip[0]), max(box[1], clip[1])
    x1, y1 = min(box[2], clip[2]), min(box[3], clip[3])
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


class DocumentInterpreter:
    """Runs the content streams of every page and collects text runs and painted boxes."""

    def __init__(self, document):
        self.document = document
        self.runs = []
        self.boxes = []
        self.referenced_streams = set()   # object numbers we executed as page content or forms
        self.operations = 0
        self._fonts = {}
        self._fallback_font = fallback_font(document)
        self._order = 0
        self._page = 0

    def run(self, pages):
        for page_number, page in enumerate(pages, start=1):
            if self.operations > MAX_OPERATIONS_PER_DOCUMENT:
                self.document.warnings.append("Stopped interpreting: too many drawing operations")
                break
            self._page = page_number
            resolve = self.document.resolve
            contents = page.get("Contents")
            parts = resolve(contents)
            parts = parts if isinstance(parts, list) else [contents]
            chunks = []
            for part in parts:
                if isinstance(part, Ref):
                    self.referenced_streams.add(part.number)
                data = self.document.stream_data(resolve(part))
                if data:
                    chunks.append(data)
            resources = resolve(page.get("Resources"))
            self.execute(b"\n".join(chunks), resources if isinstance(resources, dict) else {}, GraphicsState(), 0)
        return self

    def _next_order(self):
        self._order += 1
        return self._order

    def _font(self, resources, name):
        fonts = self.document.resolve(resources.get("Font"))
        reference = fonts.get(name) if isinstance(fonts, dict) else None
        key = reference.number if isinstance(reference, Ref) else id(reference)
        if key not in self._fonts:
            font = self.document.resolve(reference)
            self._fonts[key] = PdfFont(self.document, font) if isinstance(font, dict) else self._fallback_font
        return self._fonts[key]

    def execute(self, content, resources, state, depth):
        parser = Parser(content)
        stack, operands, path = [], [], []
        curved = clipping = False
        text_matrix = line_matrix = IDENTITY
        while self.operations <= MAX_OPERATIONS_PER_DOCUMENT:
            try:
                if parser.at_end():
                    break
                token = parser.read_object()
            except PdfError:
                break
            if not isinstance(token, Keyword):
                operands.append(token)
                if len(operands) > 1000:          # garbage, not operands
                    operands = operands[-16:]
                continue
            operator = str(token)
            self.operations += 1

            if operator == "q":
                if len(stack) < MAX_STATE_STACK:
                    stack.append(state.copy())
            elif operator == "Q":
                if stack:
                    state = stack.pop()
            elif operator == "cm":
                matrix = _numbers(operands, 6)
                if matrix:
                    state.ctm = multiply(tuple(matrix), state.ctm)
            elif operator in ("g", "rg", "k"):
                state.fill = _colour_from(_numbers(operands, {"g": 1, "rg": 3, "k": 4}[operator])) or state.fill
            elif operator in ("sc", "scn"):
                numeric = [float(value) for value in operands if isinstance(value, (int, float))]
                state.fill = _colour_from(numeric) if len(numeric) == len(operands) else None
            elif operator == "cs":
                state.fill = (0.0, 0.0, 0.0)
            elif operator == "re":
                rectangle = _numbers(operands, 4)
                if rectangle:
                    x, y, width, height = rectangle
                    corners = [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
                    path.append([apply(state.ctm, px, py) for px, py in corners])
            elif operator == "m":
                point = _numbers(operands, 2)
                if point:
                    path.append([apply(state.ctm, *point)])
            elif operator in ("l", "c", "v", "y"):
                point = _numbers(operands, 2)       # the last two operands are always the end point
                if point and path:
                    path[-1].append(apply(state.ctm, *point))
                curved = curved or operator != "l"
            elif operator in ("W", "W*"):
                clipping = True                     # takes effect when the path is finished
            elif operator in FILL_OPERATORS or operator in ("n", "S", "s"):
                if operator in FILL_OPERATORS and not curved:
                    for subpath in path:
                        box = _intersect(_bounding_box(subpath), state.clip) if len(subpath) >= 3 else None
                        if box and box[2] - box[0] > 0.5 and box[3] - box[1] > 0.5:
                            self.boxes.append(PaintedBox(self._page, self._next_order(), box, "fill", state.fill,
                                                         opaque=state.fill_alpha >= OPAQUE))
                if clipping and path:
                    points = [point for subpath in path for point in subpath]
                    state.clip = _intersect(_bounding_box(points), state.clip) or (0.0, 0.0, 0.0, 0.0)
                path, curved, clipping = [], False, False
            elif operator == "BT":
                text_matrix = line_matrix = IDENTITY
            elif operator == "Tf" and len(operands) >= 2:
                state.font = self._font(resources, operands[-2])
                state.font_size = float(operands[-1]) if isinstance(operands[-1], (int, float)) else 0.0
            elif operator in ("Tc", "Tw", "Tz", "TL", "Ts", "Tr"):
                value = _numbers(operands, 1)
                if value:
                    self._set_text_parameter(state, operator, value[0])
            elif operator in ("Td", "TD"):
                offset = _numbers(operands, 2)
                if offset:
                    if operator == "TD":
                        state.leading = -offset[1]
                    line_matrix = multiply(translate(*offset), line_matrix)
                    text_matrix = line_matrix
            elif operator == "Tm":
                matrix = _numbers(operands, 6)
                if matrix:
                    text_matrix = line_matrix = tuple(matrix)
            elif operator in ("T*", "\x27", "\""):
                if operator == "\"":
                    spacing = _numbers(operands[:2], 2)
                    if spacing:
                        state.word_spacing, state.char_spacing = spacing
                line_matrix = multiply(translate(0, -state.leading), line_matrix)
                text_matrix = line_matrix
                if operator != "T*" and operands and isinstance(operands[-1], bytes):
                    text_matrix = self._show(operands[-1], state, text_matrix)
            elif operator == "Tj" and operands and isinstance(operands[-1], bytes):
                text_matrix = self._show(operands[-1], state, text_matrix)
            elif operator == "TJ" and operands and isinstance(operands[-1], list):
                for item in operands[-1]:
                    if isinstance(item, bytes):
                        text_matrix = self._show(item, state, text_matrix)
                    elif isinstance(item, (int, float)):
                        shift = -item / 1000 * state.font_size * state.horizontal_scale
                        text_matrix = multiply(translate(shift, 0), text_matrix)
            elif operator == "gs" and operands:
                state.fill_alpha = self._fill_alpha(resources, operands[-1], state.fill_alpha)
            elif operator == "Do" and operands:
                self._draw_xobject(operands[-1], resources, state, depth)
            elif operator == "BI":
                image_data = content.find(b"ID", parser.position)
                end = INLINE_IMAGE_END.search(content, image_data + 2) if image_data >= 0 else None
                parser.position = end.end() if end else len(content)
                self._add_image_box(state)
            operands = []

    @staticmethod
    def _set_text_parameter(state, operator, value):
        if operator == "Tc":
            state.char_spacing = value
        elif operator == "Tw":
            state.word_spacing = value
        elif operator == "Tz":
            state.horizontal_scale = value / 100
        elif operator == "TL":
            state.leading = value
        elif operator == "Ts":
            state.rise = value
        elif operator == "Tr":
            state.render_mode = int(value)

    def _show(self, raw, state, text_matrix):
        """Record one string of glyphs and return the text matrix moved past it."""
        font = state.font or self._fallback_font
        size, scale = state.font_size, state.horizontal_scale
        matrix = multiply(text_matrix, state.ctm)
        chars, advance = [], 0.0
        for text, width, is_space in font.decode(raw):
            glyph_width = width / 1000 * size * scale
            centre = apply(matrix, advance + glyph_width / 2, state.rise + size * 0.35)
            edge = apply(matrix, advance + glyph_width, state.rise + size * 0.35)
            chars.append((text, centre[0], centre[1], abs(edge[0] - centre[0])))
            advance += glyph_width + (state.char_spacing + (state.word_spacing if is_space else 0)) * scale
        origin = apply(matrix, 0, state.rise)
        top = apply(matrix, 0, state.rise + size)
        if any(char[0].strip() for char in chars):
            self.runs.append(TextRun(
                page=self._page, order=self._next_order(), text="".join(char[0] for char in chars),
                x=origin[0], y=origin[1], end_x=apply(matrix, advance, state.rise)[0],
                size=math.hypot(top[0] - origin[0], top[1] - origin[1]), font=font.name,
                render_mode=state.render_mode, fill=state.fill, chars=chars))
        return multiply(translate(advance, 0), text_matrix)

    def _fill_alpha(self, resources, name, current):
        """Fill opacity after a gs operator: its ExtGState's /ca, and a soft mask makes it see-through."""
        states = self.document.resolve(resources.get("ExtGState"))
        settings = self.document.resolve(states.get(name)) if isinstance(states, dict) else None
        if not isinstance(settings, dict):
            return current
        alpha = settings.get("ca", current)
        mask = settings.get("SMask")
        if mask not in (None, "None"):
            alpha = min(alpha, 0.5) if isinstance(alpha, (int, float)) else 0.5
        return float(alpha) if isinstance(alpha, (int, float)) else current

    def _add_image_box(self, state, transparent=False):
        corners = [apply(state.ctm, x, y) for x, y in ((0, 0), (1, 0), (1, 1), (0, 1))]
        box = _intersect(_bounding_box(corners), state.clip)
        if box:
            self.boxes.append(PaintedBox(self._page, self._next_order(), box, "image", None,
                                         opaque=not transparent and state.fill_alpha >= OPAQUE))

    def _draw_xobject(self, name, resources, state, depth):
        resolve = self.document.resolve
        xobjects = resolve(resources.get("XObject"))
        reference = xobjects.get(name) if isinstance(xobjects, dict) else None
        xobject = resolve(reference)
        if not isinstance(xobject, Stream):
            return
        subtype = xobject.get("Subtype")
        if subtype == "Image":
            self._add_image_box(state, transparent="SMask" in xobject.attributes or "Mask" in xobject.attributes)
        elif subtype == "Form" and depth < MAX_FORM_DEPTH:
            if isinstance(reference, Ref):
                self.referenced_streams.add(reference.number)
            matrix = resolve(xobject.get("Matrix"))
            form_state = state.copy()
            if isinstance(matrix, list) and len(matrix) == 6 and all(isinstance(v, (int, float)) for v in matrix):
                form_state.ctm = multiply(tuple(float(v) for v in matrix), state.ctm)
            form_resources = resolve(xobject.get("Resources"))
            self.execute(self.document.stream_data(xobject) or b"",
                         form_resources if isinstance(form_resources, dict) else resources, form_state, depth + 1)
