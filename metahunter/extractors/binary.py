"""Low-level helpers for reading untrusted binary data safely."""

import datetime
import zlib

# Upper bound for anything we inflate, so a tiny compressed bomb cannot eat all memory.
MAX_INFLATED_SIZE = 64 * 1024 * 1024

UTC = datetime.timezone.utc
MAC_EPOCH = datetime.datetime(1904, 1, 1, tzinfo=UTC)        # QuickTime / MP4
WINDOWS_EPOCH = datetime.datetime(1601, 1, 1, tzinfo=UTC)    # FILETIME
MATROSKA_EPOCH = datetime.datetime(2001, 1, 1, tzinfo=UTC)


class TooLarge(Exception):
    """Decompressed data would exceed MAX_INFLATED_SIZE."""


def inflate(data, limit=MAX_INFLATED_SIZE, wbits=15):
    """zlib-decompress with a hard output limit."""
    decompressor = zlib.decompressobj(wbits)
    output = decompressor.decompress(data, limit)
    if decompressor.unconsumed_tail:
        raise TooLarge(f"inflated data is larger than {limit} bytes")
    return output


def seconds_since(epoch, seconds):
    """Timestamp string for an offset from an epoch, or None if it is nonsense."""
    try:
        moment = epoch + datetime.timedelta(seconds=seconds)
    except (OverflowError, ValueError):
        return None
    if moment.year < 1971 or moment.year > 2200:
        return None
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def filetime(value):
    """Windows FILETIME (100 ns ticks since 1601) -> timestamp string."""
    if not value:
        return None
    return seconds_since(WINDOWS_EPOCH, value / 10_000_000)


def ascii_or_none(raw):
    """Decode bytes that should be plain ASCII text, or None if they clearly are not."""
    text = raw.split(b"\x00", 1)[0]
    if text and all(32 <= byte < 127 for byte in text):
        return text.decode("ascii")
    return None
