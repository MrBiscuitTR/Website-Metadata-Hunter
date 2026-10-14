"""Subdomain enumeration from public sources.

Every source except "links" sends the domain name to a third-party service, so all of
them are opt-in. Results are only names: nothing here touches the subdomains themselves.
"""

import json
import re
import socket
import time

from ..control import run_parallel
from ..net.http import FetchError
from ..net.urls import HOSTNAME, host_of, is_same_or_subdomain

SOURCE_TIMEOUT = 60
RETRY_DELAY = 3
MAX_RESPONSE = 32 * 1024 * 1024


def _names_from_crtsh(domain, fetcher):
    body = fetcher.get(f"https://crt.sh/?q=%25.{domain}&output=json", MAX_RESPONSE, timeout=SOURCE_TIMEOUT).body
    return [name for entry in json.loads(body or b"[]") for name in str(entry.get("name_value", "")).split("\n")]


def _names_from_certspotter(domain, fetcher):
    url = f"https://api.certspotter.com/v1/issuances?domain={domain}&include_subdomains=true&expand=dns_names"
    body = fetcher.get(url, MAX_RESPONSE, timeout=SOURCE_TIMEOUT).body
    return [name for entry in json.loads(body or b"[]") for name in entry.get("dns_names", [])]


def _names_from_hackertarget(domain, fetcher):
    body = fetcher.get(f"https://api.hackertarget.com/hostsearch/?q={domain}", MAX_RESPONSE, timeout=SOURCE_TIMEOUT).body
    return [line.split(",", 1)[0] for line in body.decode("utf-8", "replace").splitlines() if "," in line]


def _names_from_alienvault(domain, fetcher):
    url = f"https://otx.alienvault.com/api/v1/indicators/domain/{domain}/passive_dns"
    body = fetcher.get(url, MAX_RESPONSE, timeout=SOURCE_TIMEOUT).body
    return [entry.get("hostname", "") for entry in json.loads(body or b"{}").get("passive_dns", [])]


def _names_from_wayback(domain, fetcher):
    url = (f"https://web.archive.org/cdx/search/cdx?url=*.{domain}&output=txt&fl=original"
           "&collapse=urlkey&limit=20000")
    body = fetcher.get(url, MAX_RESPONSE, timeout=SOURCE_TIMEOUT).body
    return [host_of(line.strip()) for line in body.decode("utf-8", "replace").splitlines()]


SOURCES = {
    "crtsh": _names_from_crtsh,
    "certspotter": _names_from_certspotter,
    "hackertarget": _names_from_hackertarget,
    "alienvault": _names_from_alienvault,
    "wayback": _names_from_wayback,
}


def clean_names(names, domain):
    """Lower-case, strip wildcards and junk, keep only real subdomains of `domain`."""
    cleaned = set()
    for name in names:
        name = str(name).strip().lower().rstrip(".")
        name = re.sub(r"^\*\.", "", name)
        if HOSTNAME.match(name) and is_same_or_subdomain(name, domain):
            cleaned.add(name)
    return cleaned


def enumerate_subdomains(domain, sources, fetcher, events):
    """{subdomain: set of source names} from the chosen third-party sources."""
    found = {}
    for source in sources:
        if source not in SOURCES or events.stop_requested():
            continue
        events.log(f"Asking {source} for subdomains of {domain}...")
        names = None
        for attempt in (1, 2):            # these free services are often briefly overloaded
            try:
                names = clean_names(SOURCES[source](domain, fetcher), domain)
                break
            except (FetchError, ValueError, AttributeError, TypeError) as error:
                events.log(f"  {source} failed{' (will retry once)' if attempt == 1 else ''}: {error}")
                if attempt == 1:
                    time.sleep(RETRY_DELAY)
        if names is None:
            continue
        events.log(f"  {source}: {len(names)} names")
        for name in names:
            found.setdefault(name, set()).add(source)
    return found


def _resolves(name):
    try:
        return bool(socket.getaddrinfo(name, None))
    except (socket.gaierror, UnicodeError, OSError):
        return False


def resolve_names(names, events, threads=8):
    """{name: True if it has a DNS record}, using your normal DNS resolver.

    Names left unchecked
 because the scan was stopped are missing from the result."""
    return dict(run_parallel(sorted(names), lambda name: (name, _resolves(name)), threads, events))

