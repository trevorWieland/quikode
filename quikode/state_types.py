"""Shared state row types and enums."""

from __future__ import annotations

from enum import StrEnum
from typing import NotRequired, TypedDict

from quikode.fsm import State

# ---------- Row type stubs (TypedDict — runtime is just a dict, but the
# typechecker now knows what keys exist and what they hold). Keeps the
# dict-like access pattern while closing the `dict | None` type hole. ----------


class TaskRow(TypedDict):
    """Shape of a row from the `tasks` table."""

    id: str
    state: str
    branch: NotRequired[str | None]
    worktree_path: NotRequired[str | None]
    container_id: NotRequired[str | None]
    pr_url: NotRequired[str | None]
    pr_number: NotRequired[int | None]
    plan_text: NotRequired[str]
    last_error: NotRequired[str | None]
    failure_reason: NotRequired[str | None]
    ci_triage_retries: NotRequired[int | None]
    last_pr_event_ts: NotRequired[str | None]
    base_ref_sha: NotRequired[str | None]
    last_synced_main_sha: NotRequired[str | None]
    conflict_resolve_retries: NotRequired[int | None]
    needs_intent_review: NotRequired[int | None]
    last_intent_review_ts: NotRequired[float | None]
    intent_review_count: NotRequired[int | None]
    replan_count: NotRequired[int | None]
    # Multi-parent stacking JSON arrays — see `Store.get_parent_task_ids` /
    # `Store.get_parent_branches`. Scalar parent_task_id / parent_branch /
    # parent_pr_branch columns were dropped in an older schema cleanup.
    resume_from_existing_subtasks: NotRequired[int | None]
    # v3 Phase A/B/C: review-loop + intervention + stacked-diffs metadata
    review_round: NotRequired[int | None]
    intervention_request: NotRequired[str | None]  # JSON {kind, message, posted_pr_comment_id, ts}
    draft_pr_number: NotRequired[int | None]  # separate from final pr_number
    last_review_poll_ts: NotRequired[float | None]
    # v3 Phase C: when a child enters REBASING_TO_MAIN we stash the active
    # state we came from here so the rebase worker can restore it on success.
    pre_rebase_state: NotRequired[str | None]
    # v3 stacked-diffs fix: orchestrator sets this when a parent merges
    # while a child worker is mid-flight. Worker checks it at safe
    # checkpoints and runs an inline rebase + PR retarget before continuing.
    needs_parent_rebase: NotRequired[int | None]
    # v3 polish: set to 1 when the daemon auto-merged this task's PR via
    # `cfg.auto_merge_when_clean`. Audit-only — does not change state-machine
    # behavior.
    auto_merged: NotRequired[int | None]
    # Plan 28: tracks the most recent GitHub Review id we've already routed to
    # ADDRESSING_FEEDBACK so we don't re-trigger on the same CHANGES_REQUESTED
    # after a daemon restart. NULL until the first non-bot review arrives.
    last_processed_review_id: NotRequired[str | None]
    # Plan 32: row kind. 'spec' for regular DAG-seeded tasks, 'merge' for
    # synthetic merge-nodes integrating multiple parents.
    kind: NotRequired[str]
    # rebase coalescing: timestamp of the most recent rebase trigger for
    # this task, used by `_schedule_rebase_to_main` to dedupe rapid-fire
    # triggers within `cfg.rebase_coalesce_window_s`.
    last_rebase_scheduled_ts: NotRequired[float | None]
    parent_task_ids: NotRequired[str]
    parent_branches: NotRequired[str]
    parent_pr_branches: NotRequired[str]
    parent_merge_base_sha: NotRequired[str | None]
    parent_merge_base_branch: NotRequired[str | None]
    last_observed_branch_tip_sha: NotRequired[str | None]
    block_forensics: NotRequired[str | None]
    pre_pr_audit_summary: NotRequired[str | None]
    seed_source: NotRequired[str | None]
    seed_evidence: NotRequired[str | None]
    seeded_at: NotRequired[float | None]
    created_at: float
    updated_at: float


