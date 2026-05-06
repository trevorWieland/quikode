"""v3 stacked-diffs fix: mid-flight parent-merge flag + worker checkpoints.

When a parent task merges while a child worker is mid-flight, the
orchestrator can't safely interrupt the worker but it can leave a
breadcrumb: `needs_parent_rebase=1` on the child row. The worker reads
that flag at safe checkpoints (top of subtask iteration, top of
final-check, top of commit/push, top of PR-open, each poll iteration)
and runs an inline rebase + PR retarget before continuing.

Children whose parent CLOSEs without merging get their stale
`parent_pr_branch` cleared so the next provision goes against main.
"""

from __future__ import annotations

import json
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from quikode.config import Config
from quikode.dag import DAG, Node
from quikode.github import PRStatus
from quikode.orchestrator import Orchestrator
from quikode.state import State, Store
from quikode.worker import TaskWorker

# ----- shared fixtures (mirror test_stacked_rebase_on_merge.py) -----


def _make_dag(tmp_path: Path) -> DAG:
    raw = {
        "schema": "test",
        "milestones": [{"id": "M-1", "title": "x", "goal": "x", "status": "planned"}],
        "nodes": [
            {
                "id": nid,
                "kind": "behavior",
                "milestone": "M-1",
                "title": nid,
                "scope": "x",
                "depends_on": deps,
                "completes_behaviors": [],
                "supports_behaviors": [],
                "boundary_with_neighbors": "",
                "expected_evidence": [],
                "playbook": [],
                "rationale": "",
                "risks": [],
            }
            for nid, deps in [("PARENT", []), ("CHILD-A", ["PARENT"]), ("CHILD-B", ["PARENT"])]
        ],
    }
    p = tmp_path / "dag.json"
    p.write_text(json.dumps(raw))
    return DAG.load(p)


def _orch(tmp_path: Path, **cfg_kw) -> Orchestrator:
    dag = _make_dag(tmp_path)
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
        **cfg_kw,
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.state_dir / "q.db")
    return Orchestrator(cfg, dag, store)


def _make_pool() -> MagicMock:
    pool = MagicMock()

    def _submit(fn, *args, **kwargs):
        f: Future = Future()
        f.set_result(None)
        return f

    pool.submit.side_effect = _submit
    return pool


def _seed_parent_pending_ci(o: Orchestrator) -> None:
    o.store.upsert_pending("PARENT")
    o.store.transition(
        "PARENT",
        State.PENDING_CI,
        branch="quikode/parent-aaa",
        pr_number=10,
        pr_url="https://github.com/owner/repo/pull/10",
    )


def _seed_stacked_child(o: Orchestrator, child_id: str, *, state: State, pr_number: int = 0) -> None:
    o.store.upsert_pending(child_id)
    o.store.transition(child_id, state, branch=f"quikode/{child_id.lower()}-bbb")
    o.store.set_field(
        child_id,
        parent_pr_branches='["quikode/parent-aaa"]',
        parent_branches='["quikode/parent-aaa"]',
        pr_number=pr_number or None,
        pr_url=(f"https://github.com/owner/repo/pull/{pr_number}" if pr_number else None),
    )


# ----- orchestrator: flag handling for in-flight vs idle children -----


def test_active_child_gets_flag_no_duplicate_future(tmp_path):
    """An in-flight child gets `needs_parent_rebase=1` set but no extra
    worker future submitted."""
    o = _orch(tmp_path)
    _seed_parent_pending_ci(o)
    _seed_stacked_child(o, "CHILD-A", state=State.DOING_SUBTASK)

    pool = _make_pool()
    pending = Future()
    futures: dict[str, Future] = {"CHILD-A": pending}
    rrf: set[str] = set()

    o._schedule_rebases_for_merged_parent("quikode/parent-aaa", pool, futures, rrf)

    # No new future submitted for the active child
    pool.submit.assert_not_called()
    # Flag IS raised so the worker handles inline at its next checkpoint
    row = o.store.get("CHILD-A")
    assert row["needs_parent_rebase"] == 1
    # State unchanged (worker is still mid-loop)
    assert row["state"] == State.DOING_SUBTASK.value
    o.store.conn.close()


