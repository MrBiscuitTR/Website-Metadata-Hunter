"""Reads the optional .config file that sits next to hunter.py.

Format: one "key = value" per line. Lines starting with # or ; are comments.
Unknown keys are ignored, so an old config never breaks a newer version.
"""

from pathlib import Path

APP_FOLDER = Path(__file__).resolve().parent.parent
CONFIG_FILE = APP_FOLDER / ".config"
DEFAULTS = {"exiftool_path": ""}


def load_config(path=CONFIG_FILE):
    settings = dict(DEFAULTS)
    path = Path(path)
    if not path.is_file():
        return settings
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        key, separator, value = line.partition("=")
        key = key.strip().lower()
        if separator and key in DEFAULTS:
            settings[key] = value.strip()
    return settings
