"""Canonical task lifecycle.

This module is the source of truth for the supported task states and
event-driven transitions.
"""

from __future__ import annotations

from enum import StrEnum


class InvalidTransition(ValueError):
    """Raised when an event is not valid from the task's current state."""


class State(StrEnum):
    PENDING = "pending"
    PROVISIONING = "provisioning"
    PLANNING = "planning"
    DOING_SUBTASK = "doing_subtask"
    CHECKING_SUBTASK = "checking_subtask"
    TRIAGING_SUBTASK = "triaging_subtask"
    COMMITTING = "committing"
    PUSHING = "pushing"
    LOCAL_CI_CHECKING = "local_ci_checking"
    PRE_PR_AUDITING = "pre_pr_auditing"
    FIXUP_PLANNING = "fixup_planning"
    PR_OPENING = "pr_opening"
    PENDING_CI = "pending_ci"
    AWAITING_REVIEW = "awaiting_review"
    MERGE_READY = "merge_ready"
    TRIAGING_FEEDBACK = "triaging_feedback"
    ADDRESSING_FEEDBACK = "addressing_feedback"
    REBASING_TO_MAIN = "rebasing_to_main"
    CONFLICT_RESOLVING = "conflict_resolving"
    MERGED = "merged"
    BLOCKED = "blocked"
    FAILED = "failed"
    ABORTED = "aborted"


class Event(StrEnum):
    START_TASK = "start_task"
    ENVIRONMENT_READY = "environment_ready"
    PLAN_VALID = "plan_valid"
    DOER_DONE = "doer_done"
    SUBTASK_PASSED = "subtask_passed"
    SUBTASK_FAILED = "subtask_failed"
    RETRY_SUBTASK = "retry_subtask"
    RETRY_EXHAUSTED = "retry_exhausted"
    COMMIT_CREATED = "commit_created"
    MORE_SUBTASKS = "more_subtasks"
    ALL_SUBTASKS_DONE = "all_subtasks_done"
    LOCAL_CI_PASSED = "local_ci_passed"
    LOCAL_CI_FAILED = "local_ci_failed"
    AUDIT_PASSED = "audit_passed"
    AUDIT_FAILED = "audit_failed"
    FIXUP_PLAN_VALID = "fixup_plan_valid"
    FIXUP_EXHAUSTED = "fixup_exhausted"
    PR_OPENED = "pr_opened"
    CI_GREEN_THREADS_CLEAN = "ci_green_threads_clean"
    SETTLE_WINDOW_ELAPSED = "settle_window_elapsed"
    MERGED = "merged"
    CI_FAILED_OR_THREADS_FOUND = "ci_failed_or_threads_found"
    THREADS_FOUND = "threads_found"
    ACTIONABLE_FEEDBACK = "actionable_feedback"
    NO_ACTIONABLE_FEEDBACK = "no_actionable_feedback"
    FEEDBACK_PUSHED = "feedback_pushed"
    FEEDBACK_EXHAUSTED = "feedback_exhausted"
    PARENT_MERGED_OR_CONFLICT = "parent_merged_or_conflict"
    REBASE_PUSHED = "rebase_pushed"
    CONFLICT = "conflict"
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    ABORT = "abort"
    CRASH = "crash"
    RETRY_TASK = "retry_task"
    RESUME_TASK = "resume_task"
    MARK_MERGED = "mark_merged"
    PR_CLOSED = "pr_closed"
    BLOCK_TASK = "block_task"