def test_idle_child_gets_flag_and_rebase_future(tmp_path):
    """An idle child (no active future) ALSO gets the flag set AND a
    rebase-to-main future scheduled. The worker future is the one that
    will actually drive the rebase; the flag is harmless since the rebase
    worker clears it on success."""
    o = _orch(tmp_path)
    _seed_parent_pending_ci(o)
    _seed_stacked_child(o, "CHILD-A", state=State.PENDING_CI, pr_number=11)

    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    o._schedule_rebases_for_merged_parent("quikode/parent-aaa", pool, futures, rrf)

    assert "CHILD-A" in futures
    pool.submit.assert_called_once()
    # Flag set even though we also scheduled a future
    assert o.store.get("CHILD-A")["needs_parent_rebase"] == 1
    o.store.conn.close()


# ----- orchestrator: parent CLOSE without merge clears stale child metadata -----


def test_parent_closed_clears_children_parent_branch(tmp_path):
    """When the daemon's review-watcher sees the parent's PR transition to
    CLOSED (not merged), it clears `parent_pr_branch` on every non-terminal
    child so their next pick/provision goes against main."""
    o = _orch(tmp_path)
    _seed_parent_pending_ci(o)
    # Only seed CHILD-A — keeping CHILD-B out of the PENDING_CI poll set
    # so the per-PR-number mock below doesn't have to disambiguate.
    _seed_stacked_child(o, "CHILD-A", state=State.DOING_SUBTASK)

    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    closed_status = PRStatus(
        number=10,
        url="https://github.com/owner/repo/pull/10",
        state="CLOSED",
        mergeable="MERGEABLE",
        checks_status="success",
        failed_checks=[],
    )
    threads_mock = MagicMock()
    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=closed_status),
        patch("quikode.orchestrator.github_graphql.get_review_threads", threads_mock),
    ):
        o._poll_review_threads(pool, futures, rrf)

    # Parent aborted
    assert o.store.get("PARENT")["state"] == State.ABORTED.value
    # CHILD-A's parent metadata cleared (active, non-terminal)
    row = o.store.get("CHILD-A")
    assert row["parent_pr_branches"] is None
    assert row["parent_branches"] is None
    assert row["needs_parent_rebase"] == 0
    o.store.conn.close()


def test_parent_closed_clears_multiple_active_children(tmp_path):
    """Multiple children stacked on a parent that closes without merge —
    all non-terminal ones get their stack metadata cleared."""
    o = _orch(tmp_path)
    _seed_parent_pending_ci(o)
    _seed_stacked_child(o, "CHILD-A", state=State.DOING_SUBTASK)
    _seed_stacked_child(o, "CHILD-B", state=State.PROVISIONING)

    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    closed_status = PRStatus(
        number=10,
        url="https://github.com/owner/repo/pull/10",
        state="CLOSED",
        mergeable="MERGEABLE",
        checks_status="success",
        failed_checks=[],
    )
    threads_mock = MagicMock()
    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=closed_status),
        patch("quikode.orchestrator.github_graphql.get_review_threads", threads_mock),
    ):
        o._poll_review_threads(pool, futures, rrf)

    assert o.store.get("PARENT")["state"] == State.ABORTED.value
    for cid in ("CHILD-A", "CHILD-B"):
        row = o.store.get(cid)
        assert row["parent_pr_branches"] is None, f"{cid} should have parent_pr_branch cleared"
        assert row["parent_branches"] is None, f"{cid} should have parent_branch cleared"
    o.store.conn.close()


# ----- worker: checkpoint handler reads flag, rebases, clears -----


def _node(task_id: str = "T-CHILD") -> Node:
    return Node(
        id=task_id,
        kind="behavior",
        milestone="M-1",
        title="x",
        scope="x",
        depends_on=(),
        completes_behaviors=(),
        supports_behaviors=(),
        boundary_with_neighbors="",
        expected_evidence=(),
        playbook=(),
        rationale="",
        risks=(),
        raw={},
    )


def _worker(tmp_path) -> Any:
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path)
    store = Store(tmp_path / "q.db")

    class _DAG:
        def __init__(self):
            self.nodes = {"T-CHILD": _node()}

    return TaskWorker(cfg, _DAG(), store, _node())


def test_handle_parent_rebase_noop_when_flag_unset(tmp_path):
    w = _worker(tmp_path)
    w.store.upsert_pending("T-CHILD")
    w.store.transition("T-CHILD", State.PR_OPENING, branch="quikode/t-child-abc")
    # No flag set
    out = w._handle_parent_rebase_if_needed()
    assert out is None


