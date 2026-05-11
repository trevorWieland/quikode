"""Plan 57: typed guards on `fsm_runtime.enter_*` helpers.

The `enter_*` helpers — plus `mark_merged` and `block_current` — now
return `State | None` and silently skip (INFO log) instead of raising
`InvalidTransition` when the task's current state doesn't allow the
helper's transition. This makes them safe to call fire-and-forget from
any worker/watcher path; plan-49 / plan-54 per-call-site guards stay as
defense-in-depth.

Coverage:

- One invalid-source-state test per `enter_*` helper (parametrized).
- One valid-source-state test per `enter_*` helper (parametrized).
- `mark_merged` invalid-source-state test.
- `block_current` invalid-source-state test.
- `block_current` from a state that DOES have a BLOCK_TASK transition
  (sanity check that the FSM-allowed path still works).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from quikode import fsm_runtime
from quikode.state import State, Store


def _store_with_task(tmp_path: Path, *, state: State) -> Store:
    store = Store(tmp_path / "q.db")
    store.upsert_pending("R-1")
    if state is not State.PENDING:
        store.transition("R-1", state)
    return store


# (helper_name, valid_source_state, expected_new_state)
# Picks one representative valid source state per helper. The full set
# of valid sources is covered indirectly by the integration tests that
# exercise each post-PR / pre-PR worker path.
_VALID_TRANSITIONS: list[tuple[str, State, State]] = [
    ("enter_doing_subtask", State.PLANNING, State.DOING_SUBTASK),
    ("enter_checking_subtask", State.DOING_SUBTASK, State.CHECKING_SUBTASK),
    ("enter_triaging_subtask", State.CHECKING_SUBTASK, State.TRIAGING_SUBTASK),
    ("enter_committing", State.CHECKING_SUBTASK, State.COMMITTING),
    ("enter_pushing", State.COMMITTING, State.PUSHING),
    ("enter_local_ci_checking", State.PUSHING, State.LOCAL_CI_CHECKING),
    # Plan 58: PRE_PR_AUDITING / ADDRESSING_FEEDBACK retired; the
    # audit-stage helpers and trigger-source entry helpers replace them.
    ("enter_audit_local_ci", State.LOCAL_CI_CHECKING, State.AUDIT_LOCAL_CI),
    ("enter_fixup_planning", State.LOCAL_CI_CHECKING, State.FIXUP_PLANNING),
    ("enter_pr_opening", State.AUDIT_BEHAVIOR, State.PR_OPENING),
    ("enter_pending_ci", State.PR_OPENING, State.PENDING_CI),
    ("enter_awaiting_review", State.PENDING_CI, State.AWAITING_REVIEW),
    ("enter_audit_cycle_for_ci_fixup", State.PENDING_CI, State.AUDIT_LOCAL_CI),
    ("enter_audit_cycle_for_review_fixup", State.AWAITING_REVIEW, State.AUDIT_LOCAL_CI),
    ("enter_rebasing_to_main", State.PENDING_CI, State.REBASING_TO_MAIN),
    ("enter_conflict_resolving", State.REBASING_TO_MAIN, State.CONFLICT_RESOLVING),
]


# Each `enter_*` helper paired with a source state for which NO valid
# transition exists. Terminal states (MERGED / BLOCKED / FAILED /
# ABORTED) are universally invalid sources for forward `enter_*` calls;
# we use MERGED throughout for consistency.
_INVALID_SOURCES: list[tuple[str, State]] = [
    ("enter_doing_subtask", State.MERGED),
    ("enter_checking_subtask", State.MERGED),
    ("enter_triaging_subtask", State.MERGED),
    ("enter_committing", State.MERGED),
    ("enter_pushing", State.MERGED),
    ("enter_local_ci_checking", State.MERGED),
    ("enter_audit_local_ci", State.MERGED),
    # `enter_fixup_planning`: PENDING_CI is a real-world invalid source
    # (plan 54's regression) — keep it here so the test mirrors the
    # production crash path.
    ("enter_fixup_planning", State.PENDING_CI),
    ("enter_pr_opening", State.MERGED),
    ("enter_pending_ci", State.MERGED),
    ("enter_awaiting_review", State.MERGED),
    # Plan 58: post-PR fixup entry helpers — MERGED is invalid.
    ("enter_audit_cycle_for_ci_fixup", State.MERGED),
    ("enter_audit_cycle_for_review_fixup", State.MERGED),
    ("enter_rebasing_to_main", State.MERGED),
    ("enter_conflict_resolving", State.MERGED),
]


@pytest.mark.parametrize(("helper_name", "invalid_state"), _INVALID_SOURCES)
def test_enter_helper_from_invalid_state_returns_none_and_logs(
    helper_name: str,
    invalid_state: State,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _store_with_task(tmp_path, state=invalid_state)
    helper = getattr(fsm_runtime, helper_name)

    with caplog.at_level(logging.INFO, logger="quikode.fsm_runtime"):
        result = helper(store, "R-1", note="should skip")

    assert result is None, f"{helper_name} from {invalid_state} should return None"
    # State unchanged.
    assert store.get("R-1")["state"] == invalid_state.value
    # INFO log captured with the helper name + the source state.
    assert any(
        f"fsm_runtime.{helper_name}: skipping" in rec.getMessage() and invalid_state.value in rec.getMessage()
        for rec in caplog.records
    ), (
        f"expected INFO log naming {helper_name} + {invalid_state.value}; got {[r.getMessage() for r in caplog.records]}"
    )
    store.conn.close()


@pytest.mark.parametrize(("helper_name", "valid_source", "expected"), _VALID_TRANSITIONS)
def test_enter_helper_from_valid_state_transitions(
    helper_name: str,
    valid_source: State,
    expected: State,
    tmp_path: Path,
) -> None:
    store = _store_with_task(tmp_path, state=valid_source)
    helper = getattr(fsm_runtime, helper_name)

    result = helper(store, "R-1", note="should transition")

    assert result is expected, f"{helper_name} from {valid_source} should yield {expected}"
    assert store.get("R-1")["state"] == expected.value
    store.conn.close()


def test_mark_merged_from_invalid_state_returns_none_and_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Plan 57: `mark_merged` from a state outside the recognized set
    (PENDING / PENDING_CI / AWAITING_REVIEW / side / terminal states)
    returns None instead of raising. PROVISIONING is a state with no
    bridge path to MERGED, so it exercises the final fall-through.
    """
    store = _store_with_task(tmp_path, state=State.PROVISIONING)

    with caplog.at_level(logging.INFO, logger="quikode.fsm_runtime"):
        result = fsm_runtime.mark_merged(store, "R-1", note="should skip")

    assert result is None
    assert store.get("R-1")["state"] == State.PROVISIONING.value
    assert any(
        "fsm_runtime.mark_merged: skipping" in rec.getMessage()
        and State.PROVISIONING.value in rec.getMessage()
        for rec in caplog.records
    )
    store.conn.close()


