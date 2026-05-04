"""`quikode unblock <id>` prints intervention info for a BLOCKED task.

Companion command to `quikode resume`: surfaces the worktree path, branch,
PR url, and instructions for the user to investigate locally. Does not
mutate state — that's `quikode resume`'s job.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from quikode.cli import app
from quikode.config import DEFAULT_CONFIG_TOML
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


def test_unblock_prints_context_for_blocked_task(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.transition(
        "R-001",
        State.BLOCKED,
        branch="quikode/r-001-abc",
        worktree_path=str(tmp_path / "wt"),
        pr_url="https://github.com/foo/bar/pull/42",
        last_error="progress check flatlined twice",
    )
    store.upsert_subtasks(
        "R-001",
        [{"subtask_id": "S-07-mcp-tools", "title": "mcp", "acceptance": ["a"]}],
    )
    store.update_subtask("R-001", "S-07-mcp-tools", state="blocked")
    store.conn.close()

    result = CliRunner().invoke(app, ["unblock", "R-001"])
    assert result.exit_code == 0, result.output
    out = result.output
    # Subtask context shown.
    assert "S-07-mcp-tools" in out
    # Worktree, branch, PR all surface.
    assert str(tmp_path / "wt") in out
    assert "quikode/r-001-abc" in out
    assert "https://github.com/foo/bar/pull/42" in out
    # Reason text shown.
    assert "flatlined" in out
    # Resume command hint present.
    assert "quikode resume R-001" in out


def test_unblock_unknown_task_exits_nonzero(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["unblock", "R-DOES-NOT-EXIST"])
    assert result.exit_code != 0
    assert "no task" in result.output


def test_unblock_non_blocked_warns_but_still_prints(tmp_path, monkeypatch):
    """If the task is in some other state, we still print context (with a
    warning) — useful for debugging stuck tasks regardless of state."""
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.transition(
        "R-001",
        State.AWAITING_MERGE,
        branch="quikode/r-001-xyz",
        worktree_path=str(tmp_path / "wt2"),
    )
    store.conn.close()

    result = CliRunner().invoke(app, ["unblock", "R-001"])
    assert result.exit_code == 0, result.output
    assert "not 'blocked'" in result.output
    # Still prints worktree info.
    assert "quikode/r-001-xyz" in result.output
