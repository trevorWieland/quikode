"""Subtask persistence + lifecycle in the SQLite store."""

from __future__ import annotations

import json

from quikode.state import Store, SubtaskState


def _subs():
    return [
        {
            "subtask_id": "S-1",
            "title": "first",
            "depends_on": [],
            "files_to_touch": ["a.rs"],
            "boundary": "first only",
            "acceptance": ["compiles"],
            "notes": "",
        },
        {
            "subtask_id": "S-2",
            "title": "second",
            "depends_on": ["S-1"],
            "files_to_touch": ["b.rs", "c.rs"],
            "boundary": "second only",
            "acceptance": ["compiles", "exports X"],
            "notes": "needs S-1 first",
        },
    ]


def test_upsert_inserts_in_pending_state(tmp_path):
    s = Store(tmp_path / "q.db")
    s.upsert_pending("R-1")
    s.upsert_subtasks("R-1", _subs())
    rows = s.list_subtasks("R-1")
    assert len(rows) == 2
    assert rows[0]["subtask_id"] == "S-1"
    assert rows[0]["state"] == SubtaskState.PENDING.value
    assert json.loads(rows[1]["depends_on"]) == ["S-1"]
    assert json.loads(rows[1]["files_to_touch"]) == ["b.rs", "c.rs"]


def test_upsert_replaces_existing(tmp_path):
    s = Store(tmp_path / "q.db")
    s.upsert_pending("R-1")
    s.upsert_subtasks("R-1", _subs())
    # Replace with a single subtask
    s.upsert_subtasks("R-1", [_subs()[0]])
    rows = s.list_subtasks("R-1")
    assert len(rows) == 1
    assert rows[0]["subtask_id"] == "S-1"


def test_get_and_update(tmp_path):
    s = Store(tmp_path / "q.db")
    s.upsert_pending("R-1")
    s.upsert_subtasks("R-1", _subs())
    r = s.get_subtask("R-1", "S-2")
    assert r is not None and r["title"] == "second"
    s.update_subtask("R-1", "S-2", state=SubtaskState.DOING.value, last_error=None)
    assert s.get_subtask("R-1", "S-2")["state"] == SubtaskState.DOING.value


def test_increment_retries(tmp_path):
    s = Store(tmp_path / "q.db")
    s.upsert_pending("R-1")
    s.upsert_subtasks("R-1", _subs())
    assert s.increment_subtask_retries("R-1", "S-1") == 1
    assert s.increment_subtask_retries("R-1", "S-1") == 2
    assert s.get_subtask("R-1", "S-1")["retries"] == 2


def test_isolation_between_tasks(tmp_path):
    s = Store(tmp_path / "q.db")
    s.upsert_pending("R-1")
    s.upsert_pending("R-2")
    s.upsert_subtasks("R-1", _subs())
    s.upsert_subtasks("R-2", [_subs()[0]])
    assert len(s.list_subtasks("R-1")) == 2
    assert len(s.list_subtasks("R-2")) == 1


def test_agent_calls_subtask_id_field(tmp_path):
    s = Store(tmp_path / "q.db")
    s.upsert_pending("R-1")
    s.record_agent_call(
        "R-1",
        phase="subtask_doer",
        cli="opencode",
        model="x",
        rc=0,
        duration_s=1.0,
        tokens_used=10,
        subtask_id="S-1",
    )
    s.record_agent_call(
        "R-1", phase="planner", cli="claude", model="x", rc=0, duration_s=2.0, tokens_used=20
    )  # no subtask
    rows = list(
        s.conn.execute(
            "SELECT phase, subtask_id FROM agent_calls WHERE task_id = ? ORDER BY id",
            ("R-1",),
        )
    )
    assert rows[0]["subtask_id"] == "S-1"
    assert rows[1]["subtask_id"] is None


def test_subtask_state_transitions_workflow(tmp_path):
    """Realistic walk: pending → doing → checking → done."""
    s = Store(tmp_path / "q.db")
    s.upsert_pending("R-1")
    s.upsert_subtasks("R-1", _subs())
    sid = "S-1"
    for new_state in [SubtaskState.DOING, SubtaskState.CHECKING, SubtaskState.DONE]:
        s.update_subtask("R-1", sid, state=new_state.value)
    assert s.get_subtask("R-1", sid)["state"] == SubtaskState.DONE.value
