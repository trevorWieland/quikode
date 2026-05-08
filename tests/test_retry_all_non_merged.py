"""Plan 38 PR-A: `qk retry --all-non-merged` bulk-retry test."""

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
                        "id": nid,
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
                    for nid in ("R-001", "R-002", "R-003", "R-004")
                ],
            }
        )
    )


def _seed_tasks(tmp_path) -> None:
    """Seed four tasks at four different states."""
    store = Store(tmp_path / ".quikode" / "quikode.db")
    for tid in ("R-001", "R-002", "R-003", "R-004"):
        store.upsert_pending(tid)
    # R-001 → BLOCKED  (should be retried)
    store.transition("R-001", State.BLOCKED, last_error="x")
    # R-002 → FAILED   (should be retried)
    store.transition("R-002", State.FAILED, last_error="y")
    # R-003 → MERGED   (should NOT be retried — terminal merged)
    store.transition("R-003", State.PENDING_CI)
    store.transition("R-003", State.AWAITING_REVIEW)
    store.transition("R-003", State.MERGED)
    # R-004 stays PENDING (already retryable; bulk retry should still pick it up
    # because the rule is "everything not merged/merge_node_retired").
    store.conn.close()


def test_retry_all_non_merged_resets_blocked_and_failed_but_not_merged(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    _seed_tasks(tmp_path)
    result = CliRunner().invoke(app, ["retry", "--all-non-merged"])
    assert result.exit_code == 0, result.output
    # R-001 (BLOCKED) + R-002 (FAILED) get retried. R-003 (MERGED) and R-004
    # (PENDING — not yet planned, retry_task event is N/A) are skipped.
    assert "2 task(s)" in result.output
    store = Store(tmp_path / ".quikode" / "quikode.db")
    assert store.get("R-001")["state"] == State.PENDING.value
    assert store.get("R-002")["state"] == State.PENDING.value
    assert store.get("R-003")["state"] == State.MERGED.value  # untouched
    assert store.get("R-004")["state"] == State.PENDING.value  # was already pending
    store.conn.close()


def test_retry_all_non_merged_rejects_positional_task_id(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    _seed_tasks(tmp_path)
    result = CliRunner().invoke(app, ["retry", "R-001", "--all-non-merged"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_retry_without_args_or_flag_fails(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    _seed_tasks(tmp_path)
    result = CliRunner().invoke(app, ["retry"])
    assert result.exit_code == 2


def test_retry_all_non_merged_noop_when_all_merged(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    for tid in ("R-001", "R-002"):
        store.upsert_pending(tid)
        store.transition(tid, State.PENDING_CI)
        store.transition(tid, State.AWAITING_REVIEW)
        store.transition(tid, State.MERGED)
    store.conn.close()
    result = CliRunner().invoke(app, ["retry", "--all-non-merged"])
    assert result.exit_code == 0, result.output
    assert "no non-merged retryable tasks" in result.output
