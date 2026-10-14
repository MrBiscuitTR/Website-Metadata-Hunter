"""How a running scan reports back and is controlled: log, progress, pause, stop, abort.

stop  -- finish quickly and still write the reports with what was collected
abort -- finish quickly and write nothing more (used for "restart" and "quit now")
pause -- no new request or file is started until resume() is called
"""

import datetime
import queue
import threading

GRACE_SECONDS = 1.5   # how long a stop waits for work already in flight before leaving it behind


class Cancelled(Exception):
    """Raised inside workers once the scan has been stopped."""


class ScanEvents:
    def __init__(self, on_log=print, on_progress=None):
        self._on_log = on_log
        self._on_progress = on_progress
        self._stop = threading.Event()
        self._abort = threading.Event()
        self._running = threading.Event()
        self._running.set()
        self.lines = []

    def log(self, message):
        self.lines.append(f"{datetime.datetime.now():%H:%M:%S} {message}")
        self._on_log(message)

    def progress(self, done, total, label=""):
        if self._on_progress:
            self._on_progress(done, total, label)

    # ---- control, callable from any thread
    def pause(self):
        self._running.clear()

    def resume(self):
        self._running.set()

    def request_stop(self):
        self._stop.set()
        self._running.set()          # wake paused workers so they can notice the stop

    def abort(self):
        self._abort.set()
        self.request_stop()

    def paused(self):
        return not self._running.is_set()

    def stop_requested(self):
        return self._stop.is_set()

    def aborted(self):
        return self._abort.is_set()

    def checkpoint(self):
        """Call before starting any new piece of work: waits while paused, raises once stopped."""
        while not self._running.wait(0.2):
            pass
        if self._stop.is_set():
            raise Cancelled()


def run_parallel(jobs, worker, threads, events, on_result=None):
    """Run worker(job) on daemon threads; return results of finished jobs.

    Daemon threads never keep the program alive, so a stop (or Ctrl+C) is never held
    up by a slow download: after GRACE_SECONDS, unfinished work is simply left behind.
    """
    pending = queue.Queue()
    for job in jobs:
        pending.put(job)
    finished = queue.Queue()

    def loop():
        while True:
            try:
                events.checkpoint()
                job = pending.get_nowait()
            except (Cancelled, queue.Empty):
                return
            try:
                finished.put(("ok", worker(job)))
            except Cancelled:
                finished.put(("cancelled", None))
            except Exception as error:   # one bad job must not take the whole scan down
                finished.put(("error", error))

    workers = [threading.Thread(target=loop, daemon=True) for _ in range(max(1, min(threads, len(jobs))))]
    for thread in workers:
        thread.start()
    results, outstanding, stop_deadline = [], len(jobs), None
    while outstanding:
        try:
            kind, value = finished.get(timeout=0.1)
        except queue.Empty:
            if events.stop_requested():
                stop_deadline = stop_deadline or datetime.datetime.now() + datetime.timedelta(seconds=GRACE_SECONDS)
                idle = not any(thread.is_alive() for thread in workers)
                if idle or datetime.datetime.now() > stop_deadline:
                    break
            continue
        outstanding -= 1
        if kind == "ok":
            results.append(value)
            if on_result:
                on_result(value, len(results), len(jobs))
        elif kind == "error":
            events.log(f"  internal error: {type(value).__name__}: {value}")
    return results
