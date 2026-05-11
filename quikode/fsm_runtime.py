"""Runtime helpers for applying canonical FSM events.

Plan 57: every `enter_*` helper, plus `mark_merged` and `block_current`,
returns `State | None` and silently skips (with an INFO log naming the
helper + the current state) instead of raising `InvalidTransition` when
the task's current state doesn't allow the helper's transition. The
helpers are safe to call fire-and-forget from any worker/watcher path;
the underlying `store.apply_event` still raises on invalid transitions
(plan 57 only relaxes the helper layer). Plan-49 / plan-54 per-call-site
guards remain as defense-in-depth — they short-circuit upstream and
keep the call-site rationale explicit.

Plan 58: PRE_PR_AUDITING / ADDRESSING_FEEDBACK retire. The audit-stage
helpers (`enter_audit_local_ci` ... `enter_audit_behavior`) cover the
new first-class states. `enter_addressing_feedback` is replaced by
`enter_audit_local_ci_from_pr_review` (and the equivalent CI / review
variants) — the explicit name signals "re-enter the gauntlet for a
post-PR fixup cycle". The plan-54 no-op guards on
`enter_doing_subtask` / `enter_checking_subtask` / `enter_triaging_subtask`
are removed: these states are now the natural place for the per-subtask
loop regardless of trigger source.
"""

from __future__ import annotations

import logging
from typing import Any

from quikode.fsm import Event, InvalidTransition, State

log = logging.getLogger("quikode.fsm_runtime")


# Plan 58: stage names map (state, advance-event, fail-event). The driver
# uses this to look up the right event after running each audit agent.
_AUDIT_STAGE_EVENTS: dict[State, tuple[Event, Event]] = {
    State.AUDIT_LOCAL_CI: (Event.AUDIT_LOCAL_CI_PASSED, Event.AUDIT_LOCAL_CI_FAILED),
    State.AUDIT_RUBRIC: (Event.AUDIT_RUBRIC_PASSED, Event.AUDIT_RUBRIC_FAILED),
    State.AUDIT_STANDARDS: (Event.AUDIT_STANDARDS_PASSED, Event.AUDIT_STANDARDS_FAILED),
    State.AUDIT_ARCHITECTURE: (Event.AUDIT_ARCHITECTURE_PASSED, Event.AUDIT_ARCHITECTURE_FAILED),
    State.AUDIT_BEHAVIOR: (Event.AUDIT_BEHAVIOR_PASSED, Event.AUDIT_BEHAVIOR_FAILED),
}


def current_state(store: Any, task_id: str) -> State:
    row = store.get(task_id)
    if row is None:
        raise InvalidTransition(f"task does not exist: {task_id}")
    return State(str(row["state"]))


