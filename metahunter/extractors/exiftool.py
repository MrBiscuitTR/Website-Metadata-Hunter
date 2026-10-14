"""Optional ExifTool support.

When .config names a working ExifTool, it reads the regular metadata tags and our own
parsers add what it does not report. Security notes:
- the file is piped through stdin, so no temporary copies are written to disk;
- the command is a fixed argument list; no shell is involved;
- versions before 12.24 are refused: they could run code hidden in crafted files
  (CVE-2021-22204), and we feed ExifTool files from the open internet.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .metadata import Metadata

MINIMUM_VERSION = (12, 24)
VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)")
EXECUTABLE_NAMES = ("exiftool.exe", "exiftool")
VERSION_TIMEOUT = 20
EXTRACT_TIMEOUT = 120
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # keeps a console from flashing up on Windows

# Groups whose tags are always technical, whatever their names suggest.
TECHNICAL_GROUPS = re.compile(r"^(ICC|JFIF)")

SKIPPED_GROUPS = {"System", "ExifTool"}
SPECIAL_CASES = {("PDF", "Creator"): "software", ("XMP-xmp", "CreatorTool"): "software"}
# First matching rule wins. Camera comes before time so ExposureTime lands in camera.
CATEGORY_RULES = [
    ("location", r"GPS|Location|City|Country|State$|Province|Sublocation|Latitude|Longitude|Altitude"),
    ("camera", r"Exposure|FNumber|Aperture|^ISO|FocalLength|Flash|ShutterSpeed|Metering|WhiteBalance|"
               r"SceneCaptureType|DigitalZoom|SubjectDistance|LightSource"),
    ("device", r"Serial|^Make$|Model|Lens|DeviceManufacturer|HostComputer|Firmware|Printer"),
    ("software", r"Software|Producer|CreatorTool|Application|Encoder|Generator|SoftwareAgent|HandlerVendor"),
    ("people", r"Author|Artist|Creator|By-?line|Copyright|Rights|Owner|Company|Manager|LastModifiedBy|"
               r"Publisher|Credit|Contact|Email|Writer|Composer|Contributor|Editor|Photographer"),
    ("time", r"Date|Time|Year"),
    ("document", r"Title|Subject|Keywords|Description|Caption|Headline|PageCount|Pages$|Language|Revision|"
                 r"Category|Label|Rating|Genre|Album|Track"),
    ("hidden", r"Comment|Thumbnail|Preview|DocumentID|InstanceID|UniqueID|Note|Hidden|Macro|Private|UserData"),
]
COMPILED_RULES = [(category, re.compile(pattern)) for category, pattern in CATEGORY_RULES]


def categorise(group, tag, value):
    if (group, tag) in SPECIAL_CASES:
        return SPECIAL_CASES[(group, tag)]
    if isinstance(value, str) and value.startswith("(Binary data"):
        return "hidden"
    if TECHNICAL_GROUPS.match(group) and not tag.startswith("GPS"):
        return "technical"
    for category, pattern in COMPILED_RULES:
        if pattern.search(tag):
            return category
    return "technical"


def readable(tag):
    """GPSLatitude -> GPS Latitude, ModifyDate -> Modify Date."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", tag)


def _strip_quotes(text):
    text = text.strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in "\"\x27":
        text = text[1:-1].strip()
    return text


def _setting_variants(setting):
    """The setting as one path, plus each entry if it looks like a PATH-style list."""
    text = _strip_quotes(setting)
    if text.lower().startswith("file:"):
        text = unquote(urlsplit(text).path)
        if re.match(r"^/[A-Za-z]:", text):          # file:///C:/... on Windows
            text = text[1:]
    variants = [text]
    for separator in {os.pathsep, ";"}:
        if separator in text:
            variants += [_strip_quotes(part) for part in text.split(separator) if part.strip()]
    return variants


def _normalise(text):
    """Undo escaping and expand ~ and environment variables."""
    text = os.path.expanduser(os.path.expandvars(text))
    leading = len(text) - len(text.lstrip("\x5c"))
    prefix = "\x5c\x5c" if leading >= 2 else "\x5c" * leading    # keep network-share prefixes
    body = text[leading:]
    while "\x5c\x5c" in body:                                    # escaped (doubled) backslashes
        body = body.replace("\x5c\x5c", "\x5c")
    text = prefix + body
    if os.sep == "/":
        text = text.replace("\x5c ", " ").replace("\x5c", "/")   # escaped spaces, Windows separators
    else:
        drive = re.match(r"^(?:/mnt)?/([A-Za-z])(?=/|$)", text)  # Git Bash /c/... and WSL /mnt/c/...
        if drive:
            text = f"{drive.group(1)}:{text[drive.end():] or '/'}"
    return text



