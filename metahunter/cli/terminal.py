"""Printing logs and a one-line progress bar without the two tripping over each other."""

import shutil
import sys
import threading


class TerminalOutput:
    def __init__(self, stream=sys.stdout):
        self.stream = stream
        self._lock = threading.Lock()
        self._progress_shown = False

    def _clear_progress(self):
        if self._progress_shown:
            width = shutil.get_terminal_size((80, 20)).columns
            self.stream.write("\r" + " " * (width - 1) + "\r")
            self._progress_shown = False

    def log(self, message):
        with self._lock:
            self._clear_progress()
            self.stream.write(f"{message}\n")
            self.stream.flush()

    def progress(self, done, total, label=""):
        with self._lock:
            width = shutil.get_terminal_size((80, 20)).columns
            bar_width = 24
            filled = int(bar_width * done / total) if total else bar_width
            line = f"  [{'#' * filled}{'.' * (bar_width - filled)}] {done}/{total}  {label}"
            self.stream.write("\r" + line[:width - 1].ljust(width - 1))
            self._progress_shown = True
            if done == total:
                self.stream.write("\n")
                self._progress_shown = False
            self.stream.flush()
