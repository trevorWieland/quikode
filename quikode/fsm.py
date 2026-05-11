"""Canonical task lifecycle.

This module is the source of truth for the supported task states and
event-driven transitions.

Plan 28 cutover (2026-05-07): the post-PR slice is streamlined to two polling
phases. `MERGE_READY` and `TRIAGING_FEEDBACK` retire; the settle window
retires; the per-thread classifier retires. Only formal GitHub Reviews trigger
state changes — bot/AI-reviewer line comments become bundled CONTEXT for the
fixup planner, not polling triggers. See plans/28-streamlined-post-pr-fsm.md.

Plan 58 cutover (2026-05-10): the two umbrella states `PRE_PR_AUDITING` and
`ADDRESSING_FEEDBACK` are removed. The 5-stage audit gauntlet now exposes
each stage as a first-class FSM state (`AUDIT_LOCAL_CI`, `AUDIT_RUBRIC`,
`AUDIT_STANDARDS`, `AUDIT_ARCHITECTURE`, `AUDIT_BEHAVIOR`). The shared
inner fixup machinery (`FIXUP_PLANNING` → `DOING_SUBTASK` → `CHECKING_SUBTASK`
→ `TRIAGING_SUBTASK` → `COMMITTING` → `PUSHING`) now serves all fixup
triggers — INITIAL_AUDIT, CI_FAILURE, REVIEW_FEEDBACK — instead of being
masked by an umbrella state.
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
    # Plan 58: the five audit gauntlet stages are first-class states.
    AUDIT_LOCAL_CI = "audit_local_ci"
    AUDIT_RUBRIC = "audit_rubric"
    AUDIT_STANDARDS = "audit_standards"
    AUDIT_ARCHITECTURE = "audit_architecture"
    AUDIT_BEHAVIOR = "audit_behavior"
    FIXUP_PLANNING = "fixup_planning"
    PR_OPENING = "pr_opening"
    PENDING_CI = "pending_ci"
    AWAITING_REVIEW = "awaiting_review"
    REBASING_TO_MAIN = "rebasing_to_main"
    CONFLICT_RESOLVING = "conflict_resolving"
    MERGED = "merged"
    # Plan 32: merge-nodes are first-class synthetic tasks with `kind="merge"`.
    MERGE_NODE_READY = "merge_node_ready"
    MERGE_NODE_RETIRED = "merge_node_retired"
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
    # Plan 58: per-stage audit advance events. The gauntlet walks
    # local_ci → rubric → standards → architecture → behavior; any
    # stage may fail and divert to FIXUP_PLANNING, then re-enter the
    # gauntlet from the top on the next cycle.
    AUDIT_LOCAL_CI_PASSED = "audit_local_ci_passed"
    AUDIT_LOCAL_CI_FAILED = "audit_local_ci_failed"
    AUDIT_RUBRIC_PASSED = "audit_rubric_passed"
    AUDIT_RUBRIC_FAILED = "audit_rubric_failed"
    AUDIT_STANDARDS_PASSED = "audit_standards_passed"
    AUDIT_STANDARDS_FAILED = "audit_standards_failed"
    AUDIT_ARCHITECTURE_PASSED = "audit_architecture_passed"
    AUDIT_ARCHITECTURE_FAILED = "audit_architecture_failed"
    AUDIT_BEHAVIOR_PASSED = "audit_behavior_passed"
    AUDIT_BEHAVIOR_FAILED = "audit_behavior_failed"
    # Plan 58: trigger-source-aware entry into the audit cycle. All three
    # land at AUDIT_LOCAL_CI; the trigger source flows through to the
    # outer wrapping (push + open PR vs. push + return to PENDING_CI).
    INITIAL_AUDIT_START = "initial_audit_start"
    CI_FIXUP_START = "ci_fixup_start"
    REVIEW_FIXUP_START = "review_fixup_start"
    # Pre-plan-58 AUDIT_PASSED / AUDIT_FAILED retired in favor of the
    # per-stage events above. The driver fires AUDIT_BEHAVIOR_PASSED on a
    # clean cycle (→ PR_OPENING or → PENDING_CI depending on trigger).
    FIXUP_PLAN_VALID = "fixup_plan_valid"
    FIXUP_EXHAUSTED = "fixup_exhausted"
    PR_OPENED = "pr_opened"
    CI_PASSED = "ci_passed"
    CI_FAILED = "ci_failed"
    CHANGES_REQUESTED_RECEIVED = "changes_requested_received"
    MERGED = "merged"
    # Plan 32: merge-node lifecycle events.
    MERGE_NODE_BUILT = "merge_node_built"
    PARENT_ADVANCED = "parent_advanced"
    ALL_PARENTS_MERGED = "all_parents_merged"
    # Plan 58: FEEDBACK_PUSHED retired (used to come out of ADDRESSING_FEEDBACK).
    # Post-PR fixup loops now exit the audit gauntlet at AUDIT_BEHAVIOR_PASSED
    # which routes via PUSHING → PENDING_CI by way of the driver.
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


# Plan 58: per-stage advance map. Each stage has a PASSED event that
# advances to the next stage and a FAILED event that diverts to
# FIXUP_PLANNING (which then re-enters the gauntlet on the next cycle).
_AUDIT_STAGES: tuple[tuple[State, Event, Event, State | None], ...] = (
    (State.AUDIT_LOCAL_CI, Event.AUDIT_LOCAL_CI_PASSED, Event.AUDIT_LOCAL_CI_FAILED, State.AUDIT_RUBRIC),
    (State.AUDIT_RUBRIC, Event.AUDIT_RUBRIC_PASSED, Event.AUDIT_RUBRIC_FAILED, State.AUDIT_STANDARDS),
    (
        State.AUDIT_STANDARDS,
        Event.AUDIT_STANDARDS_PASSED,
        Event.AUDIT_STANDARDS_FAILED,
        State.AUDIT_ARCHITECTURE,
    ),
    (
        State.AUDIT_ARCHITECTURE,
        Event.AUDIT_ARCHITECTURE_PASSED,
        Event.AUDIT_ARCHITECTURE_FAILED,
        State.AUDIT_BEHAVIOR,
    ),
    (State.AUDIT_BEHAVIOR, Event.AUDIT_BEHAVIOR_PASSED, Event.AUDIT_BEHAVIOR_FAILED, None),
)


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
    # Plan 58: LOCAL_CI_CHECKING → AUDIT_LOCAL_CI starts the gauntlet.
    (State.LOCAL_CI_CHECKING, Event.LOCAL_CI_PASSED): State.AUDIT_LOCAL_CI,
    (State.LOCAL_CI_CHECKING, Event.LOCAL_CI_FAILED): State.FIXUP_PLANNING,
    # Plan 58: per-stage transitions.
    (State.AUDIT_LOCAL_CI, Event.AUDIT_LOCAL_CI_PASSED): State.AUDIT_RUBRIC,
    (State.AUDIT_LOCAL_CI, Event.AUDIT_LOCAL_CI_FAILED): State.FIXUP_PLANNING,
    (State.AUDIT_RUBRIC, Event.AUDIT_RUBRIC_PASSED): State.AUDIT_STANDARDS,
    (State.AUDIT_RUBRIC, Event.AUDIT_RUBRIC_FAILED): State.FIXUP_PLANNING,
    (State.AUDIT_STANDARDS, Event.AUDIT_STANDARDS_PASSED): State.AUDIT_ARCHITECTURE,
    (State.AUDIT_STANDARDS, Event.AUDIT_STANDARDS_FAILED): State.FIXUP_PLANNING,
    (State.AUDIT_ARCHITECTURE, Event.AUDIT_ARCHITECTURE_PASSED): State.AUDIT_BEHAVIOR,
    (State.AUDIT_ARCHITECTURE, Event.AUDIT_ARCHITECTURE_FAILED): State.FIXUP_PLANNING,
    # Spec-task clean cycle: AUDIT_BEHAVIOR_PASSED → PR_OPENING.
    # Post-PR fixup flows (CI_FIXUP / REVIEW_FIXUP) exit via PUSHING → PENDING_CI;
    # the driver explicitly drives those transitions, no FSM-level branching here.
    (State.AUDIT_BEHAVIOR, Event.AUDIT_BEHAVIOR_PASSED): State.PR_OPENING,
    (State.AUDIT_BEHAVIOR, Event.AUDIT_BEHAVIOR_FAILED): State.FIXUP_PLANNING,
    # Plan 58: trigger-source-aware entry from PENDING_CI / AWAITING_REVIEW.
    # CI_FIXUP_START fires on CI failure; REVIEW_FIXUP_START fires on
    # CHANGES_REQUESTED. INITIAL_AUDIT_START is the rename of the old
    # LOCAL_CI_PASSED → PRE_PR_AUDITING hop; the driver fires it explicitly.
    (State.PENDING_CI, Event.CI_FIXUP_START): State.AUDIT_LOCAL_CI,
    (State.AWAITING_REVIEW, Event.CI_FIXUP_START): State.AUDIT_LOCAL_CI,
    (State.AWAITING_REVIEW, Event.REVIEW_FIXUP_START): State.AUDIT_LOCAL_CI,
    (State.FIXUP_PLANNING, Event.FIXUP_PLAN_VALID): State.DOING_SUBTASK,
    (State.FIXUP_PLANNING, Event.FIXUP_EXHAUSTED): State.BLOCKED,
    (State.PR_OPENING, Event.PR_OPENED): State.PENDING_CI,
    # Plan 28/58 post-PR slice: pre-plan-58 CI_FAILED / CHANGES_REQUESTED transitions
    # retired — the driver now uses the explicit CI_FIXUP_START / REVIEW_FIXUP_START
    # events above which carry trigger-source context.
    (State.PENDING_CI, Event.CI_PASSED): State.AWAITING_REVIEW,
    (State.AWAITING_REVIEW, Event.MERGED): State.MERGED,
    # Plan 32: merge-node terminal/refresh transitions. AUDIT_BEHAVIOR_PASSED for
    # `kind="merge"` rows fires MERGE_NODE_BUILT instead of progressing to
    # PR_OPENING (worker-layer dispatch on kind).
    (State.AUDIT_BEHAVIOR, Event.MERGE_NODE_BUILT): State.MERGE_NODE_READY,
    (State.MERGE_NODE_READY, Event.PARENT_ADVANCED): State.PENDING,
    (State.MERGE_NODE_READY, Event.ALL_PARENTS_MERGED): State.MERGE_NODE_RETIRED,
    (State.PENDING_CI, Event.PARENT_MERGED_OR_CONFLICT): State.REBASING_TO_MAIN,
    (State.AWAITING_REVIEW, Event.PARENT_MERGED_OR_CONFLICT): State.REBASING_TO_MAIN,
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
    (State.AUDIT_LOCAL_CI, Event.CRASH): State.FAILED,
    (State.AUDIT_RUBRIC, Event.CRASH): State.FAILED,
    (State.AUDIT_STANDARDS, Event.CRASH): State.FAILED,
    (State.AUDIT_ARCHITECTURE, Event.CRASH): State.FAILED,
    (State.AUDIT_BEHAVIOR, Event.CRASH): State.FAILED,
    (State.FIXUP_PLANNING, Event.CRASH): State.FAILED,
    (State.PR_OPENING, Event.CRASH): State.FAILED,
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
}

# Plan 58: the five audit-stage states join the active set.
_AUDIT_STAGE_STATES = frozenset(
    {
        State.AUDIT_LOCAL_CI,
        State.AUDIT_RUBRIC,
        State.AUDIT_STANDARDS,
        State.AUDIT_ARCHITECTURE,
        State.AUDIT_BEHAVIOR,
    }
)
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
        State.FIXUP_PLANNING,
        State.PR_OPENING,
        State.REBASING_TO_MAIN,
        State.CONFLICT_RESOLVING,
    }
    | _AUDIT_STAGE_STATES
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
        for state in ACTIVE_STATES | {State.PENDING_CI, State.AWAITING_REVIEW}
        if state is not State.REBASING_TO_MAIN and (state, Event.PARENT_MERGED_OR_CONFLICT) not in TRANSITIONS
    }
)
TERMINAL_STATES = frozenset(
    {State.MERGED, State.MERGE_NODE_RETIRED, State.BLOCKED, State.FAILED, State.ABORTED}
)
POST_PR_STATES = frozenset({State.PENDING_CI, State.AWAITING_REVIEW})
RETRYABLE_STATES = frozenset({State.BLOCKED, State.FAILED, State.ABORTED})
# Plan 32: MERGE_NODE_READY joins STACK_READY_STATES — children depending on a
# merge-node fork off it once it's built + audited.
STACK_READY_STATES = frozenset({State.PENDING_CI, State.AWAITING_REVIEW, State.MERGE_NODE_READY})


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
    """Return the state and field updates for crash/orphan recovery.

    Plan 58: ADDRESSING_FEEDBACK / PRE_PR_AUDITING are retired. Audit-stage
    states (AUDIT_LOCAL_CI..AUDIT_BEHAVIOR) recover the same way the old
    PRE_PR_AUDITING did: PR-aware → PENDING_CI; otherwise → PENDING with
    `resume_from_existing_subtasks=1`.
    """

    current = _coerce_state(state)
    if current in TERMINAL_STATES or current is State.PENDING:
        return (current, {})
    if current in POST_PR_STATES:
        return (State.PENDING_CI, {})
    if current is State.PROVISIONING:
        return (State.PENDING, {"branch": None, "worktree_path": None, "container_id": None})
    if (
        current
        in {
            State.PR_OPENING,
            State.REBASING_TO_MAIN,
            State.CONFLICT_RESOLVING,
        }
        | _AUDIT_STAGE_STATES
    ):
        if has_pr:
            return (State.PENDING_CI, {})
        return (State.PENDING, {"resume_from_existing_subtasks": 1})
    return (State.PENDING, {"resume_from_existing_subtasks": 1})


def audit_stage_transitions() -> tuple[tuple[State, Event, Event, State | None], ...]:
    """Plan 58 helper: ordered audit-stage advance/fail metadata for callers
    that walk the gauntlet (driver, tests, docs)."""
    return _AUDIT_STAGES


def mermaid() -> str:
    lines = ["stateDiagram-v2", "  [*] --> pending"]
    for state in State:
        lines.append(f"  state {state.value}")
    for (source, event), target in TRANSITIONS.items():
        lines.append(f"  {source.value} --> {target.value}: {event.value}")
    return "\n".join(lines)
