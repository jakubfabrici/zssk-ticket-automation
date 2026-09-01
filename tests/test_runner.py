import sys
import time

import pytest

from zssk_runner import Runner, RunnerBusy


def drain(runner, timeout=10.0):
    lines, offset = [], 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = runner.status(offset)
        lines += status["lines"]
        offset = status["next_offset"]
        if not status["running"] and status["returncode"] is not None:
            return lines, status["returncode"]
        time.sleep(0.05)
    raise AssertionError("runner did not finish in time")


def test_streams_output_and_exit_code():
    runner = Runner()
    runner.start([sys.executable, "-u", "-c",
                  "for i in range(3): print('line', i)"], "test")
    lines, code = drain(runner)
    assert lines == ["line 0", "line 1", "line 2"] and code == 0


def test_refuses_second_run_while_alive():
    runner = Runner()
    runner.start([sys.executable, "-u", "-c",
                  "import time; time.sleep(30)"], "prvy")
    try:
        with pytest.raises(RunnerBusy):
            runner.start([sys.executable, "-c", "pass"], "druhy")
    finally:
        runner.stop(grace=2)


def test_stop_terminates_the_process_group():
    runner = Runner()
    runner.start([sys.executable, "-u", "-c",
                  "import time; print('up', flush=True); time.sleep(60)"], "kill")
    time.sleep(0.3)
    runner.stop(grace=2)
    status = runner.status()
    assert not status["running"] and status["returncode"] != 0


def test_fresh_runner_reports_idle():
    status = Runner().status()
    assert status == {"running": False, "returncode": None, "label": "",
                      "lines": [], "next_offset": 0}


def test_stop_tolerates_already_reaped_process(monkeypatch):
    runner = Runner()
    runner.start([sys.executable, "-u", "-c",
                  "import time; time.sleep(30)"], "race")
    time.sleep(0.2)
    import zssk_runner as zr

    def raising_killpg(pgid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(zr.os, "killpg", raising_killpg)
    runner.stop(grace=1)          # must not raise
    monkeypatch.undo()
    runner.stop(grace=2)          # real cleanup so the child does not linger
