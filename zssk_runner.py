"""Run zssk_bulk.py as a child process for the web UI.

One run at a time: the browser flow must never race itself, so start()
refuses while a run is alive. Output is buffered in memory (a run prints a
few hundred lines at most) and read incrementally by the polling endpoint.
start_new_session=True puts the child in its own process group, so stop()
takes Firefox and xvfb down with it.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading


class RunnerBusy(RuntimeError):
    """A run is already in progress; the message names it."""


class Runner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lines: list[str] = []
        self._label = ""
        self._returncode: int | None = None

    def start(self, argv: list[str], label: str) -> None:
        with self._lock:
            if self._proc is not None and self._returncode is None:
                raise RunnerBusy(self._label)
            self._proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, start_new_session=True,
            )
            self._lines = []
            self._label = label
            self._returncode = None
            self._thread = threading.Thread(target=self._pump,
                                            args=(self._proc,), daemon=True)
            self._thread.start()

    def _pump(self, proc: subprocess.Popen) -> None:
        # Completion is published HERE, after the pipe is drained -- never read
        # from poll() in status(), or a fast-exiting child races its own tail:
        # poll() says finished while the last lines are still in the pipe.
        assert proc.stdout is not None
        for line in proc.stdout:
            with self._lock:
                self._lines.append(line.rstrip("\n"))
        code = proc.wait()
        with self._lock:
            self._returncode = code

    def status(self, offset: int = 0) -> dict:
        with self._lock:
            fresh = self._lines[offset:]
            return {
                "running": self._proc is not None and self._returncode is None,
                "returncode": self._returncode,
                "label": self._label,
                "lines": fresh,
                "next_offset": offset + len(fresh),
            }

    def stop(self, grace: float = 10.0) -> None:
        with self._lock:
            proc = self._proc
            thread = self._thread
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)
        if thread is not None:
            thread.join(timeout=5)
