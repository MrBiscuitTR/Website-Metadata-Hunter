"""Entry point: parse the command line and run the chosen command."""

import datetime
import json
import threading

from ..net.http import FetchError
from ..options import ScanOptions
from ..output import OutputFolder, scan_folder_name
from ..runner import ScanEvents, inspect_local, lookup_subdomains, run_scan, scans_folder
from .arguments import build_parser, options_from_arguments
from .overview import overview_text
from .terminal import TerminalOutput


def run_with_interrupt(job, events, terminal):
    """Run a long job in a worker thread so Ctrl+C can ask it to stop cleanly."""
    outcome = {}

    def work():
        try:
            outcome["result"] = job()
        except Exception as error:   # reported to the user below, with its type
            outcome["error"] = error

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    while worker.is_alive():
        try:
            worker.join(0.2)
        except KeyboardInterrupt:
            if events.stop_requested():
                terminal.log("Stopping right away. Reports for this run will be missing.")
                raise
            events.request_stop()
            terminal.log("Stopping after the files in progress; reports will still be written. Ctrl+C again to quit now.")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")


def finish_message(result, terminal):
    terminal.log("")
    terminal.log(f"Done. Open {result.report_files['html']}")
    terminal.log(f"      or read {result.report_files['summary']}")


def command_scan(options, terminal):
    problems = options.problems()
    if problems:
        terminal.log("Cannot start: " + "; ".join(problems))
        return 2
    events = ScanEvents(terminal.log, terminal.progress)
    try:
        result = run_with_interrupt(lambda: run_scan(options, events), events, terminal)
    except (ValueError, FetchError) as error:
        terminal.log(f"Cannot scan {options.target}: {error}")
        return 1
    finish_message(result, terminal)
    return 0


def command_inspect(paths, options, terminal, extensions=None):
    events = ScanEvents(terminal.log, terminal.progress)
    try:
        result = run_with_interrupt(lambda: inspect_local(paths, options, events, extensions), events, terminal)
    except FileNotFoundError as error:
        terminal.log(str(error))
        return 1
    finish_message(result, terminal)
    return 0


def command_subdomains(arguments, terminal):
    options = ScanOptions(output_dir=arguments.output_dir)
    events = ScanEvents(terminal.log, terminal.progress)
    try:
        found = run_with_interrupt(lambda: lookup_subdomains(arguments.domain, arguments.sources, options, events),
                                   events, terminal)
    except ValueError as error:
        terminal.log(str(error))
        return 1
    for name, info in found.items():
        terminal.log(f"  {name:50} {'resolves' if info['resolves'] else 'no DNS record':14} {', '.join(info['sources'])}")
    folder = OutputFolder.create(scans_folder(options), scan_folder_name(f"subdomains_{arguments.domain}", datetime.datetime.now()))
    folder.write_text("subdomains.txt", "\n".join(found) + "\n")
    folder.write_text("subdomains.json", json.dumps(found, indent=2))
    terminal.log(f"{len(found)} subdomains. Saved to {folder.root}")
    return 0


def main(argv):
    terminal = TerminalOutput()
    parser, commands = build_parser()
    if not argv:
        terminal.log(overview_text())
        from .wizard import run_wizard
        try:
            if input("Start the guided wizard now? [Y/n] ").strip().lower() in ("", "y", "yes"):
                return run_wizard(terminal)
        except (EOFError, KeyboardInterrupt):
            terminal.log("")
        return 0
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
        return command_scan(options_from_arguments(arguments), terminal)
    if arguments.command == "inspect":
        return command_inspect(arguments.paths, options_from_arguments(arguments), terminal)
    if arguments.command == "pdf":
        options = ScanOptions(file_types=["pdf"], save_files=False, output_dir=arguments.output_dir)
        return command_inspect(arguments.paths, options, terminal, extensions={"pdf"})
    if arguments.command == "subdomains":
        return command_subdomains(arguments, terminal)
    terminal.log(overview_text())
    return 0
