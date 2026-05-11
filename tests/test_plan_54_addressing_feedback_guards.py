"""Plan 54: post-PR CI-fix loop must not crash on ADDRESSING_FEEDBACK.

Two unrelated-but-related crash paths fixed by plan 54:

1. The plan-53 no-op-DONE path, which fires `_handle_subtask_pass` →
   `_handle_passed_subtask` → `enter_committing` (`SUBTASK_PASSED`). When
   the parent task is in `ADDRESSING_FEEDBACK` (the post-PR CI-fix
   loop), the FSM rejects `addressing_feedback → committing` and the
   worker crashes. The fix in `_handle_passed_subtask` gates the per-
   subtask-loop FSM events on the parent state. (Covered alongside the
   per-subtask commit tests in `test_per_subtask_commit.py`.)

2. The plan-49 follow-up: `_run_fixup_round` reads parent state, then
   conditionally fires `enter_fixup_planning`. Between the read and
   the FSM call, the parent state can drift (e.g. an exception in a
   sibling worker pushed it back to `PENDING_CI`). The original guard
   only short-circuited on `ADDRESSING_FEEDBACK`; on any other
   non-source state it raised `InvalidTransition`. The fix re-reads
   state right before the FSM call and skips the event when the
   current state isn't a valid source for `enter_fixup_planning`.

This file tests fix #2.

Plan 57: the `enter_fixup_planning` helper now silently skips (returns
None + INFO log) instead of raising `InvalidTransition` when the source
state is invalid. The plan-54 call-site guard stays as defense-in-depth
(cheaper, clearer rationale at the call site); the FSM-layer no-op is
the safety net. The legacy "would raise without guard" sanity test is
updated to assert the new return-None semantics.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from quikode import fsm_runtime
from quikode.config import Config
from quikode.dag import DAG
from quikode.state import State, Store
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


def _build_plan() -> Plan:
    return Plan(
        node_id="R-001",
        summary="test plan",
        subtasks=(
            Subtask(
                id="S-01",
                title="seed subtask",
                depends_on=(),
                files_to_touch=("foo.rs",),
                boundary="",
                acceptance=("compiles",),
                notes="",
            ),
        ),
        final_acceptance=("just ci passes",),
    )


def _build_worker(tmp_path: Path, *, initial_state: State) -> TaskWorker:
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        prompts_dir=tmp_path / "missing-prompts",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
        subtask_hard_max_attempts=2,
        subtask_progress_check_after=10,
        subtask_progress_check_every=10,
        pre_commit_runner="none",
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    dag = _build_dag(tmp_path)
    store = Store(cfg.state_dir / "quikode.db")
    store.upsert_pending("R-001")
    store.transition("R-001", initial_state)
    store.set_field("R-001", branch="quikode/r-001-abc123")
    plan = _build_plan()
    worker = TaskWorker(cfg, dag, store, dag.nodes["R-001"])
    worker.plan = plan
    worker.handle = MagicMock()
    worker.handle.container_name = "qk-stub"
    return worker


def _stub_block_current(*args: Any, **kwargs: Any) -> Any:
    """Patch target for `fsm_runtime.block_current` so failed-planner
    fallthroughs don't crash on bogus FSM events in tests where the
    parent state is unusual."""
    return State.BLOCKED


def test_run_fixup_round_skips_enter_fixup_planning_on_drifted_state(tmp_path):
    """Plan 54 follow-up: parent state drifted to PENDING_CI between
    the dispatcher's read and the FSM call. `enter_fixup_planning` is
    only valid from {LOCAL_CI_CHECKING, PRE_PR_AUDITING, FIXUP_PLANNING};
    PENDING_CI is not a valid source. The worker must log + skip the
    FSM call and continue to the planner invocation (which returns
    None below to end the test cleanly)."""
    worker = _build_worker(tmp_path, initial_state=State.PENDING_CI)

    # Make the planner produce nothing — `_run_fixup_round` will then
    # call `block_current`, which we stub to avoid driving an invalid
    # FSM event from PENDING_CI in this synthetic test.
    with (
        patch.object(worker, "_invoke_fixup_planner", return_value=None),
        patch("quikode.workers.pre_pr.fsm_runtime.block_current", side_effect=_stub_block_current),
    ):
        outcome = worker._run_fixup_round(
            kind="fixup-ci",
            round_no=1,
            trigger="ci",
        )

    # The parent state must be unchanged — no FSM event fired.
    assert worker.store.get("R-001")["state"] == State.PENDING_CI.value
    # And we got a BLOCKED outcome from the empty-plan path (sanity).
    assert outcome is not None
    assert outcome.final_state is State.BLOCKED
    worker.store.conn.close()


def test_run_fixup_round_fires_enter_fixup_planning_from_local_ci_checking(tmp_path):
    """Plan 54 regression guard: from LOCAL_CI_CHECKING (a valid source
    state for `enter_fixup_planning`), the FSM event still fires."""
    worker = _build_worker(tmp_path, initial_state=State.LOCAL_CI_CHECKING)

    captured: dict[str, Any] = {}

    def _capture_state_during_planner(**_kwargs: Any) -> None:
        captured["state_during_planner"] = worker.store.get("R-001")["state"]

    with (
        patch.object(worker, "_invoke_fixup_planner", side_effect=_capture_state_during_planner),
        patch("quikode.workers.pre_pr.fsm_runtime.block_current", side_effect=_stub_block_current),
    ):
        worker._run_fixup_round(kind="fixup-ci", round_no=1, trigger="ci")

    # Inside _invoke_fixup_planner, the parent must already be in
    # FIXUP_PLANNING (the FSM transition was applied).
    assert captured["state_during_planner"] == State.FIXUP_PLANNING.value
    worker.store.conn.close()


def test_run_fixup_round_skips_enter_fixup_planning_when_already_addressing_feedback(tmp_path):
    """Plan 54 + plan-49 prior behavior: when the parent task is in
    ADDRESSING_FEEDBACK, the worker is running inside the feedback flow
    (post-PR CI fix or review response). The fixup planner runs without
    an additional FSM transition; the feedback caller will transition
    ADDRESSING_FEEDBACK → PENDING_CI on completion."""
    worker = _build_worker(tmp_path, initial_state=State.ADDRESSING_FEEDBACK)

    captured: dict[str, Any] = {}

    def _capture_state_during_planner(**_kwargs: Any) -> None:
        captured["state_during_planner"] = worker.store.get("R-001")["state"]

    with (
        patch.object(worker, "_invoke_fixup_planner", side_effect=_capture_state_during_planner),
        patch("quikode.workers.pre_pr.fsm_runtime.block_current", side_effect=_stub_block_current),
    ):
        worker._run_fixup_round(kind="fixup-ci", round_no=1, trigger="ci")

    # State unchanged: the worker recognized the feedback context and
    # skipped the FSM event.
    assert captured["state_during_planner"] == State.ADDRESSING_FEEDBACK.value
    worker.store.conn.close()


def test_enter_fixup_planning_from_pending_ci_returns_none_after_plan_57(tmp_path):
    """Plan 57: `enter_fixup_planning` no longer raises from invalid
    source states — it logs INFO and returns `None`. This confirms the
    underlying FSM still doesn't allow `pending_ci → fixup_planning`
    (so the plan-54 call-site guard still has a real risk to guard
    against) but the helper now fails closed instead of crashing the
    worker."""
    worker = _build_worker(tmp_path, initial_state=State.PENDING_CI)
    result = fsm_runtime.enter_fixup_planning(worker.store, "R-001", note="should skip")
    assert result is None
    # State is unchanged: no transition occurred.
    assert worker.store.get("R-001")["state"] == State.PENDING_CI.value
    worker.store.conn.close()
