"""Phase 2: `qk reset-retries <task> [<subtask>]` CLI behavior.

Pinned contract:
- Refuses (exit 2) on tasks not in BLOCKED or FAILED.
- Without subtask_id: zeroes retries on every blocked subtask + flips state
  to pending; clears last_error/transient_retries/flatline_count.
- With subtask_id: targets only that subtask (must exist).
- Unknown task → exit 1.
- Unknown subtask on a known task → exit 1.
- No-op on a BLOCKED task with no blocked subtasks (rare but valid) → exit 0
  with a yellow notice.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from quikode.cli import app
from quikode.config_template import DEFAULT_CONFIG_TOML
from quikode.state import State, Store


def _bootstrap(tmp_path):
    qkdir = tmp_path / ".quikode"
    qkdir.mkdir()
    (qkdir / "config.toml").write_text(
        DEFAULT_CONFIG_TOML.format(repo_path=str(tmp_path), dag_path=str(tmp_path / "dag.json"))
    )
    (tmp_path / "dag.json").write_text(
        json.dumps(
            {
                "schema": "test",
                "milestones": [{"id": "M-1", "title": "x", "goal": "x", "status": "planned"}],
                "nodes": [
                    {
                        "id": "R-001",
                        "kind": "behavior",
                        "milestone": "M-1",
                        "title": "x",
                        "scope": "x",
                        "depends_on": [],
                        "completes_behaviors": [],
                        "supports_behaviors": [],
                        "boundary_with_neighbors": "",
                        "expected_evidence": [],
                        "playbook": [],
                        "rationale": "",
                        "risks": [],
                    }
                ],
            }
        )
    )


def _put_task_in_blocked(tmp_path, retries: int = 50):
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.BLOCKED, last_error="exhausted hard ceiling of 50 attempts")
    store.upsert_subtasks(
        "R-001",
        [
            {"subtask_id": "S-01", "title": "first", "acceptance": ["a"]},
            {"subtask_id": "S-02", "title": "second", "acceptance": ["b"]},
        ],
    )
    store.update_subtask("R-001", "S-01", state="done", retries=2)
    store.update_subtask(
        "R-001",
        "S-02",
        state="blocked",
        retries=retries,
        last_error="container vanished",
    )
    store.conn.close()


def test_resets_all_blocked_subtasks_by_default(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    _put_task_in_blocked(tmp_path)

    result = CliRunner().invoke(app, ["reset-retries", "R-001"])
    assert result.exit_code == 0, result.output
    assert "reset R-001/S-02" in result.output
    # The done subtask is NOT touched — only `state == 'blocked'` rows.
    assert "S-01" not in result.output

    store = Store(tmp_path / ".quikode" / "quikode.db")
    s2 = store.get_subtask("R-001", "S-02")
    assert s2["retries"] == 0
    assert s2["state"] == "pending"
    assert s2["last_error"] is None
    s1 = store.get_subtask("R-001", "S-01")
    assert s1["state"] == "done"  # untouched
    assert s1["retries"] == 2  # untouched
    store.conn.close()


def test_targets_specific_subtask(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    _put_task_in_blocked(tmp_path)

    result = CliRunner().invoke(app, ["reset-retries", "R-001", "S-02"])
    assert result.exit_code == 0, result.output
    store = Store(tmp_path / ".quikode" / "quikode.db")
    assert store.get_subtask("R-001", "S-02")["retries"] == 0
    store.conn.close()


def test_refuses_on_running_task(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.DOING_SUBTASK)
    store.conn.close()

    result = CliRunner().invoke(app, ["reset-retries", "R-001"])
    assert result.exit_code == 2, result.output
    assert "doing_subtask" in result.output


def test_unknown_task_exits_one(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["reset-retries", "R-DOES-NOT-EXIST"])
    assert result.exit_code == 1
    assert "no such task" in result.output


def test_unknown_subtask_exits_one(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    _put_task_in_blocked(tmp_path)

    result = CliRunner().invoke(app, ["reset-retries", "R-001", "S-NOPE"])
    assert result.exit_code == 1
    assert "no subtask" in result.output


def test_no_blocked_subtasks_is_noop(tmp_path, monkeypatch):
    """A task in BLOCKED state but with no subtask in 'blocked' status —
    rare but valid (e.g., manually transitioned via FSM). Exit 0 with a
    yellow notice rather than misleading green output."""
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.BLOCKED)
    store.upsert_subtasks("R-001", [{"subtask_id": "S-01", "title": "x", "acceptance": ["a"]}])
    store.update_subtask("R-001", "S-01", state="done", retries=0)
    store.conn.close()

    result = CliRunner().invoke(app, ["reset-retries", "R-001"])
    assert result.exit_code == 0, result.output
    assert "no blocked subtasks" in result.output