def test_handle_parent_rebase_runs_rebase_and_clears(tmp_path, monkeypatch):
    """Flag set → rebase runs, PR retargets, parent metadata cleared,
    flag cleared. Worker continues."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-CHILD")
    w.store.transition("T-CHILD", State.PR_OPENING, branch="quikode/t-child-abc")
    w.store.set_field(
        "T-CHILD",
        parent_branches='["quikode/parent-xyz"]',
        parent_pr_branches='["quikode/parent-xyz"]',
        pr_number=42,
    )
    w.store.mark_needs_parent_rebase("T-CHILD")
    w.handle = MagicMock(container_name="qk-stub")

    # Track retarget calls
    retargeted: list[int] = []
    monkeypatch.setattr(
        TaskWorker,
        "_retarget_pr_to_main",
        lambda self, pr: retargeted.append(pr),
    )

    parent_sha = "0123abcd" * 5
    git_calls: list[list[str]] = []

    def fake_git(args):
        git_calls.append(args)
        if args[:2] == ["rev-parse", "--verify"]:
            return 0, parent_sha + "\n"
        if args[0] == "fetch":
            return 0, ""
        if args[:2] == ["rev-list", "--count"]:
            return 0, "2\n"
        # rebase + push
        return 0, ""

    w._git_in_workspace = fake_git

    outcome = w._handle_parent_rebase_if_needed()
    assert outcome is None  # success → continue worker

    # The rebase command used --onto with the parent_sha
    rebase_calls = [c for c in git_calls if "rebase" in c]
    assert len(rebase_calls) == 1
    assert "--onto" in rebase_calls[0]

    # PR was retargeted
    assert retargeted == [42]

    # Stacking metadata cleared, flag cleared
    row = w.store.get("T-CHILD")
    assert row["parent_branches"] is None
    assert row["parent_pr_branches"] is None
    assert row["needs_parent_rebase"] == 0


def test_handle_parent_rebase_fails_returns_blocked(tmp_path, monkeypatch):
    """Rebase failure → BLOCKED outcome so the caller can return."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-CHILD")
    w.store.transition("T-CHILD", State.PR_OPENING, branch="quikode/t-child-abc")
    w.store.set_field("T-CHILD", parent_branches='["quikode/parent-xyz"]')
    w.store.mark_needs_parent_rebase("T-CHILD")
    w.handle = MagicMock(container_name="qk-stub")

    # Make _rebase_to_base_branch return False (push fails).
    def fake_git(args):
        if args[:2] == ["rev-parse", "--verify"]:
            return 0, "deadbeef\n"
        if args[0] == "fetch":
            return 0, ""
        if args[:2] == ["rev-list", "--count"]:
            return 0, "2\n"
        if "rebase" in args:
            return 0, ""
        if args[0] == "push":
            return 1, "rejected"
        return 0, ""

    w._git_in_workspace = fake_git
    # Stub spawn_conflict_resolver to None (no conflict, so unused)
    monkeypatch.setattr(TaskWorker, "_spawn_conflict_resolver", lambda self: None)

    outcome = w._handle_parent_rebase_if_needed()
    assert outcome is not None
    assert outcome.final_state == State.BLOCKED


def test_handle_parent_rebase_called_pre_provision_is_noop(tmp_path):
    """If somehow the helper fires before _provision (no container handle),
    it returns None — the next checkpoint will retry."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-CHILD")
    w.store.mark_needs_parent_rebase("T-CHILD")
    # Don't set handle.
    out = w._handle_parent_rebase_if_needed()
    assert out is None


# ----- store helper coverage -----


def test_mark_and_clear_needs_parent_rebase(tmp_path):
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path)
    store = Store(cfg.state_dir / "q.db")
    store.upsert_pending("T-1")
    assert (store.get("T-1") or {}).get("needs_parent_rebase") in (0, None)
    store.mark_needs_parent_rebase("T-1")
    assert store.get("T-1")["needs_parent_rebase"] == 1
    store.clear_needs_parent_rebase("T-1")
    assert store.get("T-1")["needs_parent_rebase"] == 0
    store.conn.close()


def test_clear_parent_branch_also_clears_flag(tmp_path):
    """clear_parent_branch is the post-rebase cleanup path; it should zero
    the needs_parent_rebase flag too so a subsequent stale flag doesn't
    re-fire the helper."""
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path)
    store = Store(cfg.state_dir / "q.db")
    store.upsert_pending("T-1")
    store.set_field(
        "T-1",
        parent_branches='["quikode/parent-xyz"]',
        parent_pr_branches='["quikode/parent-xyz"]',
    )
    store.mark_needs_parent_rebase("T-1")
    assert store.get("T-1")["needs_parent_rebase"] == 1

    store.clear_parent_branch("T-1")
    row = store.get("T-1")
    assert row["parent_branches"] is None
    assert row["parent_pr_branches"] is None
    assert row["needs_parent_rebase"] == 0
    store.conn.close()
