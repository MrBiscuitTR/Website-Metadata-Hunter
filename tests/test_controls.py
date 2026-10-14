"""Ctrl+C in the terminal: pause menu, stop, restart and quit, driven by a real KeyboardInterrupt."""

import _thread
import builtins
import io
import threading
import time
import unittest
from unittest import mock

from metahunter.cli.main import run_controlled
from metahunter.cli.terminal import TerminalOutput
from metahunter.control import Cancelled


def slow_job(events):
    """Pretends to work for up to 10 seconds, honouring pause and stop like a real scan."""
    for _ in range(100):
        try:
            events.checkpoint()
        except Cancelled:
            return "stopped" if not events.aborted() else "aborted"
        time.sleep(0.1)
    return "finished"


class CtrlC(unittest.TestCase):
    def run_with_answers(self, answers):
        terminal = TerminalOutput(io.StringIO())
        threading.Timer(0.3, _thread.interrupt_main).start()
        started = time.monotonic()
        with mock.patch.object(builtins, "input", side_effect=answers):
            outcome = run_controlled(slow_job, terminal)
        return outcome, time.monotonic() - started

    def test_stop_writes_and_returns_quickly(self):
        (status, result), elapsed = self.run_with_answers(["s"])
        self.assertEqual((status, result), ("done", "stopped"))
        self.assertLess(elapsed, 2)

    def test_quit_is_immediate(self):
        (status, _), elapsed = self.run_with_answers(["q"])
        self.assertEqual(status, "quit")
        self.assertLess(elapsed, 1)

    def test_restart_and_unknown_answer(self):
        (status, _), elapsed = self.run_with_answers(["x", "r"])
        self.assertEqual(status, "restart")
        self.assertLess(elapsed, 1)

    def test_second_ctrl_c_at_the_menu_quits(self):
        (status, _), _ = self.run_with_answers(KeyboardInterrupt())
        self.assertEqual(status, "quit")


if __name__ == "__main__":
    unittest.main()