def _executable_in(folder):
    """exiftool(.exe) inside a folder, or one level down (the unzipped Windows package nests a folder)."""
    nested = sorted(path for path in folder.iterdir() if path.is_dir() and path.name.lower().startswith("exiftool"))
    for directory in [folder] + nested:
        for name in EXECUTABLE_NAMES:
            if (directory / name).is_file():
                return directory / name
    return None


def locate_executable(setting, base_folder):
    """Turn whatever the user typed into an existing file path, or None.

    Accepts quoted or unquoted paths, either slash style, doubled (escaped) backslashes,
    relative paths (to the config folder or the current folder), a folder holding the
    executable, a bare program name found on PATH, a PATH-style list, ~, $VAR, %VAR%
    and file:// URIs.
    """
    if not setting or any(ord(character) < 32 for character in setting):
        return None
    for variant in _setting_variants(setting):
        text = _normalise(variant)
        if not text:
            continue
        path = Path(text)
        candidates = [path] if path.is_absolute() else [Path(base_folder) / path, Path.cwd() / path]
        for candidate in candidates:
            try:
                candidate = candidate.resolve()
                if candidate.is_dir():
                    found = _executable_in(candidate)
                    if found:
                        return found
                elif candidate.is_file():
                    return candidate
                elif os.name == "nt" and not candidate.suffix and candidate.with_suffix(".exe").is_file():
                    return candidate.with_suffix(".exe")
            except OSError:
                continue
        if "/" not in text and "\x5c" not in text:                # a bare name: look it up on PATH
            found = shutil.which(text)
            if found:
                return Path(found).resolve()
    return None


class ExifTool:
    def __init__(self, executable, version):
        self.executable = executable
        self.version = version

    @classmethod
    def from_setting(cls, setting, base_folder):
        """Returns (ExifTool or None, a one-line status message for the user)."""
        if not _strip_quotes(setting):
            return None, "ExifTool not configured; using the built-in parsers."

        executable = locate_executable(setting, base_folder)
        if executable is None:
            return None, f"ExifTool not found at {setting!r}; using the built-in parsers."
        if "(-k)" in executable.name:
            return None, ("exiftool(-k).exe waits for a key press; rename it to exiftool.exe. "
                          "Using the built-in parsers.")
        try:
            result = subprocess.run([str(executable), "-ver"], stdin=subprocess.DEVNULL, capture_output=True,
                                    timeout=VERSION_TIMEOUT, creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as error:
            return None, f"Could not run ExifTool at {executable}: {error}. Using the built-in parsers."
        output = result.stdout.decode("utf-8", "replace").strip()
        match = VERSION_PATTERN.match(output)
        if not match:
            return None, f"{executable} did not print an ExifTool version; using the built-in parsers."
        if (int(match.group(1)), int(match.group(2))) < MINIMUM_VERSION:
            return None, (f"ExifTool {match.group(0)} is too old (needs 12.24+, older versions have a known "
                          "code-execution bug). Using the built-in parsers.")
        version = output.split()[0]
        return cls(executable, version), f"Using ExifTool {version} at {executable}, plus built-in deep checks."

    def extract(self, data):
        """Metadata from ExifTool, or None if it failed or does not know the format."""
        command = [str(self.executable), "-json", "-G1", "-a", "-u", "-c", "%+.8f", "-"]
        try:
            result = subprocess.run(command, input=data, capture_output=True, timeout=EXTRACT_TIMEOUT,
                                    creationflags=NO_WINDOW)
            records = json.loads(result.stdout.decode("utf-8", "replace") or "[]")
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if not records or not isinstance(records[0], dict) or "ExifTool:Error" in records[0]:
            return None
        return self._to_metadata(records[0])

    @staticmethod
    def _to_metadata(record):
        meta = Metadata()
        for key, value in record.items():
            group, _, tag = key.rpartition(":")
            if key == "SourceFile":
                continue
            if group == "ExifTool" and tag in ("Warning", "Error"):
                meta.warnings.append(f"ExifTool: {value}")
            if group in SKIPPED_GROUPS:
                continue
            meta.add(categorise(group, tag, value), f"{readable(tag)} ({group})", value)

        latitude, longitude = record.get("Composite:GPSLatitude"), record.get("Composite:GPSLongitude")
        if latitude is None:   # QuickTime and some XMP only offer a combined "lat, lon[, alt]" string
            combined = next((value for key, value in record.items() if key.endswith(("GPSCoordinates", "GPSPosition"))), None)
            numbers = re.findall(r"[+-]?\d+(?:\.\d+)?", str(combined)) if combined else []
            if len(numbers) >= 2:
                latitude, longitude = numbers[0], numbers[1]
        try:
            if latitude is not None and longitude is not None:
                meta.add_gps(float(latitude), float(longitude), "ExifTool GPS")
        except (TypeError, ValueError):
            meta.warnings.append(f"ExifTool GPS values not numeric: {latitude}, {longitude}")
        return meta