TRANSITIONS: dict[tuple[State, Event], State] = {
    (State.PENDING, Event.START_TASK): State.PROVISIONING,
    (State.PROVISIONING, Event.ENVIRONMENT_READY): State.PLANNING,
    (State.PLANNING, Event.PLAN_VALID): State.DOING_SUBTASK,
    (State.DOING_SUBTASK, Event.DOER_DONE): State.CHECKING_SUBTASK,
    (State.CHECKING_SUBTASK, Event.SUBTASK_PASSED): State.COMMITTING,
    (State.CHECKING_SUBTASK, Event.SUBTASK_FAILED): State.TRIAGING_SUBTASK,
    (State.TRIAGING_SUBTASK, Event.RETRY_SUBTASK): State.DOING_SUBTASK,
    (State.TRIAGING_SUBTASK, Event.RETRY_EXHAUSTED): State.BLOCKED,
    (State.COMMITTING, Event.COMMIT_CREATED): State.PUSHING,
    (State.PUSHING, Event.MORE_SUBTASKS): State.DOING_SUBTASK,
    (State.PUSHING, Event.ALL_SUBTASKS_DONE): State.LOCAL_CI_CHECKING,
    (State.LOCAL_CI_CHECKING, Event.LOCAL_CI_PASSED): State.PRE_PR_AUDITING,
    (State.LOCAL_CI_CHECKING, Event.LOCAL_CI_FAILED): State.FIXUP_PLANNING,
    (State.PRE_PR_AUDITING, Event.AUDIT_PASSED): State.PR_OPENING,
    (State.PRE_PR_AUDITING, Event.AUDIT_FAILED): State.FIXUP_PLANNING,
    (State.FIXUP_PLANNING, Event.FIXUP_PLAN_VALID): State.DOING_SUBTASK,
    (State.FIXUP_PLANNING, Event.FIXUP_EXHAUSTED): State.BLOCKED,
    (State.PR_OPENING, Event.PR_OPENED): State.PENDING_CI,
    (State.PENDING_CI, Event.CI_GREEN_THREADS_CLEAN): State.AWAITING_REVIEW,
    (State.AWAITING_REVIEW, Event.SETTLE_WINDOW_ELAPSED): State.MERGE_READY,
    (State.MERGE_READY, Event.MERGED): State.MERGED,
    (State.PENDING_CI, Event.CI_FAILED_OR_THREADS_FOUND): State.TRIAGING_FEEDBACK,
    (State.AWAITING_REVIEW, Event.THREADS_FOUND): State.TRIAGING_FEEDBACK,
    (State.MERGE_READY, Event.THREADS_FOUND): State.TRIAGING_FEEDBACK,
    (State.TRIAGING_FEEDBACK, Event.ACTIONABLE_FEEDBACK): State.ADDRESSING_FEEDBACK,
    (State.TRIAGING_FEEDBACK, Event.NO_ACTIONABLE_FEEDBACK): State.PENDING_CI,
    (State.ADDRESSING_FEEDBACK, Event.FEEDBACK_PUSHED): State.PENDING_CI,
    (State.ADDRESSING_FEEDBACK, Event.FEEDBACK_EXHAUSTED): State.BLOCKED,
    (State.PENDING_CI, Event.PARENT_MERGED_OR_CONFLICT): State.REBASING_TO_MAIN,
    (State.AWAITING_REVIEW, Event.PARENT_MERGED_OR_CONFLICT): State.REBASING_TO_MAIN,
    (State.MERGE_READY, Event.PARENT_MERGED_OR_CONFLICT): State.REBASING_TO_MAIN,
    (State.REBASING_TO_MAIN, Event.REBASE_PUSHED): State.PENDING_CI,
    (State.REBASING_TO_MAIN, Event.CONFLICT): State.CONFLICT_RESOLVING,
    (State.CONFLICT_RESOLVING, Event.RESOLVED): State.REBASING_TO_MAIN,
    (State.CONFLICT_RESOLVING, Event.UNRESOLVED): State.BLOCKED,
    (State.PENDING, Event.ABORT): State.ABORTED,
    (State.PROVISIONING, Event.CRASH): State.FAILED,
    (State.PLANNING, Event.CRASH): State.FAILED,
    (State.DOING_SUBTASK, Event.CRASH): State.FAILED,
    (State.CHECKING_SUBTASK, Event.CRASH): State.FAILED,
    (State.TRIAGING_SUBTASK, Event.CRASH): State.FAILED,
    (State.COMMITTING, Event.CRASH): State.FAILED,
    (State.PUSHING, Event.CRASH): State.FAILED,
    (State.LOCAL_CI_CHECKING, Event.CRASH): State.FAILED,
    (State.PRE_PR_AUDITING, Event.CRASH): State.FAILED,
    (State.FIXUP_PLANNING, Event.CRASH): State.FAILED,
    (State.PR_OPENING, Event.CRASH): State.FAILED,
    (State.TRIAGING_FEEDBACK, Event.CRASH): State.FAILED,
    (State.ADDRESSING_FEEDBACK, Event.CRASH): State.FAILED,
    (State.REBASING_TO_MAIN, Event.CRASH): State.FAILED,
    (State.CONFLICT_RESOLVING, Event.CRASH): State.FAILED,
    (State.BLOCKED, Event.RETRY_TASK): State.PENDING,
    (State.FAILED, Event.RETRY_TASK): State.PENDING,
    (State.ABORTED, Event.RETRY_TASK): State.PENDING,
    (State.BLOCKED, Event.RESUME_TASK): State.PENDING,
    (State.FAILED, Event.RESUME_TASK): State.PENDING,
    (State.PENDING, Event.MARK_MERGED): State.MERGED,
    (State.PENDING_CI, Event.PR_CLOSED): State.ABORTED,
    (State.AWAITING_REVIEW, Event.PR_CLOSED): State.ABORTED,
    (State.MERGE_READY, Event.PR_CLOSED): State.ABORTED,
}