def start_task(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    return store.apply_event(task_id, Event.START_TASK, note=note, **fields)


def environment_ready(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    return store.apply_event(task_id, Event.ENVIRONMENT_READY, note=note, **fields)


def enter_doing_subtask(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State | None:
    state = current_state(store, task_id)
    if state is State.DOING_SUBTASK:
        return state
    event_by_state = {
        State.PLANNING: Event.PLAN_VALID,
        State.TRIAGING_SUBTASK: Event.RETRY_SUBTASK,
        State.FIXUP_PLANNING: Event.FIXUP_PLAN_VALID,
        State.PUSHING: Event.MORE_SUBTASKS,
    }
    event = event_by_state.get(state)
    if event is None:
        log.info(
            "fsm_runtime.enter_doing_subtask: skipping (current state %r doesn't allow this transition)",
            state.value,
        )
        return None
    return store.apply_event(task_id, event, note=note, **fields)


def enter_checking_subtask(
    store: Any, task_id: str, *, note: str | None = None, **fields: Any
) -> State | None:
    state = current_state(store, task_id)
    if state is State.CHECKING_SUBTASK:
        return state
    if state is not State.DOING_SUBTASK:
        log.info(
            "fsm_runtime.enter_checking_subtask: skipping (current state %r doesn't allow this transition)",
            state.value,
        )
        return None
    return store.apply_event(task_id, Event.DOER_DONE, note=note, **fields)


def enter_triaging_subtask(
    store: Any, task_id: str, *, note: str | None = None, **fields: Any
) -> State | None:
    state = current_state(store, task_id)
    if state is State.TRIAGING_SUBTASK:
        return state
    if state is not State.CHECKING_SUBTASK:
        log.info(
            "fsm_runtime.enter_triaging_subtask: skipping (current state %r doesn't allow this transition)",
            state.value,
        )
        return None
    return store.apply_event(task_id, Event.SUBTASK_FAILED, note=note, **fields)


def enter_committing(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State | None:
    state = current_state(store, task_id)
    if state is not State.CHECKING_SUBTASK:
        log.info(
            "fsm_runtime.enter_committing: skipping (current state %r doesn't allow this transition)",
            state.value,
        )
        return None
    return store.apply_event(task_id, Event.SUBTASK_PASSED, note=note, **fields)


def enter_pushing(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State | None:
    state = current_state(store, task_id)
    if state is not State.COMMITTING:
        log.info(
            "fsm_runtime.enter_pushing: skipping (current state %r doesn't allow this transition)",
            state.value,
        )
        return None
    return store.apply_event(task_id, Event.COMMIT_CREATED, note=note, **fields)


def enter_local_ci_checking(
    store: Any, task_id: str, *, note: str | None = None, **fields: Any
) -> State | None:
    state = current_state(store, task_id)
    if state is State.LOCAL_CI_CHECKING:
        return state
    if state is not State.PUSHING:
        log.info(
            "fsm_runtime.enter_local_ci_checking: skipping (current state %r doesn't allow this transition)",
            state.value,
        )
        return None
    return store.apply_event(task_id, Event.ALL_SUBTASKS_DONE, note=note, **fields)


def enter_audit_local_ci(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State | None:
    """Plan 58: enter the audit gauntlet's first stage.

    Valid from:
      - `LOCAL_CI_CHECKING`           (initial-audit start; fires LOCAL_CI_PASSED)
      - `PENDING_CI`                  (CI fixup start; fires CI_FIXUP_START)
      - `AWAITING_REVIEW`             (CI flake / review-feedback start; fires
                                       CI_FIXUP_START or REVIEW_FIXUP_START)
      - any audit-stage state         (re-entry after fixup-then-recycle, but
                                       this case is more naturally driven
                                       through FIXUP_PLANNING then back)

    The caller may pass `event=` to disambiguate AWAITING_REVIEW's two
    possible source events; default is REVIEW_FIXUP_START.
    """
    state = current_state(store, task_id)
    if state is State.AUDIT_LOCAL_CI:
        return state
    if state is State.LOCAL_CI_CHECKING:
        return store.apply_event(task_id, Event.LOCAL_CI_PASSED, note=note, **fields)
    if state is State.PENDING_CI:
        return store.apply_event(task_id, Event.CI_FIXUP_START, note=note, **fields)
    if state is State.AWAITING_REVIEW:
        event = fields.pop("event", None) or Event.REVIEW_FIXUP_START
        return store.apply_event(task_id, event, note=note, **fields)
    log.info(
        "fsm_runtime.enter_audit_local_ci: skipping (current state %r doesn't allow this transition)",
        state.value,
    )
    return None


def _advance_audit_stage(
    store: Any, task_id: str, *, passed: bool, expected_state: State, note: str | None, **fields: Any
) -> State | None:
    """Plan 58 internal: fire the passed/failed event for the named audit stage.

    Silently skips when the task is not currently in `expected_state` (plan-57
    pattern). On pass, advances to the next stage; on fail, diverts to
    FIXUP_PLANNING.
    """
    state = current_state(store, task_id)
    if state is not expected_state:
        log.info(
            "fsm_runtime._advance_audit_stage(%s, passed=%s): skipping (current state %r)",
            expected_state.value,
            passed,
            state.value,
        )
        return None
    passed_event, failed_event = _AUDIT_STAGE_EVENTS[expected_state]
    event = passed_event if passed else failed_event
    return store.apply_event(task_id, event, note=note, **fields)


def audit_local_ci_passed(
    store: Any, task_id: str, *, note: str | None = None, **fields: Any
) -> State | None:
    return _advance_audit_stage(
        store, task_id, passed=True, expected_state=State.AUDIT_LOCAL_CI, note=note, **fields
    )


def audit_local_ci_failed(
    store: Any, task_id: str, *, note: str | None = None, **fields: Any
) -> State | None:
    return _advance_audit_stage(
        store, task_id, passed=False, expected_state=State.AUDIT_LOCAL_CI, note=note, **fields
    )


def enter_audit_rubric(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State | None:
    """Plan 58: advance from AUDIT_LOCAL_CI to AUDIT_RUBRIC on stage pass."""
    return audit_local_ci_passed(store, task_id, note=note, **fields)


def audit_rubric_passed(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State | None:
    return _advance_audit_stage(
        store, task_id, passed=True, expected_state=State.AUDIT_RUBRIC, note=note, **fields
    )


def audit_rubric_failed(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State | None:
    return _advance_audit_stage(
        store, task_id, passed=False, expected_state=State.AUDIT_RUBRIC, note=note, **fields
    )


def enter_audit_standards(
    store: Any, task_id: str, *, note: str | None = None, **fields: Any
) -> State | None:
    return audit_rubric_passed(store, task_id, note=note, **fields)


def audit_standards_passed(
    store: Any, task_id: str, *, note: str | None = None, **fields: Any
) -> State | None:
    return _advance_audit_stage(
        store, task_id, passed=True, expected_state=State.AUDIT_STANDARDS, note=note, **fields
    )


def audit_standards_failed(
    store: Any, task_id: str, *, note: str | None = None, **fields: Any
) -> State | None:
    return _advance_audit_stage(
        store, task_id, passed=False, expected_state=State.AUDIT_STANDARDS, note=note, **fields
    )


def enter_audit_architecture(
    store: Any, task_id: str, *, note: str | None = None, **fields: Any
) -> State | None:
    return audit_standards_passed(store, task_id, note=note, **fields)


def audit_architecture_passed(
    store: Any, task_id: str, *, note: str | None = None, **fields: Any
) -> State | None:
    return _advance_audit_stage(
        store, task_id, passed=True, expected_state=State.AUDIT_ARCHITECTURE, note=note, **fields
    )


def audit_architecture_failed(
    store: Any, task_id: str, *, note: str | None = None, **fields: Any
) -> State | None:
    return _advance_audit_stage(
        store, task_id, passed=False, expected_state=State.AUDIT_ARCHITECTURE, note=note, **fields
    )


def enter_audit_behavior(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State | None:
    return audit_architecture_passed(store, task_id, note=note, **fields)


def audit_behavior_passed(
    store: Any, task_id: str, *, note: str | None = None, **fields: Any
) -> State | None:
    return _advance_audit_stage(
        store, task_id, passed=True, expected_state=State.AUDIT_BEHAVIOR, note=note, **fields
    )


def audit_behavior_failed(
    store: Any, task_id: str, *, note: str | None = None, **fields: Any
) -> State | None:
    return _advance_audit_stage(
        store, task_id, passed=False, expected_state=State.AUDIT_BEHAVIOR, note=note, **fields
    )


def enter_fixup_planning(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State | None:
    """Plan 58: FIXUP_PLANNING is now reachable from any of the audit-stage
    states plus LOCAL_CI_CHECKING. The driver looks up the right
    stage-failed event for the current state."""
    state = current_state(store, task_id)
    if state is State.FIXUP_PLANNING:
        return state
    event_by_state: dict[State, Event] = {
        State.LOCAL_CI_CHECKING: Event.LOCAL_CI_FAILED,
        State.AUDIT_LOCAL_CI: Event.AUDIT_LOCAL_CI_FAILED,
        State.AUDIT_RUBRIC: Event.AUDIT_RUBRIC_FAILED,
        State.AUDIT_STANDARDS: Event.AUDIT_STANDARDS_FAILED,
        State.AUDIT_ARCHITECTURE: Event.AUDIT_ARCHITECTURE_FAILED,
        State.AUDIT_BEHAVIOR: Event.AUDIT_BEHAVIOR_FAILED,
    }
    event = event_by_state.get(state)
    if event is None:
        log.info(
            "fsm_runtime.enter_fixup_planning: skipping (current state %r doesn't allow this transition)",
            state.value,
        )
        return None
    return store.apply_event(task_id, event, note=note, **fields)


def enter_pr_opening(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State | None:
    """Plan 58: PR_OPENING now arrives via AUDIT_BEHAVIOR_PASSED. For
    post-PR fixup cycles the driver fires the same event then `_open_pr`
    short-circuits on `pr_number` already set."""
    state = current_state(store, task_id)
    if state is State.PR_OPENING:
        return state
    if state is not State.AUDIT_BEHAVIOR:
        log.info(
            "fsm_runtime.enter_pr_opening: skipping (current state %r doesn't allow this transition)",
            state.value,
        )
        return None
    return store.apply_event(task_id, Event.AUDIT_BEHAVIOR_PASSED, note=note, **fields)


def enter_pending_ci(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State | None:
    """Plan 28/58: PENDING_CI is reached from PR_OPENING (PR_OPENED), or
    from REBASING_TO_MAIN after rebase-push (REBASE_PUSHED). Post-PR
    fixup cycles re-enter the gauntlet, then exit via AUDIT_BEHAVIOR_PASSED
    → PR_OPENING (which reuses the existing PR) → PR_OPENED → PENDING_CI."""
    state = current_state(store, task_id)
    if state is State.PENDING_CI:
        return state
    event_by_state = {
        State.PR_OPENING: Event.PR_OPENED,
        State.REBASING_TO_MAIN: Event.REBASE_PUSHED,
    }
    event = event_by_state.get(state)
    if event is None:
        log.info(
            "fsm_runtime.enter_pending_ci: skipping (current state %r doesn't allow this transition)",
            state.value,
        )
        return None
    return store.apply_event(task_id, event, note=note, **fields)


def enter_awaiting_review(
    store: Any, task_id: str, *, note: str | None = None, **fields: Any
) -> State | None:
    """Plan 28: AWAITING_REVIEW is reached from PENDING_CI on CI_PASSED."""
    state = current_state(store, task_id)
    if state is State.AWAITING_REVIEW:
        return state
    if state is not State.PENDING_CI:
        log.info(
            "fsm_runtime.enter_awaiting_review: skipping (current state %r doesn't allow this transition)",
            state.value,
        )
        return None
    return store.apply_event(task_id, Event.CI_PASSED, note=note, **fields)


def enter_audit_cycle_for_ci_fixup(
    store: Any, task_id: str, *, note: str | None = None, **fields: Any
) -> State | None:
    """Plan 58 replacement for `enter_addressing_feedback` on the CI-failure
    path. PENDING_CI / AWAITING_REVIEW → AUDIT_LOCAL_CI via CI_FIXUP_START.

    Returns None when the task is not in a valid source state (typed-guard
    pattern from plan 57)."""
    state = current_state(store, task_id)
    if state in _AUDIT_STAGE_EVENTS:
        return state
    if state not in {State.PENDING_CI, State.AWAITING_REVIEW}:
        log.info(
            "fsm_runtime.enter_audit_cycle_for_ci_fixup: skipping (current state %r)",
            state.value,
        )
        return None
    return store.apply_event(task_id, Event.CI_FIXUP_START, note=note, **fields)


def enter_audit_cycle_for_review_fixup(
    store: Any, task_id: str, *, note: str | None = None, **fields: Any
) -> State | None:
    """Plan 58 replacement for `enter_addressing_feedback` on the
    CHANGES_REQUESTED path. AWAITING_REVIEW → AUDIT_LOCAL_CI via
    REVIEW_FIXUP_START."""
    state = current_state(store, task_id)
    if state in _AUDIT_STAGE_EVENTS:
        return state
    if state is not State.AWAITING_REVIEW:
        log.info(
            "fsm_runtime.enter_audit_cycle_for_review_fixup: skipping (current state %r)",
            state.value,
        )
        return None
    return store.apply_event(task_id, Event.REVIEW_FIXUP_START, note=note, **fields)


def mark_merged(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State | None:
    """Plan 28/58: MERGED reached from PENDING (seed-from-base via MARK_MERGED),
    PENDING_CI (CI_PASSED → AWAITING_REVIEW → MERGED), or AWAITING_REVIEW
    directly.

    Plan 56: bridge side states (REBASING_TO_MAIN / audit-stage states /
    terminal failure states) through PENDING via BLOCK_TASK + RETRY_TASK.

    Plan 57: any state outside the recognized bridge set returns None +
    logs INFO instead of raising InvalidTransition.

    Plan 58: ADDRESSING_FEEDBACK retired; audit-stage states join the
    side-state bridge.
    """
    state = current_state(store, task_id)
    if state is State.MERGED:
        return state
    if state is State.PENDING:
        return store.apply_event(task_id, Event.MARK_MERGED, note=note, **fields)
    if state is State.PENDING_CI:
        store.apply_event(task_id, Event.CI_PASSED, note=note)
        state = State.AWAITING_REVIEW
    if state is State.AWAITING_REVIEW:
        return store.apply_event(task_id, Event.MERGED, note=note, **fields)
    bridge_note = note or "auto-merge via ancestry: bridging side/terminal state to MERGED"
    if state is State.CONFLICT_RESOLVING:
        store.apply_event(task_id, Event.UNRESOLVED, note=bridge_note)
        state = State.BLOCKED
    elif state is State.REBASING_TO_MAIN or state in _AUDIT_STAGE_EVENTS:
        store.apply_event(task_id, Event.BLOCK_TASK, note=bridge_note)
        state = State.BLOCKED
    if state in {State.BLOCKED, State.FAILED, State.ABORTED}:
        store.apply_event(task_id, Event.RETRY_TASK, note=bridge_note)
        return store.apply_event(task_id, Event.MARK_MERGED, note=note, **fields)
    log.info(
        "fsm_runtime.mark_merged: skipping (current state %r doesn't allow this transition)",
        state.value,
    )
    return None


def enter_rebasing_to_main(
    store: Any, task_id: str, *, note: str | None = None, **fields: Any
) -> State | None:
    state = current_state(store, task_id)
    if state is State.REBASING_TO_MAIN:
        return state
    if state is State.CONFLICT_RESOLVING:
        return store.apply_event(task_id, Event.RESOLVED, note=note, **fields)
    try:
        return store.apply_event(task_id, Event.PARENT_MERGED_OR_CONFLICT, note=note, **fields)
    except InvalidTransition:
        log.info(
            "fsm_runtime.enter_rebasing_to_main: skipping (current state %r doesn't allow this transition)",
            state.value,
        )
        return None


def enter_conflict_resolving(
    store: Any, task_id: str, *, note: str | None = None, **fields: Any
) -> State | None:
    state = current_state(store, task_id)
    if state is not State.REBASING_TO_MAIN:
        log.info(
            "fsm_runtime.enter_conflict_resolving: skipping (current state %r doesn't allow this transition)",
            state.value,
        )
        return None
    return store.apply_event(task_id, Event.CONFLICT, note=note, **fields)


def block_current(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State | None:
    """Plan 28/57/58: BLOCK_TASK from any active state. FIXUP_PLANNING uses
    FIXUP_EXHAUSTED; TRIAGING_SUBTASK uses RETRY_EXHAUSTED.

    Plan 57: silently skip on invalid source states.
    """
    state = current_state(store, task_id)
    event_by_state = {
        State.TRIAGING_SUBTASK: Event.RETRY_EXHAUSTED,
        State.FIXUP_PLANNING: Event.FIXUP_EXHAUSTED,
        State.CONFLICT_RESOLVING: Event.UNRESOLVED,
    }
    event = event_by_state.get(state, Event.BLOCK_TASK)
    try:
        return store.apply_event(task_id, event, note=note, **fields)
    except InvalidTransition:
        log.info(
            "fsm_runtime.block_current: skipping (current state %r doesn't allow this transition)",
            state.value,
        )
        return None


def force_recover_to_pending_ci(
    store: Any, task_id: str, *, note: str | None = None, **fields: Any
) -> State | None:
    """Plan 58: supervisor escape hatch — force a stalled audit-stage task
    back to PENDING_CI so the next watcher tick can re-dispatch. This is
    an out-of-band recovery: there's no FSM event that maps "any state
    → PENDING_CI", so we delegate to the store's raw `transition` helper
    with a clear note recording the supervisor's rationale. The
    architecture guard explicitly allows this single call site."""
    state = current_state(store, task_id)
    if state is State.PENDING_CI:
        return state
    bridge_note = note or "supervisor force-recovery: bridging to PENDING_CI"
    store.transition(task_id, State.PENDING_CI, note=bridge_note, **fields)
    return State.PENDING_CI


def crash_current(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    return store.apply_event(task_id, Event.CRASH, note=note, **fields)


def abort_pending(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    return store.apply_event(task_id, Event.ABORT, note=note, **fields)


def pr_closed(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    return store.apply_event(task_id, Event.PR_CLOSED, note=note, **fields)


def retry_task(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    return store.apply_event(task_id, Event.RETRY_TASK, note=note, **fields)


def resume_task(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    return store.apply_event(task_id, Event.RESUME_TASK, note=note, **fields)


def merge_node_built(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    """Plan 32/58: AUDIT_BEHAVIOR → MERGE_NODE_READY for `kind="merge"` rows.

    Spec tasks fire AUDIT_BEHAVIOR_PASSED → PR_OPENING; merge-nodes never
    open a PR, so they fire MERGE_NODE_BUILT and become an integration
    artifact serving as the effective base for downstream multi-parent
    children.
    """
    return store.apply_event(task_id, Event.MERGE_NODE_BUILT, note=note, **fields)
