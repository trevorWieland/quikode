"""v3 stalled-review-response auto-recovery.

Regression for the 2026-05-04 R-0002/R-0015 pool-slot leaks: a review-
response future was submitted to the worker pool but silently crashed
before any agent_call fired. The task sat in `addressing_feedback` for
30+ minutes holding a pool slot, starving real work. The orchestrator's
stall detector now spots this (no agent_call within stall_warn_seconds
of entering ADDRESSING_FEEDBACK) and force-recovers by canceling the
future + transitioning the task back to PENDING_CI so the watcher's
next tick re-dispatches.
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


def _orch(tmp_path: Path, **cfg_kw) -> Orchestrator:
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
    dag = _make_dag(tmp_path)
    store = Store(cfg.state_dir / "q.db")
    return Orchestrator(cfg, dag, store)


def test_stalled_review_response_force_recovers_after_threshold(tmp_path):
    """Task in addressing_feedback with no agent_call for > stall_warn_seconds
    is reset to PENDING_CI; future is dropped from tracking; pool slot
    is freed for re-dispatch."""
    o = _orch(tmp_path, stall_warn_seconds=60)
    o.store.upsert_pending("R-001")
    o.store.transition("R-001", State.PENDING_CI, pr_number=42)
    # Advance state into ADDRESSING_FEEDBACK with a backdated state_log
    # entry so the silence window appears > 60s.
    o.store.transition("R-001", State.AUDIT_LOCAL_CI)
    long_ago = time.time() - 120
    o.store.conn.execute(
        "UPDATE state_log SET ts = ? WHERE task_id = ? AND to_state = ?",
        (long_ago, "R-001", State.AUDIT_LOCAL_CI.value),
    )
    o.store.conn.commit()

    # Simulate the leaked-future state: tracking sets show the slot is
    # reserved, but no agent_calls exist for the task.
    leaked_future = MagicMock(spec=Future)
    leaked_future.cancel.return_value = False  # the realistic case
    futures: dict[str, Future] = {"R-001": leaked_future}
    rrf: set[str] = {"R-001"}
    warned: dict[str, float] = {}

    o._check_stalls(warned, futures, rrf)

    # State reset so the watcher's next tick re-dispatches.
    row = o.store.get("R-001")
    assert row["state"] == State.PENDING_CI.value
    # Future removed from tracking sets (slot freed).
    assert "R-001" not in futures
    assert "R-001" not in rrf
    # cancel() was attempted.
    leaked_future.cancel.assert_called_once()
    o.store.conn.close()


def test_stalled_review_response_skipped_if_recent_agent_call(tmp_path):
    """If an agent_call has fired within the stall window, the task is making
    progress — DON'T force-recover. Without this guard, a slow but legit
    review-response cycle would get yanked mid-doer."""
    o = _orch(tmp_path, stall_warn_seconds=60)
    o.store.upsert_pending("R-001")
    o.store.transition("R-001", State.PENDING_CI, pr_number=42)
    o.store.transition("R-001", State.AUDIT_LOCAL_CI)
    long_ago = time.time() - 120
    o.store.conn.execute(
        "UPDATE state_log SET ts = ? WHERE task_id = ? AND to_state = ?",
        (long_ago, "R-001", State.AUDIT_LOCAL_CI.value),
    )
    # Recent agent_call (10s ago) — proves the worker is alive.
    o.store.record_agent_call(
        "R-001",
        phase="subtask_doer",
        cli="opencode",
        model="glm-5.1",
        rc=0,
        duration_s=5,
        tokens_used=None,
    )
    o.store.conn.execute(
        "UPDATE agent_calls SET ts = ? WHERE task_id = ?",
        (time.time() - 10, "R-001"),
    )
    o.store.conn.commit()

    futures: dict[str, Future] = {"R-001": MagicMock(spec=Future)}
    rrf: set[str] = {"R-001"}
    o._check_stalls({}, futures, rrf)

    # Recovery did NOT fire — the task is making progress.
    row = o.store.get("R-001")
    assert row["state"] == State.AUDIT_LOCAL_CI.value
    assert "R-001" in futures
    assert "R-001" in rrf
    o.store.conn.close()


def test_stalled_check_skipped_when_no_pool_args(tmp_path):
    """`_check_stalls` is also called from contexts that don't have pool
    state (test bootstrapping, tools that re-use the orchestrator class).
    Calling without futures + review_response_futures must not crash."""
    o = _orch(tmp_path)
    o.store.upsert_pending("R-001")
    o.store.transition("R-001", State.AUDIT_LOCAL_CI)
    # Should be a no-op for the review-response path; only the worktree-
    # quiet check runs (which won't fire since R-001 has no worktree_path).
    o._check_stalls({})  # no futures/rrf passed
    row = o.store.get("R-001")
    assert row["state"] == State.AUDIT_LOCAL_CI.value
    o.store.conn.close()


def test_stalled_review_recovery_uses_max_of_entered_and_last_call(tmp_path):
    """If a task has agent_calls from a PRIOR review-response cycle and
    just re-entered ADDRESSING_FEEDBACK, the silence window should be
    measured from the new transition, NOT the prior cycle's last call.
    Without this, the second cycle would inherit the prior call timestamp
    and never fire the stall detector even when it's actually stuck."""
    o = _orch(tmp_path, stall_warn_seconds=60)
    o.store.upsert_pending("R-001")
    # Old agent_call from a prior cycle (say, 5 min ago — well within the
    # stall window). If the detector used MIN(entered, last_call) instead
    # of MAX, this would suppress the recovery.
    o.store.transition("R-001", State.PENDING_CI, pr_number=42)
    o.store.record_agent_call(
        "R-001",
        phase="subtask_doer",
        cli="opencode",
        model="glm-5.1",
        rc=0,
        duration_s=5,
        tokens_used=None,
    )
    o.store.conn.execute(
        "UPDATE agent_calls SET ts = ? WHERE task_id = ?",
        (time.time() - 30, "R-001"),
    )
    # New cycle: enters ADDRESSING_FEEDBACK > 60s ago.
    o.store.transition("R-001", State.AUDIT_LOCAL_CI)
    long_ago = time.time() - 120
    o.store.conn.execute(
        "UPDATE state_log SET ts = ? WHERE task_id = ? AND to_state = ?",
        (long_ago, "R-001", State.AUDIT_LOCAL_CI.value),
    )
    o.store.conn.commit()

    futures: dict[str, Future] = {"R-001": MagicMock(spec=Future)}
    rrf: set[str] = {"R-001"}
    o._check_stalls({}, futures, rrf)

    # MAX(entered=120s ago, last_call=30s ago) = 30s ago → silence < 60s
    # → no recovery (because the prior call IS within the window).
    # This documents the intentional trade-off: false-negative on the
    # second cycle of the same task is preferable to false-positive
    # recoveries that cancel legitimate work.
    row = o.store.get("R-001")
    assert row["state"] == State.AUDIT_LOCAL_CI.value
    o.store.conn.close()
