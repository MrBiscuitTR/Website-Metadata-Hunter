"""Entry point: parse the command line and run the chosen command."""

import datetime
import json
import threading

from ..control import ScanEvents
from ..net.http import FetchError
from ..options import ScanOptions
from ..output import OutputFolder, scan_folder_name
from ..runner import inspect_local, lookup_subdomains, run_scan, scans_folder
from .arguments import build_parser, options_from_arguments
from .overview import overview_text
from .terminal import TerminalOutput
from .views import render_views

PAUSE_MENU = "[c] continue   [s] stop and write the report   [r] restart   [q] quit now"


def _pause_menu(events, terminal):
    """Ctrl+C lands here. Returns "continue", "stop", "restart" or "quit"."""
    events.pause()
    terminal.log("\nPaused. Nothing new starts until you choose (Ctrl+C again quits now).")
    while True:
        try:
            answer = input(f"{PAUSE_MENU}\n> ").strip().lower()[:1]
        except (KeyboardInterrupt, EOFError):
            return "quit"
        choice = {"c": "continue", "": "continue", "s": "stop", "r": "restart", "q": "quit"}.get(answer)
        if choice:
            return choice
        terminal.log("Type c, s, r or q.")


def run_controlled(job, terminal):
    """Run job(events) in a worker thread, with Ctrl+C opening the pause menu.

    Returns ("done", result), ("restart", None) or ("quit", None). Errors from the job are re-raised.
    """
    events = ScanEvents(terminal.log, terminal.progress)
    outcome = {}
    finished = threading.Event()     # waited on instead of Thread.join, which Ctrl+C can confuse

    def work():
        try:
            outcome["result"] = job(events)
        except Exception as error:   # handed back to the main thread below
            outcome["error"] = error
        finally:
            finished.set()

    threading.Thread(target=work, daemon=True).start()
    while not finished.is_set():
        try:
            finished.wait(0.1)
        except KeyboardInterrupt:
            choice = _pause_menu(events, terminal)
            if choice == "continue":
                events.resume()
                terminal.log("Continuing.")
            elif choice == "stop":
                terminal.log("Stopping; the report is written with what was collected.")
                events.request_stop()
            elif choice == "restart":
                events.abort()
                terminal.log("Restarting from scratch...")
                return "restart", None
            else:
                events.abort()
                terminal.log("Quit.")
                return "quit", None
    if "error" in outcome:
        raise outcome["error"]
    return "done", outcome.get("result")


def show_result(result, views, terminal):
    if views:
        terminal.log(render_views(result, views))
    terminal.log("")
    if result.report_files:
        terminal.log(f"Report: {result.report_files['html']}")
        terminal.log(f"        {result.report_files['summary']}")
    elif result.folder.created:
        terminal.log(f"Files saved in {result.folder.root}")
    else:
        terminal.log("Nothing was written to disk (report files and copies were turned off).")


def run_job(job, views, terminal):
    """Run a scan-like job until it finishes or the user quits; a restart runs it again."""
    while True:
        try:
            status, result = run_controlled(job, terminal)
        except (ValueError, FetchError, FileNotFoundError) as error:
            terminal.log(f"Cannot continue: {error}")
            return 1
        if status == "restart":
            continue
        if status == "quit":
            return 130
        show_result(result, views, terminal)
        return 0


def command_scan(options, views, terminal):
    problems = options.problems()
    if problems:
        terminal.log("Cannot start: " + "; ".join(problems))
        return 2
    terminal.log("Tip: press Ctrl+C to pause, stop, restart or quit.")
    return run_job(lambda events: run_scan(options, events), views, terminal)


def command_inspect(paths, options, views, terminal, extensions=None):
    return run_job(lambda events: inspect_local(paths, options, events, extensions), views, terminal)


def command_subdomains(arguments, terminal):
    options = ScanOptions(output_dir=arguments.output_dir)
    status, found = run_controlled(lambda events: lookup_subdomains(arguments.domain, arguments.sources, options, events),
                                   terminal)
    if status != "done":
        return 130
    for name, info in found.items():
        state = {True: "resolves", False: "no DNS record", None: "not checked"}[info["resolves"]]
        terminal.log(f"  {name:50} {state:14} {', '.join(info['sources'])}")
    folder = OutputFolder(scans_folder(options), scan_folder_name(f"subdomains_{arguments.domain}", datetime.datetime.now()))
    folder.write_text("subdomains.txt", "\n".join(found) + "\n")
    folder.write_text("subdomains.json", json.dumps(found, indent=2))
    terminal.log(f"{len(found)} subdomains. Saved to {folder.root}")
    return 0


def main(argv):
    terminal = TerminalOutput()
    parser, commands = build_parser()
    if not argv:
        terminal.log(overview_text())
        try:
            if input("Start the guided wizard now? [Y/n] ").strip().lower() not in ("", "y", "yes"):
                return 0
        except (EOFError, KeyboardInterrupt):
            terminal.log("")
            return 0
        from .wizard import run_wizard
        return run_wizard(terminal)
    arguments = parser.parse_args(argv)

    if arguments.command == "help":
        if arguments.topic in commands.choices:
            commands.choices[arguments.topic].print_help()
        else:
            terminal.log(overview_text())
        return 0
    if arguments.command == "wizard":
        from .wizard import run_wizard
        return run_wizard(terminal)
    if arguments.command == "gui":
        from ..gui.app import launch
        return launch()
    if arguments.command == "scan":
        return command_scan(options_from_arguments(arguments), arguments.views, terminal)
    if arguments.command == "inspect":
        return command_inspect(arguments.paths, options_from_arguments(arguments), arguments.views, terminal)
    if arguments.command == "pdf":
        options = ScanOptions(file_types=["pdf"], save_files=False, output_dir=arguments.output_dir,
                              save_reports=arguments.save_reports)
        return command_inspect(arguments.paths, options, arguments.views, terminal, extensions={"pdf"})
    if arguments.command == "subdomains":
        return command_subdomains(arguments, terminal)
    terminal.log(overview_text())
    return 0
