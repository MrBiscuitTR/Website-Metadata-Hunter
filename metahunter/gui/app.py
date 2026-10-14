"""A small tkinter window around the same ScanOptions and runner the terminal uses.

The scan runs in a worker thread. It only talks to the window through a queue,
which the window drains every 100 ms. Tk must only be touched from the main thread.
"""

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from ..net.http import FetchError
from ..net.urls import SCOPES, normalise_target
from ..options import FILE_TYPES, INFO_CATEGORIES, SUBDOMAIN_SOURCES, ScanOptions
from ..runner import ScanEvents, inspect_local, run_scan

PADDING = {"padx": 8, "pady": 4}
POLL_MS = 100


class WrappingLabel(ttk.Label):
    """A label whose text re-wraps to whatever width it is given, so it never overflows."""

    def __init__(self, parent, **options):
        super().__init__(parent, **options)
        self.bind("<Configure>", lambda event: self.configure(wraplength=max(event.width - 4, 100)))


def open_path(path):
    """Show a file or folder with the system's default application."""
    if sys.platform.startswith("win"):
        os.startfile(path)                                  # noqa: S606 - opens our own output
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


class HunterWindow:
    def __init__(self, root):
        self.root = root
        self.messages = queue.Queue()
        self.events = None
        self.worker = None
        self.last_result = None
        self.variables = {}
        root.title("Website Metadata Hunter")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)
        self._build_target_row()
        self._build_tabs()
        self._build_log()
        self._build_bottom_row()
        self._update_depth_controls()
        root.update_idletasks()
        width = max(root.winfo_reqwidth(), 940)
        height = max(root.winfo_reqheight(), 700)
        root.minsize(root.winfo_reqwidth(), root.winfo_reqheight())
        root.geometry(f"{width}x{height}")
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(POLL_MS, self._drain_messages)

    # ------------------------------------------------------------ variables

    def _bool(self, name, value):
        self.variables[name] = tk.BooleanVar(value=value)
        return self.variables[name]

    def _text(self, name, value):
        self.variables[name] = tk.StringVar(value=str(value))
        return self.variables[name]

    def _checkbox(self, parent, name, value, text, row, column=0):
        box = ttk.Checkbutton(parent, text=text, variable=self._bool(name, value))
        box.grid(row=row, column=column, sticky="w", **PADDING)
        return box

    def _number(self, parent, name, value, text, row, low, high, step=1):
        ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w", **PADDING)
        spin = ttk.Spinbox(parent, from_=low, to=high, increment=step, width=8, textvariable=self._text(name, value))
        spin.grid(row=row, column=1, sticky="w", **PADDING)
        return spin

    # ------------------------------------------------------------ layout

    def _build_target_row(self):
        row = ttk.Frame(self.root, padding=(10, 10, 10, 4))
        row.grid(row=0, column=0, sticky="ew")
        row.columnconfigure(1, weight=1)
        ttk.Label(row, text="Website:").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(row, textvariable=self._text("target", ""))
        entry.grid(row=0, column=1, sticky="ew", padx=8)
        entry.bind("<Return>", lambda _: self.start_scan())
        entry.focus_set()
        self.start_button = ttk.Button(row, text="Start scan", command=self.start_scan)
        self.start_button.grid(row=0, column=2, padx=(0, 6))
        self.stop_button = ttk.Button(row, text="Stop", command=self.stop_scan, state="disabled")
        self.stop_button.grid(row=0, column=3)
        ttk.Label(row, text="A domain (example.com), a subdomain (blog.example.com) or one page URL.",
                  foreground="#5d6675").grid(row=1, column=1, sticky="w", padx=8, pady=(2, 0))

    def _build_tabs(self):
        notebook = ttk.Notebook(self.root)
        notebook.grid(row=1, column=0, sticky="nsew", padx=10, pady=4)
        for title, builder in (("Reach", self._reach_tab), ("Subdomains", self._subdomain_tab),
                               ("Files", self._files_tab), ("Information", self._information_tab),
                               ("PDF check", self._pdf_tab), ("Network", self._network_tab)):
            frame = ttk.Frame(notebook, padding=10)
            frame.columnconfigure(0, weight=1)
            builder(frame)
            notebook.add(frame, text=title)

    def _reach_tab(self, tab):
        pages = ttk.LabelFrame(tab, text="Pages to read", padding=6)
        pages.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.reach = tk.StringVar(value="page")
        for row, (value, text) in enumerate((("page", "Only the page I entered"),
                                             ("linked", "That page and the pages it links to"),
                                             ("deeper", "Follow links deeper, up to this many hops:"))):
            ttk.Radiobutton(pages, text=text, value=value, variable=self.reach,
                            command=self._update_depth_controls).grid(row=row, column=0, sticky="w", **PADDING)
        self.depth_spin = ttk.Spinbox(pages, from_=2, to=20, width=5, textvariable=self._text("depth", 2))
        self.depth_spin.grid(row=2, column=1, sticky="w")
        self.max_pages_spin = self._number(pages, "max_pages", 100, "Stop after this many pages:", 3, 1, 100000)

        scope = ttk.LabelFrame(tab, text="Links may lead to", padding=6)
        scope.grid(row=0, column=1, sticky="nsew")
        tab.columnconfigure(1, weight=1)
        self.scope = tk.StringVar(value="host")
        self.scope_buttons = []
        for row, (value, text) in enumerate(SCOPES.items()):
            button = ttk.Radiobutton(scope, text=text[0].upper() + text[1:], value=value, variable=self.scope)
            button.grid(row=row, column=0, sticky="w", **PADDING)
            self.scope_buttons.append(button)

        extras = ttk.LabelFrame(tab, text="Also", padding=6)
        extras.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self._checkbox(extras, "external_files", True, "Download files on other hosts (CDNs) that these pages embed", 0)
        self._checkbox(extras, "follow_stylesheets", True, "Read stylesheets for background images", 1)
        self._checkbox(extras, "use_sitemap", False, "Visit pages listed in the sitemap", 0, 1)
        self._checkbox(extras, "respect_robots", True, "Obey robots.txt", 1, 1)
        self._checkbox(extras, "wayback_files", False, "Fetch archived copies of files from the Wayback Machine", 2)

    def _subdomain_tab(self, tab):
        WrappingLabel(tab, text="Every source except the first sends the domain name to that third-party "
                                "service. Found subdomains are listed in the report.").grid(row=0, column=0, sticky="ew", **PADDING)
        sources = ttk.LabelFrame(tab, text="Look for subdomains in", padding=6)
        sources.grid(row=1, column=0, sticky="ew")
        for row, (name, text) in enumerate(SUBDOMAIN_SOURCES.items()):
            self._checkbox(sources, f"source_{name}", False, text[0].upper() + text[1:], row)
        action = ttk.LabelFrame(tab, text="With the subdomains found", padding=6)
        action.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.subdomain_action = tk.StringVar(value="report")
        ttk.Radiobutton(action, text="Only list them in the report", value="report",
                        variable=self.subdomain_action).grid(row=0, column=0, sticky="w", **PADDING)
        ttk.Radiobutton(action, text="List them and crawl the ones that resolve", value="crawl",
                        variable=self.subdomain_action).grid(row=1, column=0, sticky="w", **PADDING)

    def _files_tab(self, tab):
        types = ttk.LabelFrame(tab, text="Collect these file types", padding=6)
        types.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        for row, (name, (label, extensions)) in enumerate(FILE_TYPES.items()):
            shown = ", ".join(extensions[:7]) + (", ..." if len(extensions) > 7 else "")
            self._checkbox(types, f"type_{name}", True, f"{label} ({shown})", row)
        limits = ttk.LabelFrame(tab, text="Limits and saving", padding=6)
        limits.grid(row=0, column=1, sticky="nsew")
        tab.columnconfigure(1, weight=1)
        self._number(limits, "max_files", 500, "Maximum files:", 0, 1, 100000)
        self._number(limits, "max_file_mb", 50, "Maximum file size (MB):", 1, 1, 10000)
        self._checkbox(limits, "save_files", True, "Keep copies of downloaded files", 2)
        self._checkbox(limits, "save_embedded", True, "Save thumbnails, cover art, attachments", 3)
        self._checkbox(limits, "leak_scan", True, "Search files for user folders and emails", 4)

    def _information_tab(self, tab):
        WrappingLabel(tab, text="Everything is extracted; these choices decide what goes into the report."
                      ).grid(row=0, column=0, columnspan=2, sticky="ew", **PADDING)
        for index, (name, text) in enumerate(INFO_CATEGORIES.items()):
            self._checkbox(tab, f"info_{name}", True, f"{name.capitalize()}: {text}", 1 + index // 2, index % 2)
        tab.columnconfigure(1, weight=1)
        buttons = ttk.Frame(tab)
        buttons.grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(buttons, text="Select all", command=lambda: self._set_info(True)).grid(row=0, column=0, **PADDING)
        ttk.Button(buttons, text="Select none", command=lambda: self._set_info(False)).grid(row=0, column=1, **PADDING)

    def _set_info(self, value):
        for name in INFO_CATEGORIES:
            self.variables[f"info_{name}"].set(value)

    def _pdf_tab(self, tab):
        self._checkbox(tab, "redaction_check", True, "Run the redaction and hidden-text check on every PDF", 0)
        WrappingLabel(tab, text=(
            "Finds text that is still inside a PDF but was not meant to be read: text painted over by black "
            "boxes or images, text under unapplied Redact annotations, invisible or white text, text placed "
            "outside the page, and text that only exists in earlier saved revisions of the file. "
            "For each PDF a folder under pdf_analysis/ gets a plain-language summary, all text per page, "
            "every text run with its position and colour, every painted shape, an object listing, the "
            "earlier revisions as separate PDFs and any attached files, so you can compare them yourself."
        )).grid(row=1, column=0, sticky="ew", **PADDING)

    def _network_tab(self, tab):
        self._number(tab, "threads", 4, "Parallel downloads:", 0, 1, 32)
        self._number(tab, "delay", 0.3, "Seconds between requests:", 1, 0, 60, 0.1)
        self._number(tab, "timeout", 20, "Request timeout (seconds):", 2, 1, 300)
        ttk.Label(tab, text="User agent (empty = default):").grid(row=3, column=0, sticky="w", **PADDING)
        ttk.Entry(tab, textvariable=self._text("user_agent", "")).grid(row=3, column=1, sticky="ew", **PADDING)
        tab.columnconfigure(0, weight=0)
        tab.columnconfigure(1, weight=1)
        self._checkbox(tab, "verify_tls", True, "Verify TLS certificates", 4)
        self._checkbox(tab, "allow_private", False, "Allow hosts on private / local networks", 5)

    def _build_log(self):
        frame = ttk.Frame(self.root, padding=(10, 4, 10, 0))
        frame.grid(row=2, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        self.progress = ttk.Progressbar(frame, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew")
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(frame, textvariable=self.status).grid(row=1, column=0, sticky="ew", pady=(2, 4))
        self.log = ScrolledText(frame, height=10, wrap="word", state="disabled", font=("TkFixedFont", 9))
        self.log.grid(row=2, column=0, sticky="nsew")

    def _build_bottom_row(self):
        row = ttk.Frame(self.root, padding=10)
        row.grid(row=3, column=0, sticky="ew")
        row.columnconfigure(2, weight=1)
        self.inspect_button = ttk.Button(row, text="Inspect local files...", command=lambda: self.inspect_files(False))
        self.inspect_button.grid(row=0, column=0, padx=(0, 6))
        self.pdf_button = ttk.Button(row, text="Check local PDFs...", command=lambda: self.inspect_files(True))
        self.pdf_button.grid(row=0, column=1)
        self.report_button = ttk.Button(row, text="Open report", command=self.open_report, state="disabled")
        self.report_button.grid(row=0, column=3, padx=6)
        self.folder_button = ttk.Button(row, text="Open scan folder", command=self.open_folder, state="disabled")
        self.folder_button.grid(row=0, column=4)

    def _update_depth_controls(self):
        deeper = self.reach.get() != "page"
        self.depth_spin.configure(state="normal" if self.reach.get() == "deeper" else "disabled")
        self.max_pages_spin.configure(state="normal" if deeper else "disabled")
        for button in self.scope_buttons:
            button.configure(state="normal" if deeper else "disabled")

    # ------------------------------------------------------------ options

    def _read_number(self, name, cast, label):
        try:
            return cast(self.variables[name].get())
        except ValueError:
            raise ValueError(f"{label} must be a number.") from None

    def collect_options(self, need_target=True):
        """ScanOptions from the widgets. Raises ValueError with a readable message."""
        get = lambda name: self.variables[name].get()      # noqa: E731 - short local alias
        options = ScanOptions(
            depth={"page": 0, "linked": 1}.get(self.reach.get()) or self._read_number("depth", int, "Hops"),
            scope=self.scope.get(),
            max_pages=self._read_number("max_pages", int, "Maximum pages"),
            external_files=get("external_files"), use_sitemap=get("use_sitemap"),
            follow_stylesheets=get("follow_stylesheets"), respect_robots=get("respect_robots"),
            wayback_files=get("wayback_files"),
            subdomain_sources=[name for name in SUBDOMAIN_SOURCES if get(f"source_{name}")],
            crawl_subdomains=self.subdomain_action.get() == "crawl",
            file_types=[name for name in FILE_TYPES if get(f"type_{name}")],
            info_categories=[name for name in INFO_CATEGORIES if get(f"info_{name}")],
            max_files=self._read_number("max_files", int, "Maximum files"),
            max_file_mb=self._read_number("max_file_mb", float, "Maximum file size"),
            redaction_check=get("redaction_check"), save_files=get("save_files"),
            save_embedded=get("save_embedded"), leak_scan=get("leak_scan"),
            threads=self._read_number("threads", int, "Parallel downloads"),
            delay=self._read_number("delay", float, "Seconds between requests"),
            timeout=self._read_number("timeout", float, "Request timeout"),
            user_agent=get("user_agent").strip(), verify_tls=get("verify_tls"), allow_private=get("allow_private"),
        )
        if need_target:
            if not get("target").strip():
                raise ValueError("Enter a website first, for example example.com")
            options.target = normalise_target(get("target"))
        problems = options.problems()
        if problems:
            raise ValueError("\n".join(problem[0].upper() + problem[1:] for problem in problems))
        return options

    # ------------------------------------------------------------ running

    def start_scan(self):
        if self.worker:
            return
        try:
            options = self.collect_options()
        except ValueError as error:
            messagebox.showerror("Cannot start", str(error), parent=self.root)
            return
        self._run(lambda events: run_scan(options, events), f"Scanning {options.target}")

    def inspect_files(self, pdf_only):
        if self.worker:
            return
        kinds = [("PDF files", "*.pdf")] if pdf_only else [("All files", "*.*")]
        paths = filedialog.askopenfilenames(parent=self.root, title="Choose files to inspect", filetypes=kinds)
        if not paths:
            return
        try:
            options = self.collect_options(need_target=False)
        except ValueError as error:
            messagebox.showerror("Cannot start", str(error), parent=self.root)
            return
        options.save_files = False            # the originals are already on disk
        if pdf_only:
            options.redaction_check = True
        self._run(lambda events: inspect_local(list(paths), options, events), f"Inspecting {len(paths)} file(s)")

    def _run(self, job, status):
        self.events = ScanEvents(on_log=lambda message: self.messages.put(("log", message)),
                                 on_progress=lambda done, total, label: self.messages.put(("progress", (done, total))))
        self._set_running(True)
        self.status.set(status + "...")
        self.progress.configure(value=0, maximum=1)
        self._append_log(f"--- {status} ---")
        events = self.events

        def work():
            try:
                self.messages.put(("done", job(events)))
            except (ValueError, FetchError, FileNotFoundError, OSError) as error:
                self.messages.put(("error", str(error)))
            except Exception as error:     # show anything unexpected instead of failing silently
                self.messages.put(("error", f"{type(error).__name__}: {error}"))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def stop_scan(self):
        if self.events:
            self.events.request_stop()
            self.status.set("Stopping after the files in progress; the report will still be written...")
            self.stop_button.configure(state="disabled")

    def _set_running(self, running):
        idle = "disabled" if running else "normal"
        for button in (self.start_button, self.inspect_button, self.pdf_button):
            button.configure(state=idle)
        self.stop_button.configure(state="normal" if running else "disabled")
        if running:
            self.report_button.configure(state="disabled")
            self.folder_button.configure(state="disabled")
