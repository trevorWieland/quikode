"""Item 1: smarter rebase scheduling on parent merge.

When the daemon's `_schedule_rebases_for_merged_parent` fires, it should
NOT blindly schedule a rebase for every child sharing parent_pr_branch.
Instead:

* If the child's PR is MERGEABLE AND its base ref still exists, skip the
  rebase entirely and clear stale parent metadata.
* If the child's PR is CONFLICTING, schedule the rebase.
* If the child's base branch was deleted on the remote, schedule the
  rebase (PR likely auto-closed).
* If poll_pr fails, fall back to scheduling (be conservative).
"""

from __future__ import annotations

import json
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock, patch

from quikode.config import Config
from quikode.dag import DAG
from quikode.github import PRStatus
from quikode.orchestrator import Orchestrator
from quikode.state import State, Store


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
            for nid, deps in [("PARENT", []), ("CHILD", ["PARENT"])]
        ],
    }
    p = tmp_path / "dag.json"
    p.write_text(json.dumps(raw))
    return DAG.load(p)


def _orch(tmp_path: Path) -> Orchestrator:
    dag = _make_dag(tmp_path)
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
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


def _seed(o: Orchestrator, pr_number: int = 0) -> None:
    o.store.upsert_pending("PARENT")
    o.store.transition("PARENT", State.PENDING_CI, branch="quikode/parent-aaa")
    o.store.upsert_pending("CHILD")
    o.store.transition(
        "CHILD",
        State.PENDING_CI,
        branch="quikode/child-bbb",
        pr_number=pr_number or None,
        pr_url=(f"https://github.com/owner/repo/pull/{pr_number}" if pr_number else None),
    )
    o.store.set_field(
        "CHILD",
        parent_pr_branches='["quikode/parent-aaa"]',
        parent_branches='["quikode/parent-aaa"]',
    )


def _pr(state: str, mergeable: str, pr_number: int = 11) -> PRStatus:
    return PRStatus(
        number=pr_number,
        url=f"https://github.com/owner/repo/pull/{pr_number}",
        state=state,
        mergeable=mergeable,
        checks_status="success",
        failed_checks=[],
    )


def test_mergeable_child_with_intact_base_skips_rebase(tmp_path):
    """Child PR is MERGEABLE and the parent branch still resolves on the
    remote → no rebase scheduled, parent metadata cleared."""
    o = _orch(tmp_path)
    _seed(o, pr_number=11)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=_pr("OPEN", "MERGEABLE")),
        patch.object(o, "_remote_branch_exists", return_value=True),
    ):
        o._schedule_rebases_for_merged_parent("quikode/parent-aaa", pool, futures, rrf)

    pool.submit.assert_not_called()
    row = o.store.get("CHILD")
    assert row["state"] == State.PENDING_CI.value  # untouched
    assert row["parent_pr_branches"] is None  # cleared
    assert row["parent_branches"] is None
    assert (row.get("needs_parent_rebase") or 0) == 0
    o.store.conn.close()


def test_conflicting_child_schedules_rebase(tmp_path):
    """Child PR is CONFLICTING → rebase scheduled."""
    o = _orch(tmp_path)
    _seed(o, pr_number=11)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=_pr("OPEN", "CONFLICTING")),
        patch.object(o, "_remote_branch_exists", return_value=True),
    ):
        o._schedule_rebases_for_merged_parent("quikode/parent-aaa", pool, futures, rrf)

    pool.submit.assert_called_once()
    assert o.store.get("CHILD")["state"] == State.REBASING_TO_MAIN.value
    o.store.conn.close()


def test_deleted_base_schedules_rebase(tmp_path):
    """Child PR is OPEN+MERGEABLE but its base ref is gone from the
    remote (parent merged with --delete-branch) → rebase scheduled
    so the worker can recreate / retarget the PR."""
    o = _orch(tmp_path)
    _seed(o, pr_number=11)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=_pr("OPEN", "MERGEABLE")),
        patch.object(o, "_remote_branch_exists", return_value=False),
    ):
        o._schedule_rebases_for_merged_parent("quikode/parent-aaa", pool, futures, rrf)

    pool.submit.assert_called_once()
    assert o.store.get("CHILD")["state"] == State.REBASING_TO_MAIN.value
    o.store.conn.close()


def test_no_pr_yet_falls_back_to_scheduling(tmp_path):
    """Child has no PR number yet (mid-DOING) → no mergeable signal to
    inspect, schedule rebase so the worker handles it inline."""
    o = _orch(tmp_path)
    o.store.upsert_pending("PARENT")
    o.store.transition("PARENT", State.PENDING_CI, branch="quikode/parent-aaa")
    o.store.upsert_pending("CHILD")
    o.store.transition("CHILD", State.DOING_SUBTASK, branch="quikode/child-bbb")
    o.store.set_field(
        "CHILD",
        parent_pr_branches='["quikode/parent-aaa"]',
        parent_branches='["quikode/parent-aaa"]',
    )
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    with patch("quikode.orchestrator.github.poll_pr") as poll_mock:
        o._schedule_rebases_for_merged_parent("quikode/parent-aaa", pool, futures, rrf)
        # No PR — poll_pr should not be invoked
        poll_mock.assert_not_called()

    pool.submit.assert_called_once()
    assert o.store.get("CHILD")["state"] == State.REBASING_TO_MAIN.value
    o.store.conn.close()


def test_poll_pr_failure_schedules_rebase_conservatively(tmp_path):
    """If poll_pr raises, fall back to scheduling — better to do an
    unnecessary rebase than leave a child with a silently-broken PR."""
    o = _orch(tmp_path)
    _seed(o, pr_number=11)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    with patch("quikode.orchestrator.github.poll_pr", side_effect=OSError("boom")):
        o._schedule_rebases_for_merged_parent("quikode/parent-aaa", pool, futures, rrf)

    pool.submit.assert_called_once()
    assert o.store.get("CHILD")["state"] == State.REBASING_TO_MAIN.value
    o.store.conn.close()
