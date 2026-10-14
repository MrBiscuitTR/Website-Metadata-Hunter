"""SVG: a text format, so metadata lives in elements, attributes and comments.

Editors like Inkscape and Illustrator leave behind file names and export paths.
"""

import html
import re

from .metadata import decode_text
from .xmp import parse_xmp

MAX_SVG_TEXT = 5 * 1024 * 1024

ATTRIBUTES = {
    "inkscape:version": ("Inkscape version", "software"),
    "sodipodi:docname": ("Original file name", "document"),
    "inkscape:export-filename": ("Export path", "hidden"),
    "sodipodi:absref": ("Absolute reference path", "hidden"),
    "sodipodi:docbase": ("Document folder", "hidden"),
}


def extract(data, meta):
    text = decode_text(data[:MAX_SVG_TEXT])
    root = re.search(r"<svg\b[^>]*>", text)
    if root:
        for attribute in ("width", "height", "viewBox"):
            found = re.search(rf"\s{attribute}=\"([^\"]*)\"", root.group(0))
            if found:
                meta.add("technical", attribute, found.group(1))
    for tag, label in (("title", "Title"), ("desc", "Description")):
        for value in re.findall(rf"<{tag}\b[^>]*>(.*?)</{tag}>", text, re.S)[:5]:
            meta.add("document", label, html.unescape(value))
    for attribute, (label, category) in ATTRIBUTES.items():
        for value in re.findall(rf"\s{re.escape(attribute)}=\"([^\"]*)\"", text)[:5]:
            meta.add_deep(category, label, html.unescape(value))
    for comment in re.findall(r"<!--(.*?)-->", text, re.S)[:20]:
        generator = re.search(r"Generator:\s*([^\n]+)", comment)
        if generator:
            meta.add_deep("software", "Generator", generator.group(1).strip())
        else:
            meta.add_deep("hidden", "Comment", comment)
    for block in re.findall(r"<metadata\b[^>]*>(.*?)</metadata>", text, re.S)[:3]:
        parse_xmp(block, meta)
        for creator in re.findall(r"<dc:creator>.*?<dc:title>([^<]*)</dc:title>", block, re.S):
            meta.add("people", "Creator", html.unescape(creator))
    embedded = len(re.findall(r"href=\"data:image/", text))
    if embedded:
        meta.add_deep("hidden", "Embedded raster images", embedded)
    for link in re.findall(r"href=\"(file:[^\"]+)\"", text)[:10]:
        meta.add_deep("hidden", "Link to local file", link)
    if re.search(r"<script\b", text, re.I):
        meta.add_deep("hidden", "Script", "SVG contains a script element")
