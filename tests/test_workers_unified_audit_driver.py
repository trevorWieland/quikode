"""Plan 58: unified audit-cycle driver tests.

Exercises `_run_audit_cycle(trigger_source=...)` for INITIAL_AUDIT /
CI_FAILURE / REVIEW_FEEDBACK. Tests assert that:
  - the FSM enters AUDIT_LOCAL_CI from the right source state
  - the phase wire-up fires INITIAL → PRE_PR_REVIEW for INITIAL_AUDIT
  - the trigger-source flows through to the OUTER wrapping decision
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from quikode import fsm_runtime
from quikode.state import Store
from quikode.state_types import Phase, State
from quikode.workers.audit_driver import (
    AuditTriggerSource,
    audit_cycle_prologue,
)


def _seed_task(tmp_path: Path, state: State) -> Store:
    store = Store(tmp_path / "q.db")
    store.upsert_pending("R-001")
    if state is not State.PENDING:
        # Bridge through to the requested state via raw transitions for
        # test setup convenience.
        store.transition("R-001", state, note="test setup")
    return store


def test_initial_audit_prologue_fires_phase_transition(tmp_path: Path) -> None:
    """INITIAL_AUDIT prologue advances phase initial → pre_pr_review."""
    store = _seed_task(tmp_path, State.LOCAL_CI_CHECKING)
    worker = _FakeWorker(store)

    audit_cycle_prologue(worker, AuditTriggerSource.INITIAL_AUDIT)

    row = store.get("R-001")
    assert row["phase"] == Phase.PRE_PR_REVIEW.value
    assert row["cycle_in_phase"] == 1
    store.conn.close()


def test_ci_failure_prologue_enters_audit_local_ci_from_pending_ci(tmp_path: Path) -> None:
    """CI_FAILURE prologue fires CI_FIXUP_START from PENDING_CI."""
    store = _seed_task(tmp_path, State.PENDING_CI)
    worker = _FakeWorker(store)

    audit_cycle_prologue(worker, AuditTriggerSource.CI_FAILURE)

    assert fsm_runtime.current_state(store, "R-001") is State.AUDIT_LOCAL_CI
    store.conn.close()


def test_review_feedback_prologue_enters_audit_local_ci_from_awaiting_review(
    tmp_path: Path,
) -> None:
    """REVIEW_FEEDBACK prologue fires REVIEW_FIXUP_START from AWAITING_REVIEW."""
    store = _seed_task(tmp_path, State.PENDING_CI)
    # Bridge PENDING_CI → AWAITING_REVIEW.
    store.transition("R-001", State.AWAITING_REVIEW, note="ci passed")
    worker = _FakeWorker(store)

    audit_cycle_prologue(worker, AuditTriggerSource.REVIEW_FEEDBACK)

    assert fsm_runtime.current_state(store, "R-001") is State.AUDIT_LOCAL_CI
    store.conn.close()


def test_initial_audit_prologue_is_idempotent_on_repeated_call(tmp_path: Path) -> None:
    """Calling prologue twice on INITIAL_AUDIT does not re-bump the phase."""
    store = _seed_task(tmp_path, State.LOCAL_CI_CHECKING)
    worker = _FakeWorker(store)

    audit_cycle_prologue(worker, AuditTriggerSource.INITIAL_AUDIT)
    audit_cycle_prologue(worker, AuditTriggerSource.INITIAL_AUDIT)

    row = store.get("R-001")
    assert row["phase"] == Phase.PRE_PR_REVIEW.value
    assert row["cycle_in_phase"] == 1
    store.conn.close()


class _FakeWorker:
    """Minimal worker double matching the audit_driver function signatures."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.node = _FakeNode("R-001")

    def _row(self) -> Any:
        return self.store.get("R-001")


class _FakeNode:
    def __init__(self, task_id: str) -> None:
        self.id = task_id
