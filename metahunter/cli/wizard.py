"""The guided setup: one question at a time, sensible defaults, Enter to accept."""

import shlex

from ..net.urls import SCOPES, normalise_target
from ..options import FILE_TYPES, INFO_CATEGORIES, SUBDOMAIN_SOURCES, ScanOptions
from .views import VIEWS



def ask_text(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def ask_yes_no(prompt, default):
    hint = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{prompt} [{hint}]: ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes", "n", "no"):
            return answer.startswith("y")
        print("  Please answer y or n.")


def ask_number(prompt, default, minimum, maximum, cast=int):
    while True:
        answer = input(f"{prompt} [{default}]: ").strip()
        if not answer:
            return default
        try:
            value = cast(answer)
        except ValueError:
            value = None
        if value is not None and minimum <= value <= maximum:
            return value
        print(f"  Enter a number from {minimum} to {maximum}.")


def ask_choice(prompt, choices, default):
    """choices: [(key, description)]. Returns the chosen key."""
    print(prompt)
    for number, (key, description) in enumerate(choices, start=1):
        marker = "*" if key == default else " "
        print(f"  {marker} {number}. {description}")
    keys = [key for key, _ in choices]
    while True:
        answer = input(f"Choose 1-{len(choices)} [{keys.index(default) + 1}]: ").strip()
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return keys[int(answer) - 1]
        print("  Not one of the options.")


def ask_checkboxes(prompt, choices, selected):
    """A terminal checkbox list. Type numbers to toggle, a = all, n = none, Enter = done."""
    selected = set(selected)
    keys = [key for key, _ in choices]
    while True:
        print(prompt)
        for number, (key, description) in enumerate(choices, start=1):
            print(f"  [{'x' if key in selected else ' '}] {number}. {key:<13} {description}")
        answer = input("Toggle numbers (e.g. 1,3), a = all, n = none, Enter = done: ").strip().lower()
        if not answer:
            return [key for key in keys if key in selected]
        if answer == "a":
            selected = set(keys)
        elif answer == "n":
            selected = set()
        else:
            for part in answer.replace(" ", ",").split(","):
                if part.isdigit() and 1 <= int(part) <= len(keys):
                    selected ^= {keys[int(part) - 1]}
                elif part:
                    print(f"  Ignored {part!r}: not a number in the list.")


def equivalent_command(options, views):
    """The `hunter.py scan ...` command that reproduces these options."""

    defaults = ScanOptions()
    parts = ["python", "hunter.py", "scan", options.target]
    flags = [("depth", "-d"), ("scope", "--scope"), ("max_pages", "--max-pages"), ("max_files", "--max-files"),
             ("max_file_mb", "--max-size"), ("threads", "--threads"), ("delay", "--delay")]
    for field_name, flag in flags:
        if getattr(options, field_name) != getattr(defaults, field_name):
            parts += [flag, str(getattr(options, field_name))]
    for field_name, flag in (("file_types", "--types"), ("info_categories", "--info"), ("subdomain_sources", "--subdomains")):
        value = getattr(options, field_name)
        if value != getattr(defaults, field_name):
            parts += [flag, ",".join(value) or "none"]
    switches = [("external_files", False, "--no-external-files"), ("use_sitemap", True, "--sitemap"),
                ("follow_stylesheets", False, "--no-css"), ("respect_robots", False, "--ignore-robots"),
                ("wayback_files", True, "--wayback-files"), ("crawl_subdomains", True, "--crawl-subdomains"),
                ("redaction_check", False, "--no-redaction-check"), ("save_files", False, "--no-save-files"),
                ("save_embedded", False, "--no-embedded"), ("leak_scan", False, "--no-leak-scan"),
                ("save_reports", False, "--no-report-files")]
    parts += [flag for field_name, value, flag in switches if getattr(options, field_name) == value]
    if views != ["summary"]:
        parts += ["--view", ",".join(views) or "none"]
    return " ".join(shlex.quote(part) for part in parts)


def build_options():
    options = ScanOptions()
    print("\n-- Target --")
    while True:
        try:
            options.target = normalise_target(ask_text("Domain, subdomain or page URL (e.g. example.com)"))
            break
        except ValueError as error:
            print(f"  {error}")

    print("\n-- How deep --")
    reach = ask_choice("Which pages should be read?", [
        ("page", "Only this page"),
        ("linked", "This page and the pages it links to (depth 1)"),
        ("deeper", "Deeper: follow links several hops"),
    ], "page")
    options.depth = {"page": 0, "linked": 1}.get(reach) or ask_number("How many hops", 2, 2, 20)
    if options.depth:
        options.scope = ask_choice("Which links may be followed?", list(SCOPES.items()), "host")
        options.max_pages = ask_number("Stop after how many pages", options.max_pages, 1, 100_000)
        options.use_sitemap = ask_yes_no("Also visit pages listed in the sitemap", False)
    options.external_files = ask_yes_no("Download files hosted on other sites (CDNs) that these pages embed", True)
    options.wayback_files = ask_yes_no("Also fetch archived copies of files from the Wayback Machine", False)

    print("\n-- Subdomains --")
    print("Every source except 'links' sends the domain name to that third-party service.")
    options.subdomain_sources = ask_checkboxes("Where to look for subdomains:", list(SUBDOMAIN_SOURCES.items()), [])
    if options.subdomain_sources:
        options.crawl_subdomains = ask_choice("What to do with the subdomains found?", [
            ("report", "Only list them in the report"),
            ("crawl", "List them and also crawl the ones that resolve"),
        ], "report") == "crawl"

    print("\n-- Files --")
    options.file_types = ask_checkboxes("Which file types to collect:",
                                        [(key, f"{label}: {', '.join(ext[:8])}...") for key, (label, ext) in FILE_TYPES.items()],
                                        options.file_types) or list(FILE_TYPES)
    options.info_categories = ask_checkboxes("Which information is useful to you:", list(INFO_CATEGORIES.items()),
                                             options.info_categories) or list(INFO_CATEGORIES)
    if "pdf" in options.file_types:
        options.redaction_check = ask_yes_no("Run the PDF redaction check (text under black boxes, old revisions...)", True)
    options.save_files = ask_yes_no("Keep copies of the downloaded files", True)
    options.save_embedded = ask_yes_no("Save thumbnails, cover art and attachments found inside files", True)

    print("\n-- Output --")
    options.save_reports = ask_yes_no("Save the reports (HTML, JSON, text, PDF analysis) to the scan folder", True)
    views = ask_checkboxes("What to print here when the scan is done:", list(VIEWS.items()), ["summary"])


    if ask_yes_no("\nAdjust advanced settings (limits, speed, robots.txt)", False):
        options.max_files = ask_number("Maximum number of files", options.max_files, 1, 100_000)
        options.max_file_mb = ask_number("Maximum file size in MB", options.max_file_mb, 0.1, 10_000, float)
        options.threads = ask_number("Parallel downloads", options.threads, 1, 32)
        options.delay = ask_number("Seconds between requests", options.delay, 0.0, 60.0, float)
        options.respect_robots = ask_yes_no("Obey robots.txt", True)
        options.leak_scan = ask_yes_no("Search files for leaked user folders and emails", True)
    return options, views



def run_wizard(terminal):
    from .main import command_scan
    try:
        options, views = build_options()
        print("\nThe same scan as a command (for next time):")
        print(f"  {equivalent_command(options, views)}\n")
        if not ask_yes_no("Start the scan now", True):
            return 0
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 1
    return command_scan(options, views, terminal)

