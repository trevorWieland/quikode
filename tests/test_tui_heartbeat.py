"""Tests for TUI orchestrator_control heartbeat-aware status."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from quikode.tui.controllers import orchestrator_control as oc


def _make_workspace(tmp_path: Path) -> Path:
    (tmp_path / ".quikode" / "logs").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_pid(ws: Path, pid: int) -> None:
    (ws / ".quikode" / "orchestrator.pid").write_text(f"{pid}@{time.time():.0f}\n")


def _write_hb(ws: Path, age_s: float = 0.0, **fields) -> None:
    payload = {"ts": time.time() - age_s, "in_flight": 0, "pending_ci": 0}
    payload.update(fields)
    (ws / ".quikode" / "orchestrator.heartbeat").write_text(json.dumps(payload))


def test_status_no_heartbeat_file(tmp_path):
    ws = _make_workspace(tmp_path)
    _write_pid(ws, os.getpid())
    s = oc.status(ws)
    assert s.running is True
    assert s.heartbeat_age_s is None
    assert s.heartbeat_data is None
    assert s.heartbeat_stale is False


def test_status_fresh_heartbeat(tmp_path):
    ws = _make_workspace(tmp_path)
    _write_pid(ws, os.getpid())
    _write_hb(ws, age_s=2.0, in_flight=3, pending_ci=1)
    s = oc.status(ws)
    assert s.running is True
    assert s.heartbeat_age_s is not None
    assert s.heartbeat_age_s < 5
    assert s.heartbeat_data is not None
    assert s.heartbeat_data["in_flight"] == 3
    assert s.heartbeat_stale is False


def test_status_stale_heartbeat(tmp_path):
    ws = _make_workspace(tmp_path)
    _write_pid(ws, os.getpid())
    _write_hb(ws, age_s=120.0)  # 2 minutes old, default threshold 30s
    s = oc.status(ws)
    assert s.running is True
    assert s.heartbeat_stale is True
    assert s.heartbeat_age_s is not None
    assert s.heartbeat_age_s > 30


def test_status_custom_staleness_threshold(tmp_path):
    ws = _make_workspace(tmp_path)
    _write_pid(ws, os.getpid())
    _write_hb(ws, age_s=20.0)
    # Threshold tighter than default — same heartbeat now considered stale.
    s = oc.status(ws, staleness_s=10)
    assert s.heartbeat_stale is True
    s2 = oc.status(ws, staleness_s=60)
    assert s2.heartbeat_stale is False


def test_status_invalid_heartbeat_treated_as_missing(tmp_path):
    ws = _make_workspace(tmp_path)
    _write_pid(ws, os.getpid())
    (ws / ".quikode" / "orchestrator.heartbeat").write_text("not-json{{{")
    s = oc.status(ws)
    assert s.heartbeat_data is None
    assert s.heartbeat_age_s is None
    assert s.heartbeat_stale is False


def test_status_running_field_independent_of_heartbeat(tmp_path):
    """Process alive + stale heartbeat → running stays True (TUI surfaces stale separately)."""
    ws = _make_workspace(tmp_path)
    _write_pid(ws, os.getpid())
    _write_hb(ws, age_s=999.0)
    s = oc.status(ws)
    assert s.running is True
    assert s.heartbeat_stale is True
