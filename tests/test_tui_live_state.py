"""Plan 38 PR-C: TUI in-flight status reflects observed reality.

The trigger was R-0003 showing "running per-subtask doer" while the
doer call had already returned (rc=124, timeout) and the worker was in
a re-prompt cycle. The TUI was lying because "running" was synthesized
from FSM state alone — there was no per-task signal for "is the agent
call currently in flight."

Plan 38 PR-C added a start-marker to `agent_calls` (an INSERT before
the agent invokes; the worker UPDATEs the same row at return) and a
`Store.agent_in_flight_status(task_id)` helper that returns one of
`("running", phase, age, None)` / `("idle", last_phase, age, last_rc)`
/ `("never", None, None, None)`. The TUI's detail panel renders one
of three structured lines off that helper.

These tests exercise the helper + the rendering against a small
in-memory store fixture.
"""

from __future__ import annotations

import time

from quikode.state import Store
from quikode.tui.controllers.store_polls import _detail_agent_in_flight
from quikode.tui.widgets.detail_panel import DetailSnapshot, _agent_in_flight_line


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "q.db")
    s.upsert_pending("R-1")
    return s


# ----- helper: store-level in-flight status -----


def test_agent_in_flight_status_never(tmp_path):
    s = _store(tmp_path)
    status, phase, age, rc = s.agent_in_flight_status("R-1")
    assert status == "never"
    assert phase is None
    assert age is None
    assert rc is None


def test_agent_in_flight_status_running_after_start_marker(tmp_path):
    s = _store(tmp_path)
    call_id = s.record_agent_call_started(
        "R-1",
        phase="subtask_doer",
        cli="json_agent",
        model="GLM-5.1-zai",
        subtask_id="S-01",
    )
    assert isinstance(call_id, int)
    # Pretend now is 5 seconds after the start.
    now = time.time() + 5
    status, phase, age, rc = s.agent_in_flight_status("R-1", now=now)
    assert status == "running"
    assert phase == "subtask_doer"
    assert age is not None and 4.5 <= age <= 5.5
    assert rc is None


def test_agent_in_flight_status_idle_after_finish(tmp_path):
    s = _store(tmp_path)
    call_id = s.record_agent_call_started(
        "R-1",
        phase="subtask_doer",
        cli="json_agent",
        model="GLM-5.1-zai",
        subtask_id="S-01",
    )
    s.record_agent_call_finished(
        call_id,
        rc=124,
        duration_s=1305.0,
        tokens_input=12000,
        tokens_output=2400,
        cost_usd=0.42,
    )
    now = time.time() + 30
    status, phase, age, rc = s.agent_in_flight_status("R-1", now=now)
    assert status == "idle"
    assert phase == "subtask_doer"
    # last returned ~30s ago
    assert age is not None and 25.0 <= age <= 35.0
    assert rc == 124


def test_agent_in_flight_picks_latest_row_only(tmp_path):
    """When a checker call follows a doer call, only the newest row matters."""
    s = _store(tmp_path)
    call_a = s.record_agent_call_started("R-1", phase="subtask_doer", cli="json_agent", model="m")
    s.record_agent_call_finished(call_a, rc=0, duration_s=120.0)
    s.record_agent_call_started("R-1", phase="subtask_checker", cli="json_agent", model="m")
    status, phase, _, rc = s.agent_in_flight_status("R-1")
    assert status == "running"
    assert phase == "subtask_checker"
    assert rc is None


def test_record_agent_call_single_call_path_marks_finished(tmp_path):
    """The single-INSERT `record_agent_call` (the single-call API used
    by callers that don't yet split start/finish) must report idle,
    not running — ts and started_at align so rc IS NOT NULL."""
    s = _store(tmp_path)
    s.record_agent_call(
        "R-1",
        phase="planner",
        cli="json_agent",
        model="gpt-5.5",
        rc=0,
        duration_s=42.0,
        tokens_used=None,
    )
    status, phase, _, rc = s.agent_in_flight_status("R-1")
    assert status == "idle"
    assert phase == "planner"
    assert rc == 0


# ----- TUI controller helper that mirrors the Store helper -----


def test_detail_agent_in_flight_idle(tmp_path):
    """The poller's read-only-conn helper produces the same shape."""
    s = _store(tmp_path)
    call_id = s.record_agent_call_started("R-1", phase="subtask_doer", cli="json_agent", model="m")
    s.record_agent_call_finished(call_id, rc=124, duration_s=1305.0)
    now = time.time() + 30
    status, phase, age, rc = _detail_agent_in_flight(s.conn, "R-1", now=now)
    assert status == "idle"
    assert phase == "subtask_doer"
    assert age is not None and 25.0 <= age <= 35.0
    assert rc == 124


def test_detail_agent_in_flight_running(tmp_path):
    s = _store(tmp_path)
    s.record_agent_call_started("R-1", phase="subtask_checker", cli="json_agent", model="m")
    now = time.time() + 12
    status, phase, age, _ = _detail_agent_in_flight(s.conn, "R-1", now=now)
    assert status == "running"
    assert phase == "subtask_checker"
    assert age is not None and 11.0 <= age <= 13.0


# ----- end-to-end: phase line for a returned doer call shows idle, not running -----


def test_phase_line_idle_for_returned_doer_call_in_doing_subtask():
    """Trigger scenario: FSM still in `doing_subtask` but the doer call
    has returned. The structured line must say "idle ... rc=124", NOT
    a synthesized "running ...". Plan 38 PR-C closes this lie."""
    snap = DetailSnapshot(
        task_id="R-0003",
        title="example",
        task_state="doing_subtask",
        in_state_for="30m59s",
        last_worktree_edit="12s",
        agent_in_flight_status="idle",
        agent_in_flight_phase="subtask_doer",
        agent_in_flight_age_s=30.0,
        agent_in_flight_last_rc=124,
    )
    line = _agent_in_flight_line(snap)
    assert line is not None
    assert "idle" in line
    assert "subtask_doer" in line
    assert "rc=124" in line
    assert "30s ago" in line
    # Crucially, the synthesized "running per-subtask doer" string is gone.
    assert "running" not in line.lower()


def test_phase_line_running_when_call_in_flight():
    snap = DetailSnapshot(
        task_id="R-0001",
        task_state="doing_subtask",
        agent_in_flight_status="running",
        agent_in_flight_phase="subtask_doer",
        agent_in_flight_age_s=12.0,
    )
    line = _agent_in_flight_line(snap)
    assert line is not None
    assert "in-flight" in line
    assert "subtask_doer" in line
    assert "12s" in line


def test_phase_line_no_call_yet():
    snap = DetailSnapshot(task_id="R-0001", agent_in_flight_status="never")
    line = _agent_in_flight_line(snap)
    assert line is not None
    assert "no agent call yet" in line
