"""Pick the right parser for a file, run it defensively, and combine it with ExifTool if configured."""

import hashlib
import threading

from . import audio, bitmap, bmff, exif, gif, jpeg, matroska, office, ole, pdf, png, riff, svg
from .leaks import scan_for_leaks
from .metadata import Metadata

PARSERS = {
    "jpeg": jpeg.extract, "png": png.extract, "gif": gif.extract, "webp": riff.extract, "tiff": exif.extract_tiff,
    "bmp": bitmap.extract_bmp, "ico": bitmap.extract_ico, "svg": svg.extract, "bmff": bmff.extract,
    "pdf": pdf.extract, "zip": office.extract, "ole": ole.extract, "mp3": audio.extract_mp3,
    "flac": audio.extract_flac, "ogg": audio.extract_ogg, "wav": riff.extract, "avi": riff.extract,
    "matroska": matroska.extract,
}
FORMAT_NAMES = {
    "jpeg": "JPEG image", "png": "PNG image", "gif": "GIF image", "webp": "WebP image", "tiff": "TIFF image / camera raw",
    "bmp": "BMP image", "ico": "Windows icon", "svg": "SVG image", "bmff": "MP4 / MOV / M4A / HEIC / AVIF",
    "pdf": "PDF document", "zip": "ZIP-based document or archive", "ole": "Legacy Office / OLE2 document",
    "mp3": "MP3 audio", "flac": "FLAC audio", "ogg": "Ogg / Opus audio", "wav": "WAV audio", "avi": "AVI video",
    "matroska": "Matroska / WebM video", "unknown": "unrecognised",
}
MAX_NESTING = 3
_nesting = threading.local()


def detect_format(data, name=""):
    """Identify a file by its leading bytes; the name only breaks ties for weak signatures."""
    head = data[:64]
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(png.SIGNATURE):
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF":
        return {b"WEBP": "webp", b"WAVE": "wav", b"AVI ": "avi"}.get(head[8:12], "unknown")
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if b"%PDF-" in data[:1024]:
        return "pdf"
    if head[:4] == b"PK\x03\x04":
        return "zip"
    if head[:8] == ole.SIGNATURE:
        return "ole"
    if head[:3] == b"ID3" or (extension == "mp3" and head[:1] == b"\xff"):
        return "mp3"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip"):
        return "bmff"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "matroska"
    if head[:2] == b"BM" and len(data) > 26:
        return "bmp"
    if head[:4] in (b"\x00\x00\x01\x00", b"\x00\x00\x02\x00"):
        count = int.from_bytes(head[4:6], "little")
        if 0 < count < 256 and len(data) >= 6 + 16 * count:
            return "ico"
    if b"<svg" in data[:4096].lower():
        return "svg"
    return "unknown"


def run_parser(file_format, data, meta):
    """Run one parser; a malformed file adds a warning instead of crashing the scan."""
    parser = PARSERS.get(file_format)
    if parser is None:
        return
    try:
        parser(data, meta)
    except Exception as error:   # untrusted input: any parser may hit malformed data
        meta.warnings.append(f"{FORMAT_NAMES[file_format]} parser stopped early: {type(error).__name__}: {error}")


def extract_nested(data, meta, prefix, skip_technical=False):
    """Read a file found inside another one (cover art, embedded photo) and merge it under a prefix."""
    depth = getattr(_nesting, "depth", 0)
    if depth >= MAX_NESTING or not data:
        return
    _nesting.depth = depth + 1
    try:
        inner = Metadata(detect_format(data))
        run_parser(inner.format, data, inner)
        meta.merge(inner, prefix, skip_categories=("technical",) if skip_technical else ())
    finally:
        _nesting.depth = depth


def extract_metadata(data, name="", exiftool=None, scan_leaks=True):
    """Everything we can learn from one file's bytes. Purely offline."""
    file_format = detect_format(data, name)
    meta = Metadata(file_format)
    meta.add("technical", "Detected format", FORMAT_NAMES[file_format])
    meta.add("technical", "File size", f"{len(data):,} bytes")
    meta.add("technical", "SHA-256", hashlib.sha256(data).hexdigest())

    built_in = Metadata(file_format)
    run_parser(file_format, data, built_in)
    from_exiftool = exiftool.extract(data) if exiftool else None
    if from_exiftool is not None:
        meta.add("technical", "Metadata read by", f"ExifTool {exiftool.version} + built-in deep checks")
        meta.absorb(from_exiftool)
        meta.absorb(built_in.deep_findings())
    else:
        meta.add("technical", "Metadata read by", "built-in parsers")
        meta.absorb(built_in)
    if scan_leaks:
        scan_for_leaks(data, meta)
    return meta
