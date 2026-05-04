"""v3 own-branch divergence detection + recovery.

When upstream commits land on the child's own branch (operator hand-edit,
parallel quikode workspace, GitHub web-UI commit), the worker checks at
each subtask boundary whether the remote has new commits and either:
  - Pure FF (we have no local commits ahead): reset --hard origin/<branch>
  - Force-push (history rewritten, base sha not reachable): BLOCK
  - Diverged but mergeable: falls through to legacy push-fail handling
    (future: pull --rebase + conflict-resolver)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from quikode.config import Config
from quikode.dag import DAG
from quikode.state import State, Store
from quikode.worker import TaskWorker, WorkerOutcome


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
    dag = _build_dag(tmp_path)
    store = Store(cfg.state_dir / "q.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.DOING_SUBTASK, branch="quikode/r-001-abc", base_ref_sha="aaa111")
    worker = TaskWorker(cfg, dag, store, dag.nodes["R-001"])
    worker.handle = MagicMock(container_name="qk-stub")
    return worker


def _git_responses(seq: list[tuple[int, str]]):
    """Helper to return successive (rc, output) tuples on each _git_in_workspace call."""
    it = iter(seq)

    def _fake(args):
        try:
            return next(it)
        except StopIteration:
            return (0, "")

    return _fake


def test_no_divergence_returns_none(tmp_path):
    """When `git rev-list --count --left-right` reports 0 ahead, 0 behind,
    the worker is in sync — return None, no action."""
    worker = _build_worker(tmp_path)
    seq = [
        (0, ""),  # fetch
        (0, "0\t0\n"),  # rev-list ahead/behind
    ]
    with patch.object(worker, "_git_in_workspace", side_effect=_git_responses(seq)):
        outcome = worker._handle_branch_divergence_if_needed()
    assert outcome is None
    assert worker.store.get("R-001")["state"] == State.DOING_SUBTASK.value
    worker.store.conn.close()


def test_pure_ff_resets_hard_to_remote(tmp_path):
    """When remote has new commits and we have none ahead, `reset --hard
    origin/<branch>` brings us in line; worker continues."""
    worker = _build_worker(tmp_path)
    calls: list[list[str]] = []

    def fake_git(args):
        calls.append(args)
        if args[0] == "fetch":
            return (0, "")
        if args[0] == "rev-list":
            return (0, "0\t3\n")  # 0 ahead, 3 behind
        if args[0] == "reset":
            return (0, "HEAD now at deadbeef")
        return (0, "")

    with patch.object(worker, "_git_in_workspace", side_effect=fake_git):
        outcome = worker._handle_branch_divergence_if_needed()

    assert outcome is None  # success
    # Reset --hard was called.
    reset_calls = [c for c in calls if c[0] == "reset"]
    assert len(reset_calls) == 1
    assert "--hard" in reset_calls[0]
    assert "origin/quikode/r-001-abc" in reset_calls[0]
    worker.store.conn.close()


def test_force_push_blocks_with_useful_message(tmp_path):
    """When the recorded base_ref_sha is no longer reachable from origin,
    history was rewritten. Cannot safely auto-recover — BLOCK with guidance."""
    worker = _build_worker(tmp_path)

    def fake_git(args):
        if args[0] == "fetch":
            return (0, "")
        if args[0] == "rev-list":
            return (0, "2\t5\n")  # 2 ahead, 5 behind = diverged
        if args[0] == "merge-base":
            # is-ancestor of base_ref_sha → fail (sha not reachable)
            return (1, "")
        return (0, "")

    with patch.object(worker, "_git_in_workspace", side_effect=fake_git):
        outcome = worker._handle_branch_divergence_if_needed()

    assert outcome is not None
    assert outcome.final_state == State.BLOCKED
    row = worker.store.get("R-001")
    assert row["state"] == State.BLOCKED.value
    assert "force-push" in (row["last_error"] or "").lower()
    assert "quikode unblock" in (row["last_error"] or "")
    worker.store.conn.close()


def test_diverged_clean_rebase_succeeds(tmp_path):
    """Diverged (ahead > 0 AND behind > 0), base_ref_sha still reachable
    (not a force-push) → attempt `git rebase origin/<branch>`. On clean
    rebase, return None and caller continues."""
    worker = _build_worker(tmp_path)
    git_calls: list[list[str]] = []

    def fake_git(args):
        git_calls.append(args)
        if args[0] == "fetch":
            return (0, "")
        if args[0] == "rev-list":
            return (0, "2\t3\n")
        if args[0] == "merge-base":
            return (0, "")  # is-ancestor succeeds (not force-push)
        if args[0] == "-c" and "rebase" in args:
            return (0, "First, rewinding head...\nApplying...")
        if args[0] == "push":
            return (0, "")
        return (0, "")

    with patch.object(worker, "_git_in_workspace", side_effect=fake_git):
        outcome = worker._handle_branch_divergence_if_needed()

    assert outcome is None
    # The rebase command was invoked.
    rebase_calls = [c for c in git_calls if "rebase" in c]
    assert len(rebase_calls) == 1
    assert "core.editor=true" in " ".join(rebase_calls[0])
    # Force-with-lease push followed.
    push_calls = [c for c in git_calls if c[0] == "push"]
    assert len(push_calls) == 1
    assert "--force-with-lease" in push_calls[0]
    # Task still in DOING_SUBTASK.
    assert worker.store.get("R-001")["state"] == State.DOING_SUBTASK.value
    worker.store.conn.close()


def test_diverged_rebase_hard_failure_blocks(tmp_path):
    """Rebase fails with no rebase state dir (e.g. detached HEAD) →
    cannot recover; BLOCK with a clear message."""
    worker = _build_worker(tmp_path)

    def fake_git(args):
        if args[0] == "fetch":
            return (0, "")
        if args[0] == "rev-list":
            return (0, "2\t3\n")
        if args[0] == "merge-base":
            return (0, "")
        if args[0] == "-c" and "rebase" in args:
            return (1, "fatal: HEAD is detached")
        return (0, "")

    with (
        patch.object(worker, "_git_in_workspace", side_effect=fake_git),
        patch.object(worker, "_rebase_in_progress", return_value=False),
    ):
        outcome = worker._handle_branch_divergence_if_needed()

    assert outcome is not None
    assert outcome.final_state == State.BLOCKED
    row = worker.store.get("R-001")
    assert "rebase" in (row["last_error"] or "").lower()
    worker.store.conn.close()


def test_diverged_rebase_with_conflict_invokes_resolver(tmp_path):
    """Rebase reports conflict (rebase still in progress after rc!=0) →
    invoke `_spawn_conflict_resolver`. This re-uses the existing parent-
    merge conflict-resolution path."""
    worker = _build_worker(tmp_path)

    def fake_git(args):
        if args[0] == "fetch":
            return (0, "")
        if args[0] == "rev-list":
            return (0, "2\t3\n")
        if args[0] == "merge-base":
            return (0, "")
        if args[0] == "-c" and "rebase" in args:
            return (1, "CONFLICT (content): foo.rs")
        if args[0] == "push":
            return (0, "")
        return (0, "")

    resolver_called = []

    def fake_resolver():
        resolver_called.append(True)

    with (
        patch.object(worker, "_git_in_workspace", side_effect=fake_git),
        patch.object(worker, "_rebase_in_progress", return_value=True),
        patch.object(worker, "_spawn_conflict_resolver", side_effect=fake_resolver),
    ):
        outcome = worker._handle_branch_divergence_if_needed()

    assert outcome is None
    assert resolver_called == [True]
    worker.store.conn.close()


def test_diverged_rebase_resolver_blocks_propagates(tmp_path):
    """When the conflict resolver gives up + BLOCKS, propagate that outcome."""
    worker = _build_worker(tmp_path)

    def fake_git(args):
        if args[0] == "fetch":
            return (0, "")
        if args[0] == "rev-list":
            return (0, "2\t3\n")
        if args[0] == "merge-base":
            return (0, "")
        if args[0] == "-c" and "rebase" in args:
            return (1, "CONFLICT")
        return (0, "")

    with (
        patch.object(worker, "_git_in_workspace", side_effect=fake_git),
        patch.object(worker, "_rebase_in_progress", return_value=True),
        patch.object(
            worker,
            "_spawn_conflict_resolver",
            return_value=WorkerOutcome(State.BLOCKED, "resolver gave up"),
        ),
    ):
        outcome = worker._handle_branch_divergence_if_needed()

    assert outcome is not None
    assert outcome.final_state == State.BLOCKED
    worker.store.conn.close()


def test_fetch_failure_is_skipped_not_blocked(tmp_path):
    """Network failure on fetch → not divergence, just skip the check.
    Returning BLOCK on every transient network blip would be terrible."""
    worker = _build_worker(tmp_path)

    def fake_git(args):
        if args[0] == "fetch":
            return (1, "network unreachable")
        return (0, "")

    with patch.object(worker, "_git_in_workspace", side_effect=fake_git):
        outcome = worker._handle_branch_divergence_if_needed()

    assert outcome is None
    assert worker.store.get("R-001")["state"] == State.DOING_SUBTASK.value
    worker.store.conn.close()


def test_skipped_during_active_fixup_review(tmp_path):
    """Review-response cycles push 4-5 mini-commits in a tight window.
    The fetch + count check at every subtask boundary would waste time;
    skip when an active fixup-review subtask exists. The pre-push check
    in commit_subtask still catches non-FF in those cases."""
    worker = _build_worker(tmp_path)
    # Seed an active fixup-review subtask.
    worker.store.upsert_subtasks(
        "R-001",
        [
            {
                "subtask_id": "F-1-1-some-review-fix",
                "title": "x",
                "acceptance": ["x"],
                "kind": "fixup-review",
            }
        ],
    )
    worker.store.update_subtask("R-001", "F-1-1-some-review-fix", state="doing")

    git_called = []

    def fake_git(args):
        git_called.append(args)
        return (0, "")

    with patch.object(worker, "_git_in_workspace", side_effect=fake_git):
        outcome = worker._handle_branch_divergence_if_needed()

    assert outcome is None
    # `_git_in_workspace` was NOT called — skipped the whole check.
    assert git_called == []
    worker.store.conn.close()


def test_no_branch_or_handle_returns_none(tmp_path):
    """Pre-provision (no container) or pre-push (no branch) → no-op."""
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
    dag = _build_dag(tmp_path)
    store = Store(cfg.state_dir / "q.db")
    store.upsert_pending("R-001")
    # No branch set, no transition.
    worker = TaskWorker(cfg, dag, store, dag.nodes["R-001"])
    worker.handle = None  # pre-provision
    assert worker._handle_branch_divergence_if_needed() is None

    # With handle but no branch.
    worker.handle = MagicMock(container_name="qk-stub")
    assert worker._handle_branch_divergence_if_needed() is None

    store.conn.close()