ACTIVE_STATES = frozenset(
    {
        State.PROVISIONING,
        State.PLANNING,
        State.DOING_SUBTASK,
        State.CHECKING_SUBTASK,
        State.TRIAGING_SUBTASK,
        State.COMMITTING,
        State.PUSHING,
        State.LOCAL_CI_CHECKING,
        State.PRE_PR_AUDITING,
        State.FIXUP_PLANNING,
        State.PR_OPENING,
        State.TRIAGING_FEEDBACK,
        State.ADDRESSING_FEEDBACK,
        State.REBASING_TO_MAIN,
        State.CONFLICT_RESOLVING,
    }
)
TRANSITIONS.update(
    {
        (state, Event.BLOCK_TASK): State.BLOCKED
        for state in ACTIVE_STATES
        if (state, Event.BLOCK_TASK) not in TRANSITIONS and state is not State.CONFLICT_RESOLVING
    }
)
TRANSITIONS.update(
    {
        (state, Event.PARENT_MERGED_OR_CONFLICT): State.REBASING_TO_MAIN
        for state in ACTIVE_STATES | {State.PENDING_CI, State.AWAITING_REVIEW, State.MERGE_READY}
        if state is not State.REBASING_TO_MAIN and (state, Event.PARENT_MERGED_OR_CONFLICT) not in TRANSITIONS
    }
)
TERMINAL_STATES = frozenset({State.MERGED, State.BLOCKED, State.FAILED, State.ABORTED})
POST_PR_STATES = frozenset({State.PENDING_CI, State.AWAITING_REVIEW, State.MERGE_READY})
RETRYABLE_STATES = frozenset({State.BLOCKED, State.FAILED, State.ABORTED})
STACK_READY_STATES = frozenset({State.PENDING_CI, State.AWAITING_REVIEW, State.MERGE_READY})


def _coerce_state(state: State | str) -> State:
    try:
        return state if isinstance(state, State) else State(state)
    except ValueError as exc:
        raise InvalidTransition(f"unknown task state: {state!r}") from exc


def _coerce_event(event: Event | str) -> Event:
    try:
        return event if isinstance(event, Event) else Event(event)
    except ValueError as exc:
        raise InvalidTransition(f"unknown task event: {event!r}") from exc


def target_for_event(current: State | str, event: Event | str) -> State:
    state = _coerce_state(current)
    ev = _coerce_event(event)
    try:
        return TRANSITIONS[(state, ev)]
    except KeyError as exc:
        raise InvalidTransition(f"event {ev.value!r} is not valid from state {state.value!r}") from exc


def assert_transition_allowed(current: State | str, event: Event | str) -> None:
    target_for_event(current, event)


def recover_after_crash(state: State | str, *, has_pr: bool) -> tuple[State, dict[str, object]]:
    """Return the state and field updates for crash/orphan recovery."""

    current = _coerce_state(state)
    if current in TERMINAL_STATES or current is State.PENDING:
        return (current, {})
    if current in POST_PR_STATES:
        return (State.PENDING_CI, {})
    if current is State.PROVISIONING:
        return (State.PENDING, {"branch": None, "worktree_path": None, "container_id": None})
    if current in {
        State.PR_OPENING,
        State.TRIAGING_FEEDBACK,
        State.ADDRESSING_FEEDBACK,
        State.REBASING_TO_MAIN,
        State.CONFLICT_RESOLVING,
    }:
        if has_pr:
            return (State.PENDING_CI, {})
        return (State.PENDING, {"resume_from_existing_subtasks": 1})
    return (State.PENDING, {"resume_from_existing_subtasks": 1})


def mermaid() -> str:
    lines = ["stateDiagram-v2", "  [*] --> pending"]
    for state in State:
        lines.append(f"  state {state.value}")
    for (source, event), target in TRANSITIONS.items():
        lines.append(f"  {source.value} --> {target.value}: {event.value}")
    return "\n".join(lines)
