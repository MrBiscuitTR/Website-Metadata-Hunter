"""Pull links, embedded files and a little page metadata out of an HTML page.

Uses the standard library HTML parser: nothing in the page is executed.
"""

import base64
import binascii
import dataclasses
import re
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin

CSS_URL = re.compile(r"url\(\s*[\"\x27]?([^\"\x27)]+)[\"\x27]?\s*\)")
CSS_IMPORT = re.compile(r"@import\s+[\"\x27]([^\"\x27]+)[\"\x27]")
EMAIL = re.compile(r"[\w.%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,24}")
MAX_DATA_URI_BYTES = 10 * 1024 * 1024
MAX_DATA_URIS = 50

# Tag/attribute pairs that point at files, with a hint of what kind of file to expect.
ASSET_ATTRIBUTES = {
    "img": (("src", "images"), ("data-src", "images"), ("data-original", "images"), ("data-lazy-src", "images")),
    "video": (("src", "video"), ("poster", "images")),
    "audio": (("src", "audio"),),
    "source": (("src", None),),
    "track": (("src", None),),
    "embed": (("src", None),),
    "object": (("data", None),),
    "input": (("src", "images"),),
    "body": (("background", "images"),),
    "table": (("background", "images"),),
    "td": (("background", "images"),),
}
META_ASSETS = {"og:image": "images", "og:image:url": "images", "og:image:secure_url": "images",
               "twitter:image": "images", "twitter:image:src": "images", "msapplication-tileimage": "images",
               "og:video": "video", "og:video:url": "video", "og:audio": "audio"}
ICON_RELS = ("icon", "apple-touch-icon", "apple-touch-icon-precomposed", "mask-icon", "image_src")


@dataclasses.dataclass
class PageLinks:
    links: list = dataclasses.field(default_factory=list)          # absolute URLs of <a>, <iframe>...
    assets: list = dataclasses.field(default_factory=list)         # (absolute URL, category hint or None)
    stylesheets: list = dataclasses.field(default_factory=list)
    inline_files: list = dataclasses.field(default_factory=list)   # (mime type, bytes) from data: URIs
    title: str = ""
    generator: str = ""
    author: str = ""
    emails: set = dataclasses.field(default_factory=set)


def decode_data_uri(uri):
    """data:image/png;base64,.... -> (mime, bytes) or None."""
    header, separator, payload = uri[5:].partition(",")
    if not separator or len(payload) > MAX_DATA_URI_BYTES * 2:
        return None
    mime = header.split(";", 1)[0].strip().lower() or "text/plain"
    try:
        data = base64.b64decode(payload, validate=False) if header.endswith(";base64") else unquote(payload).encode("latin-1", "replace")
    except (binascii.Error, ValueError):
        return None
    return (mime, data) if len(data) <= MAX_DATA_URI_BYTES else None


def css_urls(css):
    return CSS_URL.findall(css) + CSS_IMPORT.findall(css)


class PageParser(HTMLParser):
    def __init__(self, page_url):
        super().__init__(convert_charrefs=True)
        self.base = page_url
        self.found = PageLinks()
        self._in_title = False
        self._in_style = False

    def _resolve(self, raw):
        return urljoin(self.base, raw.strip())

    def _asset(self, raw, hint):
        raw = raw.strip()
        if not raw:
            return
        if raw.lower().startswith("data:"):
            if len(self.found.inline_files) < MAX_DATA_URIS:
                decoded = decode_data_uri(raw)
                if decoded:
                    self.found.inline_files.append(decoded)
            return
        self.found.assets.append((self._resolve(raw), hint))

    def _css(self, css, hint="images"):
        for raw in css_urls(css):
            self._asset(raw, hint)

    def handle_starttag(self, tag, attributes):
        attributes = {name.lower(): (value or "") for name, value in attributes}
        if tag == "base" and attributes.get("href"):
            self.base = self._resolve(attributes["href"])
        if attributes.get("style"):
            self._css(attributes["style"])
        for attribute, hint in ASSET_ATTRIBUTES.get(tag, ()):
            if attributes.get(attribute):
                self._asset(attributes[attribute], hint)
        for attribute in ("srcset", "data-srcset"):
            for candidate in attributes.get(attribute, "").split(","):
                if candidate.strip():
                    self._asset(candidate.split()[0], "images")

        if tag in ("a", "area") and attributes.get("href"):
            href = attributes["href"].strip()
            if href.lower().startswith("mailto:"):
                self.found.emails.update(EMAIL.findall(unquote(href)))
            elif href.lower().startswith("data:"):
                self._asset(href, None)
            else:
                self.found.links.append(self._resolve(href))
        elif tag in ("iframe", "frame") and attributes.get("src"):
            self.found.links.append(self._resolve(attributes["src"]))
        elif tag == "link" and attributes.get("href"):
            relations = attributes.get("rel", "").lower().split()
            if "stylesheet" in relations:
                self.found.stylesheets.append(self._resolve(attributes["href"]))
            elif any(rel in relations for rel in ICON_RELS) or attributes.get("as") == "image":
                self._asset(attributes["href"], "images")
        elif tag == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            content = attributes.get("content", "")
            if key in META_ASSETS and content:
                self._asset(content, META_ASSETS[key])
            elif key == "generator":
                self.found.generator = content
            elif key == "author":
                self.found.author = content
        elif tag == "title":
            self._in_title = True
        elif tag == "style":
            self._in_style = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "style":
            self._in_style = False

    def handle_data(self, data):
        if self._in_title and len(self.found.title) < 500:
            self.found.title += data
        elif self._in_style:
            self._css(data)


def parse_page(html_text, page_url):
    parser = PageParser(page_url)
    try:
        parser.feed(html_text)
        parser.close()
    except (AssertionError, ValueError):   # badly broken markup: keep what we got
        pass
    parser.found.title = " ".join(parser.found.title.split())
    return parser.found
