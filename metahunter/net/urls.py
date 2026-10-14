"""URL helpers: normalising user input, comparing hosts and deciding what is in scope."""

import posixpath
import re
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

ALLOWED_SCHEMES = ("http", "https")
SCOPES = {
    "host": "only this exact host (e.g. blog.example.com)",
    "domain": "this host and all of its subdomains (*.example.com)",
    "any": "follow links to any website",
}
HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


def normalise_target(text):
    """Accept example.com, sub.example.com/page or a full URL; return a full http(s) URL."""
    text = text.strip()
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text):
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError(f"only http and https addresses can be scanned, not {parts.scheme}")
    netloc = _ascii_netloc(parts.netloc.lower())
    host = urlsplit(f"//{netloc}").hostname or ""
    if not host or not (HOSTNAME.match(host) or re.match(r"^[0-9.]+$|^\[?[0-9a-f:]+\]?$", host)):
        raise ValueError(f"not a valid host name: {parts.hostname or text}")
    return urlunsplit((parts.scheme.lower(), netloc, _encode(parts.path or "/"), _encode(parts.query), ""))


def clean_url(url, base=None):
    """Resolve a link against its page, drop the #fragment; None for non-web links."""
    try:
        absolute = urljoin(base, url.strip()) if base else url.strip()
        parts = urlsplit(absolute)
    except ValueError:
        return None
    if parts.scheme.lower() not in ALLOWED_SCHEMES or not parts.hostname:
        return None
    return urlunsplit((parts.scheme.lower(), _ascii_netloc(parts.netloc.lower()), _encode(parts.path or "/"),
                       _encode(parts.query), ""))


# Characters that may stay as they are; existing %XX escapes are kept, everything else is encoded.
SAFE_URL_CHARACTERS = "/%:@!$&'()*+,;=~-._?"


def _encode(text):
    """Percent-encode spaces and non-ASCII characters, as browsers do before sending a request."""
    return quote(text, safe=SAFE_URL_CHARACTERS)


def _ascii_netloc(netloc):
    """Internationalised host names (bÃ¼cher.de) become their ASCII "xn--" form."""
    if netloc.isascii():
        return netloc
    userinfo, at, hostport = netloc.rpartition("@")
    host, colon, port = hostport.partition(":")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    return f"{userinfo}{at}{host}{colon}{port}"


def host_of(url):
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def base_domain(host):
    """The domain whose subdomains count as "same site": the host without a leading www."""
    return host[4:] if host.startswith("www.") else host


def is_same_or_subdomain(host, domain):
    return host == domain or host.endswith("." + domain)


def in_scope(url, start_host, scope):
    host = host_of(url)
    if scope == "any":
        return bool(host)
    if scope == "host":   # example.com and www.example.com count as the same host
        return base_domain(host) == base_domain(start_host)
    return is_same_or_subdomain(host, base_domain(start_host))


def extension_of(url):
    path = unquote(urlsplit(url).path)
    name = posixpath.basename(path)
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def file_name_of(url, default="file"):
    name = posixpath.basename(unquote(urlsplit(url).path))
    return name or default
