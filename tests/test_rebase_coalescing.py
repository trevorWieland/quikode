"""Coalescing rapid-fire rebase triggers.

When a parent merges and a sibling merges within seconds of each other,
the orchestrator can fire `_schedule_rebase_to_main` twice on the same
child within `cfg.rebase_coalesce_window_s` seconds. The second trigger
is wasted work — the first rebase will already pick up both parent
states, and any genuinely-new conflict surfaces on the next watcher tick.

These tests exercise the coalescing logic in isolation.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock

from quikode.config import Config
from quikode.dag import DAG
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
                "depends_on": [],
                "completes_behaviors": [],
                "supports_behaviors": [],
                "boundary_with_neighbors": "",
                "expected_evidence": [],
                "playbook": [],
                "rationale": "",
                "risks": [],
            }
            for nid in ("CHILD-A", "CHILD-B")
        ],
    }
    p = tmp_path / "dag.json"
    p.write_text(json.dumps(raw))
    return DAG.load(p)


def _orch(tmp_path: Path, *, window: int = 30) -> Orchestrator:
    dag = _make_dag(tmp_path)
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
        rebase_coalesce_window_s=window,
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


def _seed_awaiting(o: Orchestrator, task_id: str) -> None:
    o.store.upsert_pending(task_id)
    o.store.transition(
        task_id,
        State.PENDING_CI,
        branch=f"quikode/{task_id.lower()}",
        pr_number=42,
        pr_url="https://github.com/owner/repo/pull/42",
    )


def test_second_trigger_within_window_is_coalesced(tmp_path):
    """Two `_schedule_rebase_to_main` calls within the window → only the
    first submits a worker future; the second logs and returns."""
    o = _orch(tmp_path, window=30)
    _seed_awaiting(o, "CHILD-A")
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    o._schedule_rebase_to_main("CHILD-A", pool, futures, rrf, trigger_reason="parent_merged")
    assert pool.submit.call_count == 1
    assert o.store.get("CHILD-A")["state"] == State.REBASING_TO_MAIN.value

    # Reset state to mimic mid-rebase reentry — the pre-rebase stash is
    # the PENDING_CI state, so a second trigger 1s later finds the
    # row in REBASING_TO_MAIN and would normally try to re-stash + submit.
    # Coalescing should skip it regardless.
    o._schedule_rebase_to_main("CHILD-A", pool, futures, rrf, trigger_reason="sibling_conflict")
    assert pool.submit.call_count == 1, "second trigger within window should be coalesced"

    o.store.conn.close()


def test_trigger_after_window_fires(tmp_path):
    """After the window elapses, a fresh rebase trigger goes through."""
    o = _orch(tmp_path, window=30)
    _seed_awaiting(o, "CHILD-A")
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    # Backdate the last-trigger timestamp to simulate "31s ago".
    o._schedule_rebase_to_main("CHILD-A", pool, futures, rrf, trigger_reason="parent_merged")
    assert pool.submit.call_count == 1
    o.store.set_last_rebase_scheduled("CHILD-A", time.time() - 31.0)

    # Reset row to a state where re-scheduling is meaningful (e.g. the
    # first rebase finished and the row is back in PENDING_CI).
    o.store.transition("CHILD-A", State.PENDING_CI)

    o._schedule_rebase_to_main("CHILD-A", pool, futures, rrf, trigger_reason="sibling_conflict")
    assert pool.submit.call_count == 2, "trigger past the window should fire"

    o.store.conn.close()


def test_different_tasks_are_not_cross_coalesced(tmp_path):
    """Coalescing is per-task: two different tasks both fire even if the
    triggers are simultaneous."""
    o = _orch(tmp_path, window=30)
    _seed_awaiting(o, "CHILD-A")
    _seed_awaiting(o, "CHILD-B")
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    o._schedule_rebase_to_main("CHILD-A", pool, futures, rrf, trigger_reason="parent_merged")
    o._schedule_rebase_to_main("CHILD-B", pool, futures, rrf, trigger_reason="parent_merged")

    assert pool.submit.call_count == 2, "different tasks must not coalesce against each other"
    assert o.store.get("CHILD-A")["state"] == State.REBASING_TO_MAIN.value
    assert o.store.get("CHILD-B")["state"] == State.REBASING_TO_MAIN.value

    o.store.conn.close()


def test_window_zero_disables_coalescing(tmp_path):
    """With `rebase_coalesce_window_s=0`, every trigger fires."""
    o = _orch(tmp_path, window=0)
    _seed_awaiting(o, "CHILD-A")
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    o._schedule_rebase_to_main("CHILD-A", pool, futures, rrf, trigger_reason="parent_merged")
    # Reset transient state so the second call has something to do.
    o.store.transition("CHILD-A", State.PENDING_CI)
    o._schedule_rebase_to_main("CHILD-A", pool, futures, rrf, trigger_reason="sibling_conflict")

    assert pool.submit.call_count == 2, "window=0 must disable coalescing"

    o.store.conn.close()


def test_helper_round_trip(tmp_path):
    """`set_last_rebase_scheduled` + `get_last_rebase_scheduled_ts`
    round-trip a value, and unset rows return None."""
    o = _orch(tmp_path, window=30)
    o.store.upsert_pending("CHILD-A")
    assert o.store.get_last_rebase_scheduled_ts("CHILD-A") is None
    now = time.time()
    o.store.set_last_rebase_scheduled("CHILD-A", now)
    got = o.store.get_last_rebase_scheduled_ts("CHILD-A")
    assert got is not None
    assert abs(got - now) < 0.01

    o.store.conn.close()