class SubtaskRow(TypedDict):
    """Shape of a row from the `subtasks` table."""

    id: int
    task_id: str
    subtask_id: str
    state: str
    title: NotRequired[str | None]
    depends_on: NotRequired[str]  # JSON-encoded list
    files_to_touch: NotRequired[str]  # JSON-encoded list
    boundary: NotRequired[str | None]
    acceptance: NotRequired[str | None]  # JSON-encoded list
    notes: NotRequired[str | None]
    retries: NotRequired[int | None]
    last_error: NotRequired[str | None]
    triage_notes: NotRequired[str | None]
    # v3 Phase A: per-subtask commits + transient/progress retries
    commit_sha: NotRequired[str | None]
    transient_retries: NotRequired[int | None]
    progress_check_count: NotRequired[int | None]
    flatline_count: NotRequired[int | None]
    last_failure_root_cause_hash: NotRequired[str | None]
    pre_commit_failures: NotRequired[int | None]
    # v3 fixup decomposition: 'spec' / 'fixup-final' / 'fixup-ci' / 'fixup-review'.
    kind: NotRequired[str | None]
    # v3.5 retry-cause classification: JSON array of retry reasons.
    retry_reasons: NotRequired[str | None]
    # v3.7 advisory scope review: comma-separated effective lane after the
    # scope-reviewer accepted drift from declared `files_to_touch`.
    accepted_files: NotRequired[str | None]
    created_at: NotRequired[float | None]
    updated_at: NotRequired[float | None]


class ReviewThreadRow(TypedDict):
    """Shape of a row from the v3 `review_threads` table."""

    task_id: str
    thread_id: str
    is_resolved: int
    last_comment_ts: float
    last_comment_author: NotRequired[str | None]
    last_comment_is_bot: NotRequired[int | None]
    addressed_in_commit_sha: NotRequired[str | None]
    first_seen_ts: float


class ProgressCheckRow(TypedDict):
    """Shape of a row from the v3 `progress_checks` audit table."""

    task_id: str
    subtask_id: str
    ts: float
    attempts_at_check: int
    verdict: str  # progressing | flatlined | uncertain
    rationale: NotRequired[str | None]


class ContainerStatsRow(TypedDict):
    id: int
    task_id: str
    container_name: NotRequired[str | None]
    cpu_pct: NotRequired[float | None]
    mem_bytes: NotRequired[int | None]
    mem_pct: NotRequired[float | None]
    ts: float


# States with no active worker (briefing's "in flight" excludes these).
TERMINAL = {
    State.MERGED,
    State.MERGE_NODE_READY,
    State.MERGE_NODE_RETIRED,
    State.PENDING_CI,
    State.AWAITING_REVIEW,
    State.BLOCKED,
    State.FAILED,
    State.ABORTED,
}
ACTIVE = {
    State.PROVISIONING,
    State.PLANNING,
    State.DOING_SUBTASK,
    State.CHECKING_SUBTASK,
    State.TRIAGING_SUBTASK,
    State.COMMITTING,
    State.PUSHING,
    State.PR_OPENING,
    State.PENDING_CI,
    State.REBASING_TO_MAIN,
    State.CONFLICT_RESOLVING,
    State.FIXUP_PLANNING,
    State.ADDRESSING_FEEDBACK,
    State.LOCAL_CI_CHECKING,
    State.PRE_PR_AUDITING,
}

# Convenience set: any state where the PR is open and waiting on something
# outside the worker (CI run, human/bot review). PENDING_CI is the most common
# — the worker just opened the PR and CI is running. Plan 28 retired
# MERGE_READY (the settle window died with the per-thread classifier).
POST_PR_STATES = {State.PENDING_CI, State.AWAITING_REVIEW}


class SubtaskState(StrEnum):
    PENDING = "pending"
    DOING = "doing"
    CHECKING = "checking"
    TRIAGING = "triaging"
    DONE = "done"
    BLOCKED = "blocked"  # per-subtask retry budget exhausted; whole-spec final check still gets a shot
    SKIPPED = "skipped"  # old cascade marker; workers repair it to pending
