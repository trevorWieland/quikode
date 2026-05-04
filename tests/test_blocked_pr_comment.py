"""Verify `_mark_subtask_blocked` posts a PR comment + label when a PR is open.

When the v3 progress-check agent flatlines (or the hard ceiling burns out),
the worker marks the subtask BLOCKED. Phase D adds an operator-facing
surface: a PR comment with the three intervention paths + a `quikode:blocked`
label, so the human reviewer sees the failure on the PR they were polling.

The comment + label are best-effort: subprocess failures must not raise
through `_mark_subtask_blocked` (the BLOCKED state record is the contract;
the PR surfacing is a courtesy).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from quikode.agents.progress import ProgressAttempt
from quikode.config import Config
from quikode.dag import DAG
from quikode.state import Store, SubtaskState
from quikode.subtask_schema import Plan, Subtask
from quikode.worker import TaskWorker


def _build_dag(tmp_path: Path) -> DAG:
    raw = {
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
    p = tmp_path / "dag.json"
    p.write_text(json.dumps(raw))
    return DAG.load(p)


def _build_worker(tmp_path: Path) -> TaskWorker:
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        prompts_dir=tmp_path / "missing-prompts",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    dag = _build_dag(tmp_path)
    store = Store(cfg.state_dir / "quikode.db")
    store.upsert_pending("R-001")
    store.upsert_subtasks(
        "R-001",
        [
            {
                "subtask_id": "S-07-mcp-tools",
                "title": "MCP tools",
                "depends_on": [],
                "files_to_touch": ["a.rs"],
                "boundary": "",
                "acceptance": ["compiles"],
                "notes": "",
            }
        ],
    )
    worker = TaskWorker(cfg, dag, store, dag.nodes["R-001"])
    worker.handle = MagicMock()
    worker.handle.container_name = "qk-stub"
    return worker


def _subtask() -> Subtask:
    return Subtask(
        id="S-07-mcp-tools",
        title="MCP tools",
        depends_on=(),
        files_to_touch=("a.rs",),
        boundary="",
        acceptance=("compiles",),
        notes="",
    )


def _plan_with_subtask(s: Subtask) -> Plan:
    return Plan(node_id="R-001", summary="x", subtasks=(s,), final_acceptance=("ci passes",))


def test_block_with_draft_pr_posts_comment_and_label(tmp_path):
    """draft_pr_number set → comment is posted with the three intervention
    paths, the subtask id, and the reason; `quikode:blocked` label added."""
    worker = _build_worker(tmp_path)
    worker.plan = _plan_with_subtask(_subtask())
    # Simulate the v3 draft PR being open at the time of block.
    worker.store.set_field("R-001", draft_pr_number=4242, worktree_path="/wt/r-001")

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("quikode.worker.subprocess.run", side_effect=fake_run),
        patch.object(worker, "_recent_attempt_history", return_value=[]),
    ):
        worker._mark_subtask_blocked(
            _subtask(),
            "progress-check verdict FLATLINED after 12 attempts",
        )

    # First call: gh pr comment with body. Second call: gh pr edit add-label.
    comment_call = next((c for c in calls if "comment" in c), None)
    label_call = next((c for c in calls if "edit" in c), None)
    assert comment_call is not None, f"expected gh pr comment call; got {calls}"
    assert label_call is not None, f"expected gh pr edit call; got {calls}"

    # Comment body sanity — pull the --body argument.
    assert "4242" in comment_call
    body_idx = comment_call.index("--body") + 1
    body = comment_call[body_idx]
    assert "S-07-mcp-tools" in body
    assert "FLATLINED" in body
    # Three intervention paths (1./2./3. markers in the message).
    assert "1. **Push fixes" in body
    assert "2. **Reply with guidance" in body
    assert "3. **Locally**" in body
    assert "quikode unblock R-001" in body

    # Label — args include "quikode:blocked"
    assert "quikode:blocked" in label_call
    assert "4242" in label_call

    # Subtask state is still BLOCKED regardless.
    s = worker.store.get_subtask("R-001", "S-07-mcp-tools")
    assert s["state"] == SubtaskState.BLOCKED.value


def test_block_without_pr_does_not_call_gh(tmp_path):
    """No PR open → no gh subprocess call; subtask still marked BLOCKED."""
    worker = _build_worker(tmp_path)
    worker.plan = _plan_with_subtask(_subtask())

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("quikode.worker.subprocess.run", side_effect=fake_run):
        worker._mark_subtask_blocked(_subtask(), "no PR yet")

    assert calls == []
    s = worker.store.get_subtask("R-001", "S-07-mcp-tools")
    assert s["state"] == SubtaskState.BLOCKED.value


def test_block_swallows_gh_failure(tmp_path):
    """`gh` failures must not propagate — BLOCKED state is the contract."""
    worker = _build_worker(tmp_path)
    worker.plan = _plan_with_subtask(_subtask())
    worker.store.set_field("R-001", draft_pr_number=99)

    def fake_run(args, **kwargs):
        raise OSError("gh: command not found")

    with (
        patch("quikode.worker.subprocess.run", side_effect=fake_run),
        patch.object(worker, "_recent_attempt_history", return_value=[]),
    ):
        # Must not raise.
        worker._mark_subtask_blocked(_subtask(), "boom")

    s = worker.store.get_subtask("R-001", "S-07-mcp-tools")
    assert s["state"] == SubtaskState.BLOCKED.value


def test_block_uses_pr_number_when_no_draft(tmp_path):
    """Falls back to `pr_number` when draft_pr_number is not set."""
    worker = _build_worker(tmp_path)
    worker.plan = _plan_with_subtask(_subtask())
    worker.store.set_field("R-001", pr_number=7)

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("quikode.worker.subprocess.run", side_effect=fake_run),
        patch.object(worker, "_recent_attempt_history", return_value=[]),
    ):
        worker._mark_subtask_blocked(_subtask(), "x")

    comment_call = next((c for c in calls if "comment" in c), None)
    assert comment_call is not None
    assert "7" in comment_call


def test_block_includes_recent_attempt_root_causes(tmp_path):
    """The comment surfaces last-3 root causes from `_recent_attempt_history`."""
    worker = _build_worker(tmp_path)
    worker.plan = _plan_with_subtask(_subtask())
    worker.store.set_field("R-001", draft_pr_number=12)

    fake_attempts = [
        ProgressAttempt(attempt_no=1, checker_root_cause="missing import", triage_notes=""),
        ProgressAttempt(attempt_no=2, checker_root_cause="wrong type signature", triage_notes=""),
        ProgressAttempt(attempt_no=3, checker_root_cause="test fixture path bad", triage_notes=""),
    ]
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("quikode.worker.subprocess.run", side_effect=fake_run),
        patch.object(worker, "_recent_attempt_history", return_value=fake_attempts),
    ):
        worker._mark_subtask_blocked(_subtask(), "flat")

    comment_call = next((c for c in calls if "comment" in c), None)
    assert comment_call is not None
    body = comment_call[comment_call.index("--body") + 1]
    assert "missing import" in body
    assert "wrong type signature" in body
    assert "test fixture path bad" in body
