"""The one HTTP client the app uses: polite, size-capped and kept off private networks.

- only http and https, also after redirects;
- hosts that resolve to private, loopback or link-local addresses are refused unless
  explicitly allowed, so a public page cannot make us probe your local network;
- bodies are read up to a limit and no further;
- one shared delay between requests, whatever the number of threads.
"""

import dataclasses
import http.client
import ipaddress
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from ..control import Cancelled
from .urls import ALLOWED_SCHEMES, host_of


DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; WebsiteMetadataHunter/1.0)"


class FetchError(Exception):
    """A request failed; the message says why."""


class TooLarge(FetchError):
    pass


class Blocked(FetchError):
    pass


@dataclasses.dataclass
class Response:
    url: str          # final URL after redirects
    status: int
    headers: dict     # lower-cased names
    body: bytes

    @property
    def content_type(self):
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    """Re-checks every redirect target before following it."""

    def __init__(self, fetcher):
        super().__init__()
        self.fetcher = fetcher

    def redirect_request(self, request, response, code, message, headers, new_url):
        self.fetcher.check_allowed(new_url)
        return super().redirect_request(request, response, code, message, headers, new_url)


class Fetcher:
    def __init__(self, user_agent=DEFAULT_USER_AGENT, timeout=20.0, delay=0.3, verify_tls=True,
                 allow_private=False, control=None):
        self.control = control          # ScanEvents: pauses and stops reach every request
        self.user_agent = user_agent

        self.timeout = timeout
        self.delay = delay
        self.allow_private = allow_private
        context = ssl.create_default_context()
        if not verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self._opener = urllib.request.build_opener(_GuardedRedirects(self), urllib.request.HTTPSHandler(context=context))
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self._host_verdicts = {}

    def check_allowed(self, url):
        """Raise Blocked unless the URL is http(s) on a public address."""
        if urlsplit(url).scheme.lower() not in ALLOWED_SCHEMES:
            raise Blocked(f"refusing non-web address {url}")
        host = host_of(url)
        if self.allow_private:
            return
        if host not in self._host_verdicts:
            try:
                addresses = {info[4][0].split("%", 1)[0] for info in socket.getaddrinfo(host, None)}
            except (socket.gaierror, UnicodeError) as error:
                raise FetchError(f"cannot resolve {host}: {error}") from None
            private = [address for address in addresses if not ipaddress.ip_address(address).is_global]
            self._host_verdicts[host] = private[0] if private else None
        if self._host_verdicts[host]:
            raise Blocked(f"{host} resolves to a private address ({self._host_verdicts[host]}); "
                          "use --allow-private to scan internal hosts")

    def _wait_turn(self):
        with self._lock:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self.delay
        if wait > 0:
            time.sleep(wait)

    def _read_body(self, response, max_bytes):
        """Read in chunks so a stop interrupts large downloads, and never past max_bytes."""
        chunks, size = [], 0
        while True:
            if self.control and self.control.stop_requested():
                raise Cancelled()
            chunk = response.read(min(256 * 1024, max_bytes + 1 - size))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                raise TooLarge(f"larger than the {max_bytes:,} byte limit")

    def get(self, url, max_bytes, timeout=None, accept="*/*"):
        if self.control:
            self.control.checkpoint()
        self.check_allowed(url)
        self._wait_turn()
        if self.control:
            self.control.checkpoint()

        request = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent, "Accept": accept, "Accept-Encoding": "identity"})
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as response:
                declared = response.headers.get("Content-Length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise TooLarge(f"{int(declared):,} bytes is over the {max_bytes:,} byte limit")
                body = self._read_body(response, max_bytes)
                headers
 = {name.lower(): value for name, value in response.headers.items()}
                return Response(response.geturl(), response.status, headers, body)
        except urllib.error.HTTPError as error:
            raise FetchError(f"HTTP {error.code} {error.reason}") from None
        except urllib.error.URLError as error:
            raise FetchError(str(error.reason)) from None
        except (socket.timeout, TimeoutError, ssl.SSLError, ConnectionError, http.client.HTTPException, ValueError) as error:
            raise FetchError(f"{type(error).__name__}: {error}") from None
