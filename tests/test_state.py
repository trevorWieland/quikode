"""SQLite state store tests."""

from __future__ import annotations

from pathlib import Path

from quikode.state import State, Store


def test_upsert_and_initial_state(tmp_path: Path):
    s = Store(tmp_path / "q.db")
    s.upsert_pending("R-1")
    row = s.get("R-1")
    assert row is not None
    assert row["state"] == State.PENDING.value
    assert row["do_check_retries"] == 0


def test_idempotent_upsert(tmp_path: Path):
    s = Store(tmp_path / "q.db")
    s.upsert_pending("R-1")
    # Second upsert shouldn't create a duplicate row or reset state
    s.transition("R-1", State.DOING_SUBTASK)
    s.upsert_pending("R-1")
    row = s.get("R-1")
    assert row["state"] == State.DOING_SUBTASK.value


def test_transition_records_log(tmp_path: Path):
    s = Store(tmp_path / "q.db")
    s.upsert_pending("R-1")
    s.transition("R-1", State.PROVISIONING, note="creating worktree")
    s.transition("R-1", State.PLANNING)
    log = list(s.conn.execute("SELECT to_state, note FROM state_log WHERE task_id = ? ORDER BY id", ("R-1",)))
    states = [r["to_state"] for r in log]
    assert states == [State.PENDING.value, State.PROVISIONING.value, State.PLANNING.value]


def test_completed_and_active_ids(tmp_path: Path):
    s = Store(tmp_path / "q.db")
    for nid in ("A", "B", "C", "D", "E"):
        s.upsert_pending(nid)
    s.transition("A", State.MERGED)
    s.transition("B", State.DOING_SUBTASK)
    s.transition("C", State.PENDING_CI)
    s.transition("D", State.BLOCKED)
    # E left PENDING
    assert s.completed_ids() == {"A"}
    # active = anything not in (PENDING, MERGED, BLOCKED, FAILED, ABORTED, AWAITING_MERGE)
    assert s.active_ids() == {"B"}


def test_increment(tmp_path: Path):
    s = Store(tmp_path / "q.db")
    s.upsert_pending("R-1")
    assert s.increment("R-1", "do_check_retries") == 1
    assert s.increment("R-1", "do_check_retries") == 2
    row = s.get("R-1")
    assert row["do_check_retries"] == 2


def test_set_field(tmp_path: Path):
    s = Store(tmp_path / "q.db")
    s.upsert_pending("R-1")
    s.set_field("R-1", branch="quikode/r-1-abc", pr_number=42)
    row = s.get("R-1")
    assert row["branch"] == "quikode/r-1-abc"
    assert row["pr_number"] == 42


def test_artifacts(tmp_path: Path):
    s = Store(tmp_path / "q.db")
    s.upsert_pending("R-1")
    s.add_artifact("R-1", "planner_output", "the plan goes here")
    rows = list(s.conn.execute("SELECT kind, content FROM artifacts WHERE task_id = ?", ("R-1",)))
    assert len(rows) == 1
    assert rows[0]["kind"] == "planner_output"
    assert rows[0]["content"] == "the plan goes here"
