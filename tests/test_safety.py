"""The promises the README makes: confined writes, no overwrites, escaped reports, guarded network."""

import datetime
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from metahunter.control import ScanEvents, run_parallel
from metahunter.extractors.exiftool import ExifTool, locate_executable
from metahunter.net.http import Blocked, Fetcher
from metahunter.net.urls import in_scope, normalise_target
from metahunter.output import OutputFolder, safe_filename, scan_folder_name
from metahunter.pdf.redaction import analyse_pdf
from metahunter.report.html_report import render_html
from tests.samples import badly_redacted_pdf, build_pdf, stream_object


class OutputFolderRules(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.parent = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_folder_is_only_created_on_first_write(self):
        folder = OutputFolder(self.parent, "scan")
        self.assertFalse(folder.created)
        folder.write_text("a.txt", "x")
        self.assertTrue((self.parent / "scan" / "a.txt").is_file())

    def test_never_overwrites(self):
        folder = OutputFolder(self.parent, "scan")
        first = folder.write_text("same.txt", "first")
        second = folder.write_text("same.txt", "second")
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_text(), "first")

    def test_names_from_websites_cannot_escape(self):
        folder = OutputFolder(self.parent, "scan")
        path = folder.write_text("../../outside/../../evil.txt", "x")
        self.assertIn(folder.root, path.parents)
        self.assertEqual(safe_filename("CON"), "_CON")

    def test_second_scan_in_same_minute_gets_new_folder(self):
        first, second = OutputFolder(self.parent, "scan"), OutputFolder(self.parent, "scan")
        first.write_text("a", "1")
        second.write_text("a", "2")
        self.assertNotEqual(first.root, second.root)

    def test_folder_name_format(self):
        name = scan_folder_name("https://blog.example.com/a/b.html?x=1", datetime.datetime(2026, 10, 14, 9, 5))
        self.assertEqual(name, "blog-example-com_a_b-html_141020260905")


class ReportEscaping(unittest.TestCase):
    def test_hostile_values_are_escaped_and_scripts_blocked(self):
        hostile = "<script>alert(1)</script>"
        report = {"target": "https://x.test/", "started": "", "finished": "", "metadata_engine": "", "stopped_early": False,
                  "counts": {}, "highlights": {"people": [(hostile, "Author", ["f.jpg"])]}, "subdomains": {}, "pages": [],
                  "files": [{"source": "javascript:alert(1)", "archived_from": "", "found_on": [], "status": "ok",
                             "saved_as": "", "duplicate_of": "", "embedded_files_saved": [], "pdf_check": None,
                             "format": "jpeg", "metadata": {"people": [["Author", hostile]]}, "gps": [], "warnings": []}]}
        page = render_html(report, {})
        self.assertNotIn("<script>", page)
        self.assertNotIn("href=\"javascript:", page)
        self.assertIn("Content-Security-Policy", page)


class Network(unittest.TestCase):
    def test_target_normalisation_and_scope(self):
        self.assertEqual(normalise_target("Example.com"), "https://example.com/")
        with self.assertRaises(ValueError):
            normalise_target("file:///etc/passwd")
        self.assertTrue(in_scope("https://www.example.com/x", "example.com", "host"))
        self.assertFalse(in_scope("https://cdn.example.com/x", "example.com", "host"))
        self.assertTrue(in_scope("https://cdn.example.com/x", "example.com", "domain"))
        self.assertFalse(in_scope("https://evil-example.com/x", "example.com", "domain"))

    def test_private_addresses_are_refused(self):
        with self.assertRaises(Blocked):
            Fetcher().check_allowed("http://127.0.0.1/")
        with self.assertRaises(Blocked):
            Fetcher().check_allowed("ftp://example.com/")


class RedactionChecker(unittest.TestCase):
    def test_finds_every_planted_problem(self):
        analysis = analyse_pdf(badly_redacted_pdf())
        found = {(finding.kind, finding.text) for finding in analysis.findings}
        self.assertIn(("covered", "SECRET 1234"), found)
        self.assertIn(("invisible", "Invisible words"), found)
        self.assertIn(("annotation", "Public line"), found)
        self.assertEqual(analysis.revisions[0].removed_lines, ["page 1: Old draft text"])


class InvisiblePaint(unittest.TestCase):
    def test_transparent_and_clipped_boxes_do_not_count_as_redactions(self):
        page = (b"BT /F1 12 Tf 72 700 Td (Visible name) Tj ET\n"
                b"/Clear gs 0 0 0 rg 70 696 120 16 re f\n"                    # fill opacity 0: invisible
                b"q 300 300 10 10 re W n 0 0 0 rg 70 696 120 16 re f Q\n")    # clipped away entirely
        pdf = build_pdf({
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> /ExtGState << /Clear << /ca 0 >> >> >> >>",
            4: stream_object(page),
            5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        })
        self.assertEqual(analyse_pdf(pdf).findings, [])


class ExifToolSetting(unittest.TestCase):
    def test_path_variants_resolve(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "exiftool-99.0" / "exiftool-99.0"
            folder.mkdir(parents=True)
            name = "exiftool.exe" if os.name == "nt" else "exiftool"
            executable = folder / name
            executable.write_text("not really exiftool")
            expected = executable.resolve()
            for setting in (str(executable), f"\"{executable}\"", f"'{executable}'", str(folder), str(folder.parent),
                            str(executable).replace(os.sep, "/"), str(executable).replace(os.sep, os.sep * 2)):
                self.assertEqual(locate_executable(setting, Path(temp)), expected, setting)
            self.assertEqual(locate_executable(f"exiftool-99.0/exiftool-99.0/{name}", Path(temp)), expected)
            tool, message = ExifTool.from_setting(str(executable), Path(temp))
            self.assertIsNone(tool)          # it is not a real ExifTool, so the built-in parsers are used
            self.assertIn("built-in", message)

    def test_empty_setting_means_built_in(self):
        self.assertIsNone(ExifTool.from_setting("", Path("."))[0])
        self.assertIsNone(ExifTool.from_setting("  \"\"  ", Path("."))[0])


class Stopping(unittest.TestCase):
    def test_stop_does_not_wait_for_slow_jobs(self):
        events = ScanEvents(on_log=lambda message: None)
        started = time.monotonic()
        stopper = threading.Timer(0.3, events.request_stop)
        stopper.start()
        run_parallel(list(range(20)), lambda job: time.sleep(30), 4, events)
        self.assertLess(time.monotonic() - started, 3)


if __name__ == "__main__":
    unittest.main()
