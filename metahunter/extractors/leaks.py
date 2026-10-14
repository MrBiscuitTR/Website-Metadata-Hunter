"""Search any file for strings that identify people or machines.

Plenty of formats keep user folders, network shares and email addresses in places
no metadata spec covers. We look at the raw bytes as ASCII and as UTF-16.
(\x5c is a literal backslash; it keeps the Windows path patterns readable.)
"""

import re

MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_HITS_PER_KIND = 25

PATH_PART = r"[\w .()&,+~$#@!-]{1,100}"
PATTERNS = {
    "Windows user folder": re.compile(rf"[A-Za-z]:\x5c(?:Users|Documents and Settings)(?:\x5c{PATH_PART}){{1,8}}"),
    "Unix / macOS home folder": re.compile(rf"/(?:home|Users)/[\w.-]{{1,64}}(?:/{PATH_PART}){{0,8}}"),
    "Network share": re.compile(rf"\x5c\x5c[\w.-]{{2,63}}(?:\x5c{PATH_PART}){{1,8}}"),
    "Email address": re.compile(r"\b[\w.%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,24}\b"),
}
USERNAME_IN_PATH = re.compile(r"(?:\x5cUsers\x5c|\x5cDocuments and Settings\x5c|/home/|/Users/)([^\x5c/]+)")
IGNORED_USERNAMES = {"public", "default", "all users", "shared", "runner", "user", "admin"}
# Addresses that show up inside common file formats and mean nothing.
IGNORED_EMAILS = re.compile(r"@(?:ns\.adobe\.com|adobe\.com|example\.(?:com|org)|w3\.org|purl\.org)$", re.I)


def _texts(data):
    sample = data[:MAX_SCAN_BYTES]
    yield sample.decode("latin-1")
    # UTF-16LE text appears in Windows formats; try both byte alignments.
    if b"\x00" in sample:
        yield sample.decode("utf-16-le", "ignore")
        yield sample[1:].decode("utf-16-le", "ignore")


def scan_for_leaks(data, meta):
    found = {kind: [] for kind in PATTERNS}
    for text in _texts(data):
        for kind, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                value = match.group(0).strip()
                if kind == "Email address" and IGNORED_EMAILS.search(value):
                    continue
                if value not in found[kind] and len(found[kind]) < MAX_HITS_PER_KIND:
                    found[kind].append(value)
    for kind, values in found.items():
        category = "people" if kind == "Email address" else "hidden"
        for value in values:
            meta.add(category, kind, value)
            username = USERNAME_IN_PATH.search(value)
            if username and username.group(1).lower() not in IGNORED_USERNAMES:
                meta.add("people", "Username (from a file path)", username.group(1))