def test_mark_merged_idempotent_on_merged(tmp_path: Path) -> None:
    """Idempotent: calling `mark_merged` against an already-MERGED row
    is a no-op that returns `State.MERGED`."""
    store = _store_with_task(tmp_path, state=State.MERGED)
    result = fsm_runtime.mark_merged(store, "R-1")
    assert result is State.MERGED
    assert store.get("R-1")["state"] == State.MERGED.value
    store.conn.close()


def test_block_current_from_invalid_state_returns_none_and_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Plan 57: `block_current` from a state with no BLOCK_TASK
    transition (terminal states like MERGED) returns None instead of
    raising. Pre-plan-57 the underlying `apply_event` raised
    `InvalidTransition` and crashed the worker if anyone called
    `block_current` against a terminal row.
    """
    store = _store_with_task(tmp_path, state=State.MERGED)

    with caplog.at_level(logging.INFO, logger="quikode.fsm_runtime"):
        result = fsm_runtime.block_current(store, "R-1", note="should skip")

    assert result is None
    assert store.get("R-1")["state"] == State.MERGED.value
    assert any(
        "fsm_runtime.block_current: skipping" in rec.getMessage() and State.MERGED.value in rec.getMessage()
        for rec in caplog.records
    )
    store.conn.close()


def test_block_current_from_active_state_fires_block_task(tmp_path: Path) -> None:
    """Sanity: `block_current` from an active state without a
    state-specific exhaustion event (e.g. PROVISIONING) fires the
    fallback BLOCK_TASK event and lands in BLOCKED."""
    store = _store_with_task(tmp_path, state=State.PROVISIONING)
    result = fsm_runtime.block_current(store, "R-1", note="forced block")
    assert result is State.BLOCKED
    assert store.get("R-1")["state"] == State.BLOCKED.value
    store.conn.close()


def test_block_current_from_audit_stage_lands_in_blocked(
    tmp_path: Path,
) -> None:
    """Plan 58: ADDRESSING_FEEDBACK retired. Audit-stage states use the
    generic BLOCK_TASK transition (no state-specific exhaustion event).
    The result must still be BLOCKED — verifies the generic event path
    inside `block_current`."""
    store = _store_with_task(tmp_path, state=State.AUDIT_LOCAL_CI)
    result = fsm_runtime.block_current(store, "R-1", note="audit blocked")
    assert result is State.BLOCKED
    assert store.get("R-1")["state"] == State.BLOCKED.value
    store.conn.close()
