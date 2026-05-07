"""Runtime helpers for applying canonical FSM events."""

from __future__ import annotations

from typing import Any

from quikode.fsm import Event, InvalidTransition, State


def current_state(store: Any, task_id: str) -> State:
    row = store.get(task_id)
    if row is None:
        raise InvalidTransition(f"task does not exist: {task_id}")
    return State(str(row["state"]))


def start_task(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    return store.apply_event(task_id, Event.START_TASK, note=note, **fields)


def environment_ready(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    return store.apply_event(task_id, Event.ENVIRONMENT_READY, note=note, **fields)


def enter_doing_subtask(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    state = current_state(store, task_id)
    if state is State.ADDRESSING_FEEDBACK:
        return state
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
        raise InvalidTransition(f"cannot enter doing_subtask from {state.value}")
    return store.apply_event(task_id, event, note=note, **fields)


def enter_checking_subtask(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    state = current_state(store, task_id)
    if state in {State.ADDRESSING_FEEDBACK, State.CHECKING_SUBTASK}:
        return state
    return store.apply_event(task_id, Event.DOER_DONE, note=note, **fields)


def enter_triaging_subtask(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    if current_state(store, task_id) is State.ADDRESSING_FEEDBACK:
        return State.ADDRESSING_FEEDBACK
    return store.apply_event(task_id, Event.SUBTASK_FAILED, note=note, **fields)


def enter_committing(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    return store.apply_event(task_id, Event.SUBTASK_PASSED, note=note, **fields)


def enter_pushing(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    return store.apply_event(task_id, Event.COMMIT_CREATED, note=note, **fields)


def enter_local_ci_checking(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    state = current_state(store, task_id)
    if state is State.LOCAL_CI_CHECKING:
        return state
    return store.apply_event(task_id, Event.ALL_SUBTASKS_DONE, note=note, **fields)


def enter_pre_pr_auditing(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    state = current_state(store, task_id)
    if state is State.PRE_PR_AUDITING:
        return state
    return store.apply_event(task_id, Event.LOCAL_CI_PASSED, note=note, **fields)


def enter_fixup_planning(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    state = current_state(store, task_id)
    if state is State.FIXUP_PLANNING:
        return state
    event_by_state = {
        State.LOCAL_CI_CHECKING: Event.LOCAL_CI_FAILED,
        State.PRE_PR_AUDITING: Event.AUDIT_FAILED,
    }
    event = event_by_state.get(state)
    if event is None:
        raise InvalidTransition(f"cannot enter fixup_planning from {state.value}")
    return store.apply_event(task_id, event, note=note, **fields)


def enter_pr_opening(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    return store.apply_event(task_id, Event.AUDIT_PASSED, note=note, **fields)


def enter_pending_ci(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    """Plan 28: PENDING_CI is reached from PR_OPENING (PR_OPENED), from
    ADDRESSING_FEEDBACK after fixup-push (FEEDBACK_PUSHED), or from
    REBASING_TO_MAIN after rebase-push (REBASE_PUSHED). The pre-plan-28
    `NO_ACTIONABLE_FEEDBACK` path retired with `TRIAGING_FEEDBACK`.
    """
    state = current_state(store, task_id)
    if state is State.PENDING_CI:
        return state
    event_by_state = {
        State.PR_OPENING: Event.PR_OPENED,
        State.ADDRESSING_FEEDBACK: Event.FEEDBACK_PUSHED,
        State.REBASING_TO_MAIN: Event.REBASE_PUSHED,
    }
    event = event_by_state.get(state)
    if event is None:
        raise InvalidTransition(f"cannot enter pending_ci from {state.value}")
    return store.apply_event(task_id, event, note=note, **fields)


def enter_awaiting_review(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    """Plan 28: AWAITING_REVIEW is reached from PENDING_CI on CI_PASSED. The
    pre-plan-28 MERGE_READY → AWAITING_REVIEW idempotency arm retired with
    the settle window."""
    state = current_state(store, task_id)
    if state is State.AWAITING_REVIEW:
        return state
    return store.apply_event(task_id, Event.CI_PASSED, note=note, **fields)


def enter_addressing_feedback(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    """Plan 28: ADDRESSING_FEEDBACK reached directly from PENDING_CI (CI fail)
    or AWAITING_REVIEW (CI flake or CHANGES_REQUESTED). No intermediate
    classifier state — the bundled context (CI excerpt, threads, PR comments,
    review bodies) is the input to the fixup planner."""
    state = current_state(store, task_id)
    if state is State.ADDRESSING_FEEDBACK:
        return state
    if state is State.PENDING_CI:
        return store.apply_event(task_id, Event.CI_FAILED, note=note, **fields)
    if state is State.AWAITING_REVIEW:
        # Caller picks the trigger by passing event=… — default to
        # CHANGES_REQUESTED since CI flake is the rarer case.
        event = fields.pop("event", None) or Event.CHANGES_REQUESTED_RECEIVED
        return store.apply_event(task_id, event, note=note, **fields)
    raise InvalidTransition(f"cannot enter addressing_feedback from {state.value}")


def mark_merged(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    """Plan 28: MERGED reached from PENDING (seed-from-base via MARK_MERGED),
    PENDING_CI (CI_PASSED → AWAITING_REVIEW → MERGED), or AWAITING_REVIEW
    directly. The pre-plan-28 SETTLE_WINDOW_ELAPSED hop retired."""
    state = current_state(store, task_id)
    if state is State.PENDING:
        return store.apply_event(task_id, Event.MARK_MERGED, note=note, **fields)
    if state is State.PENDING_CI:
        store.apply_event(task_id, Event.CI_PASSED, note=note)
        state = State.AWAITING_REVIEW
    return store.apply_event(task_id, Event.MERGED, note=note, **fields)


def enter_rebasing_to_main(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    state = current_state(store, task_id)
    if state is State.REBASING_TO_MAIN:
        return state
    if state is State.CONFLICT_RESOLVING:
        return store.apply_event(task_id, Event.RESOLVED, note=note, **fields)
    return store.apply_event(task_id, Event.PARENT_MERGED_OR_CONFLICT, note=note, **fields)


def enter_conflict_resolving(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    return store.apply_event(task_id, Event.CONFLICT, note=note, **fields)


def block_current(store: Any, task_id: str, *, note: str | None = None, **fields: Any) -> State:
    """Plan 28: TRIAGING_FEEDBACK retired. ADDRESSING_FEEDBACK can still
    block via FEEDBACK_EXHAUSTED."""
    state = current_state(store, task_id)
    event_by_state = {
        State.TRIAGING_SUBTASK: Event.RETRY_EXHAUSTED,
        State.FIXUP_PLANNING: Event.FIXUP_EXHAUSTED,
        State.ADDRESSING_FEEDBACK: Event.FEEDBACK_EXHAUSTED,
        State.CONFLICT_RESOLVING: Event.UNRESOLVED,
    }
    event = event_by_state.get(state)
    if event is None:
        return store.apply_event(task_id, Event.BLOCK_TASK, note=note, **fields)
    return store.apply_event(task_id, event, note=note, **fields)


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
