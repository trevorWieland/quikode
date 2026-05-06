"""v3 Phase C auto-rebase on parent merge.

When the daemon's `_poll_review_threads` detects a parent task transitioned
to MERGED, it scans for children with `parent_pr_branch=<parent.branch>`
in non-terminal states and schedules a rebase-to-main worker for each.
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


# ----- store helpers -----


def test_children_of_parent_branch_filters_terminal(tmp_path):
    o = _orch(tmp_path)
    _seed_parent_pending_ci(o)
    _seed_stacked_child(o, "CHILD-A", state=State.DOING_SUBTASK)
    _seed_stacked_child(o, "CHILD-B", state=State.MERGED)
    children = o.store.children_of_parent_branch("quikode/parent-aaa")
    ids = sorted(c["id"] for c in children)
    assert ids == ["CHILD-A"]
    o.store.conn.close()


def test_clear_parent_branch_idempotent(tmp_path):
    o = _orch(tmp_path)
    _seed_parent_pending_ci(o)
    _seed_stacked_child(o, "CHILD-A", state=State.DOING_SUBTASK)
    o.store.clear_parent_branch("CHILD-A")
    row = o.store.get("CHILD-A")
    assert row["parent_pr_branches"] is None
    assert row["parent_branches"] is None
    # Calling again is a no-op.
    o.store.clear_parent_branch("CHILD-A")
    o.store.conn.close()


def test_pre_rebase_state_roundtrip(tmp_path):
    o = _orch(tmp_path)
    o.store.upsert_pending("CHILD-A")
    assert o.store.get_pre_rebase_state("CHILD-A") is None
    o.store.set_pre_rebase_state("CHILD-A", "doing_subtask")
    assert o.store.get_pre_rebase_state("CHILD-A") == "doing_subtask"
    o.store.set_pre_rebase_state("CHILD-A", "pending_ci")
    assert o.store.get_pre_rebase_state("CHILD-A") == "pending_ci"
    o.store.conn.close()


# ----- _schedule_rebase_to_main -----


def test_schedule_rebase_to_main_transitions_and_stashes_pre_state(tmp_path):
    o = _orch(tmp_path)
    _seed_parent_pending_ci(o)
    _seed_stacked_child(o, "CHILD-A", state=State.DOING_SUBTASK)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    o._schedule_rebase_to_main("CHILD-A", pool, futures, rrf)

    assert "CHILD-A" in futures
    assert "CHILD-A" in rrf
    row = o.store.get("CHILD-A")
    assert row["state"] == State.REBASING_TO_MAIN.value
    assert row["pre_rebase_state"] == State.DOING_SUBTASK.value
    pool.submit.assert_called_once()
    assert pool.submit.call_args[0][0] == o._run_rebase_to_main_one
    o.store.conn.close()


# ----- _schedule_rebases_for_merged_parent -----


def test_merged_parent_schedules_rebase_for_all_active_children(tmp_path):
    o = _orch(tmp_path)
    _seed_parent_pending_ci(o)
    _seed_stacked_child(o, "CHILD-A", state=State.DOING_SUBTASK)
    _seed_stacked_child(o, "CHILD-B", state=State.PENDING_CI, pr_number=11)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    o._schedule_rebases_for_merged_parent("quikode/parent-aaa", pool, futures, rrf)

    assert "CHILD-A" in futures
    assert "CHILD-B" in futures
    assert pool.submit.call_count == 2
    assert o.store.get("CHILD-A")["state"] == State.REBASING_TO_MAIN.value
    assert o.store.get("CHILD-B")["state"] == State.REBASING_TO_MAIN.value
    # CHILD-B's pre-rebase state should be PENDING_CI so the rebase
    # worker restores it post-rebase.
    assert o.store.get("CHILD-B")["pre_rebase_state"] == State.PENDING_CI.value
    o.store.conn.close()


def test_merged_parent_skips_terminal_children(tmp_path):
    o = _orch(tmp_path)
    _seed_parent_pending_ci(o)
    _seed_stacked_child(o, "CHILD-A", state=State.MERGED)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    o._schedule_rebases_for_merged_parent("quikode/parent-aaa", pool, futures, rrf)

    pool.submit.assert_not_called()
    assert o.store.get("CHILD-A")["state"] == State.MERGED.value
    o.store.conn.close()


def test_merged_parent_skips_when_child_already_in_futures(tmp_path):
    """A child with an in-flight worker future is not re-scheduled — but
    the `needs_parent_rebase` flag IS set so the active worker handles the
    rebase inline at its next checkpoint."""
    o = _orch(tmp_path)
    _seed_parent_pending_ci(o)
    _seed_stacked_child(o, "CHILD-A", state=State.DOING_SUBTASK)
    pool = _make_pool()
    pending = Future()
    futures: dict[str, Future] = {"CHILD-A": pending}
    rrf: set[str] = set()

    o._schedule_rebases_for_merged_parent("quikode/parent-aaa", pool, futures, rrf)

    pool.submit.assert_not_called()
    # State unchanged — the active worker keeps its current FSM state.
    assert o.store.get("CHILD-A")["state"] == State.DOING_SUBTASK.value
    # But the flag IS raised so the worker handles the rebase inline.
    assert o.store.get("CHILD-A")["needs_parent_rebase"] == 1
    o.store.conn.close()


def test_no_children_no_schedule(tmp_path):
    o = _orch(tmp_path)
    _seed_parent_pending_ci(o)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    o._schedule_rebases_for_merged_parent("quikode/parent-aaa", pool, futures, rrf)

    pool.submit.assert_not_called()
    o.store.conn.close()


# ----- end-to-end via _poll_review_threads when parent merges -----


def test_poll_pr_merged_triggers_child_rebase_scheduling(tmp_path):
    """Driving _poll_review_threads with a MERGED PR for the parent should
    transition the parent to MERGED and schedule rebases for any stacked
    children."""
    o = _orch(tmp_path)
    _seed_parent_pending_ci(o)
    _seed_stacked_child(o, "CHILD-A", state=State.DOING_SUBTASK)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    merged_status = PRStatus(
        number=10,
        url="https://github.com/owner/repo/pull/10",
        state="MERGED",
        mergeable="MERGEABLE",
        checks_status="success",
        failed_checks=[],
    )
    threads_mock = MagicMock()
    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=merged_status),
        patch("quikode.orchestrator.github_graphql.get_review_threads", threads_mock),
    ):
        o._poll_review_threads(pool, futures, rrf)

    assert o.store.get("PARENT")["state"] == State.MERGED.value
    assert o.store.get("CHILD-A")["state"] == State.REBASING_TO_MAIN.value
    assert "CHILD-A" in futures
    threads_mock.assert_not_called()
    o.store.conn.close()
