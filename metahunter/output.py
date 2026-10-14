"""The only module that writes to disk.

Rules enforced here, so no other code can break them:
- every file goes inside one scan folder, whatever names a website suggests;
- nothing is overwritten: files are opened in exclusive-create mode and a clash gets a suffix;
- nothing is deleted: this program has no code that removes files or folders.
"""

import re
import threading
from pathlib import Path


UNSAFE_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]+")
MAX_NAME_LENGTH = 100
MAX_NAME_ATTEMPTS = 10_000
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}


def safe_filename(name, default="file"):
    """A file name that is valid and harmless on Windows, macOS and Linux."""
    name = UNSAFE_CHARACTERS.sub("_", str(name)).strip("._-")
    name = re.sub(r"_+", "_", name)
    if len(name) > MAX_NAME_LENGTH:
        stem, dot, extension = name.rpartition(".")
        if dot and 0 < len(extension) <= 10:
            name = f"{stem[:MAX_NAME_LENGTH - len(extension) - 1]}.{extension}"
        else:
            name = name[:MAX_NAME_LENGTH]
    if name.split(".", 1)[0].upper() in WINDOWS_RESERVED:
        name = f"_{name}"
    return name or default


def scan_folder_name(target, when):
    """https://blog.example.com/a/b?x=1 -> blog-example-com_a_b_DDMMYYYYHHMM"""
    text = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", target.strip())
    text = re.split(r"[?#]", text, maxsplit=1)[0]
    host, _, path = text.partition("/")
    host = host.rsplit("@", 1)[-1].replace(".", "-").replace(":", "-")
    parts = [host] + [part for part in path.split("/") if part]
    name = "_".join(re.sub(r"[^A-Za-z0-9-]+", "-", part).strip("-") for part in parts)
    name = re.sub(r"[-_]{2,}", "_", name).strip("_-")[:80] or "scan"
    return f"{name}_{when:%d%m%Y%H%M}"


class OutputFolder:
    """A scan folder that is only created when the first file is written into it.

    If the name is taken (two scans in the same minute), name-2, name-3... is used instead.
    """

    def __init__(self, parent, name):
        self.parent = Path(parent)
        self.name = name
        self._root = None
        self._lock = threading.Lock()

    @property
    def root(self):
        return self._root or (self.parent / self.name).resolve()

    @property
    def created(self):
        return self._root is not None

    def _ensure_created(self):
        with self._lock:
            if self._root is None:
                self.parent.mkdir(parents=True, exist_ok=True)
                for attempt in range(1, MAX_NAME_ATTEMPTS):
                    candidate = self.parent / (self.name if attempt == 1 else f"{self.name}-{attempt}")
                    try:
                        candidate.mkdir()
                    except FileExistsError:
                        continue
                    self._root = candidate.resolve()
                    break
                else:
                    raise OSError(f"could not create a new folder for {self.name}")
        return self._root

    def _path_for(self, relative):
        parts = [safe_filename(part) for part in re.split(r"[\x5c/]+", str(relative)) if part not in ("", ".", "..")]
        root = self._ensure_created()
        path = root.joinpath(*parts).resolve()
        if path == root or root not in path.parents:
            raise ValueError(f"refusing to write outside the scan folder: {relative}")
        return path

    def write_bytes(self, relative, data):
        """Write a new file and return its path. Existing files are never touched."""
        path = self._path_for(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(1, MAX_NAME_ATTEMPTS):
            candidate = path if attempt == 1 else path.with_name(f"{path.stem}-{attempt}{path.suffix}")
            try:
                with open(candidate, "xb") as handle:
                    handle.write(data)
            except FileExistsError:
                continue
            return candidate
        raise OSError(f"could not find a free file name for {relative}")

    def write_text(self, relative, text):
        return self.write_bytes(relative, text.encode("utf-8"))

    def relative(self, path):
        return Path(path).resolve().relative_to(self.root).as_posix()
