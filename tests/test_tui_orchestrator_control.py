"""TUI v1 step 6 — orchestrator subprocess + PID file management.

Uses a dummy `sleep` subprocess so we don't actually spawn quikode run.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from quikode.tui.controllers import orchestrator_control as oc

SLEEP_CMD = [sys.executable, "-c", "import time; time.sleep(30)"]


def _make_workspace(tmp_path: Path) -> Path:
    (tmp_path / ".quikode" / "logs").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_status_no_pid_file(tmp_path):
    s = oc.status(_make_workspace(tmp_path))
    assert s.running is False
    assert s.pid is None


def test_status_stale_pid_file_is_cleaned(tmp_path):
    ws = _make_workspace(tmp_path)
    pid_file = ws / ".quikode" / "orchestrator.pid"
    pid_file.write_text("99999999@1700000000\n")  # almost certainly dead
    s = oc.status(ws)
    assert s.running is False
    assert not pid_file.exists()  # cleaned


def test_status_invalid_pid_file_returns_not_running(tmp_path):
    ws = _make_workspace(tmp_path)
    pid_file = ws / ".quikode" / "orchestrator.pid"
    pid_file.write_text("not-a-number\n")
    s = oc.status(ws)
    assert s.running is False


def test_spawn_writes_pid_file_and_status_reflects_running(tmp_path, monkeypatch):
    ws = _make_workspace(tmp_path)
    monkeypatch.setenv("QUIKODE_BIN", sys.executable)

    # Build a custom argv that ignores the "run" subcommand quikode_run_argv adds.
    # Easier: monkeypatch _quikode_run_argv to a sleep invocation.
    monkeypatch.setattr(oc, "_quikode_run_argv", lambda extra: SLEEP_CMD)

    s = oc.spawn(ws)
    try:
        assert s.running
        assert s.pid is not None
        assert (ws / ".quikode" / "orchestrator.pid").exists()
        # status() agrees
        s2 = oc.status(ws)
        assert s2.running and s2.pid == s.pid
        # spawn-while-running raises
        with pytest.raises(FileExistsError):
            oc.spawn(ws)
    finally:
        # Cleanup the sleep process so the test doesn't leak.
        if s.pid:
            try:
                os.kill(s.pid, 9)
            except ProcessLookupError:
                pass


def test_force_quit_kills_running_proc(tmp_path, monkeypatch):
    ws = _make_workspace(tmp_path)
    monkeypatch.setattr(oc, "_quikode_run_argv", lambda extra: SLEEP_CMD)
    s = oc.spawn(ws)
    assert s.running
    ok = oc.force_quit(ws)
    assert ok
    # PID file is removed
    assert not (ws / ".quikode" / "orchestrator.pid").exists()
    # Give the kernel a moment to reap
    time.sleep(0.1)
    assert oc.status(ws).running is False


def test_stop_sends_sigterm_and_proc_dies(tmp_path, monkeypatch):
    ws = _make_workspace(tmp_path)
    monkeypatch.setattr(oc, "_quikode_run_argv", lambda extra: SLEEP_CMD)
    s = oc.spawn(ws)
    assert s.running
    ok = oc.stop(ws, timeout_s=5)
    assert ok
    assert not (ws / ".quikode" / "orchestrator.pid").exists()


def test_stop_no_op_when_not_running(tmp_path):
    assert oc.stop(_make_workspace(tmp_path)) is False


def test_force_quit_no_op_when_not_running(tmp_path):
    assert oc.force_quit(_make_workspace(tmp_path)) is False


def test_parting_status_message_none_when_stopped(tmp_path):
    msg = oc.parting_status_message(_make_workspace(tmp_path))
    assert msg is None


def test_parting_status_message_when_running(tmp_path, monkeypatch):
    ws = _make_workspace(tmp_path)
    monkeypatch.setattr(oc, "_quikode_run_argv", lambda extra: SLEEP_CMD)
    s = oc.spawn(ws)
    try:
        msg = oc.parting_status_message(ws)
        assert msg is not None
        assert "still running" in msg
        assert str(s.pid) in msg
        assert "orchestrator.pid" in msg
    finally:
        oc.force_quit(ws)
