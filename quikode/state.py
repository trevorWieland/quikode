"""SQLite state store + FSM definitions.

Single connection, WAL mode. We DO NOT implement resume logic for in-flight
containers — on restart the orchestrator tears down anything not in a terminal
state and re-runs the task from scratch.

Thread safety: SQLite supports concurrent readers in WAL mode, but on a
single shared connection (check_same_thread=False) the sqlite3 module
serializes statement execution per-connection. `BEGIN IMMEDIATE` from two
threads collides with 'cannot start a transaction within a transaction',
and a thread starting an `execute` while another is mid-`fetch` on the
same connection raises 'InterfaceError: bad parameter or other API
misuse'. To avoid both, every connection access — reads AND writes — runs
under `self._tx_lock` (an RLock so `tx()` can re-enter from helper
methods). Cursor fetches must happen inside the lock too, since cursors
are connection-bound.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any, NotRequired, TypedDict

log = logging.getLogger("quikode.state")

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
    plan_text: NotRequired[str | None]
    last_error: NotRequired[str | None]
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
    # parent_pr_branch were dropped in the legacy purge.
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
    last_notified_settled_ts: NotRequired[float | None]
    # rebase coalescing: timestamp of the most recent rebase trigger for
    # this task, used by `_schedule_rebase_to_main` to dedupe rapid-fire
    # triggers within `cfg.rebase_coalesce_window_s`.
    last_rebase_scheduled_ts: NotRequired[float | None]
    created_at: float
    updated_at: float


class SubtaskRow(TypedDict):
    """Shape of a row from the `subtasks` table."""

    id: int
    task_id: str
    subtask_id: str
    state: str
    title: NotRequired[str | None]
    depends_on: NotRequired[str | None]  # JSON-encoded list
    files_to_touch: NotRequired[str | None]  # JSON-encoded list
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


class State(StrEnum):
    PENDING = "pending"
    PROVISIONING = "provisioning"  # worktree + container coming up
    PLANNING = "planning"
    # Per-subtask flow (the only flow — the monolithic whole-spec doer/checker
    # was removed in the legacy purge).
    DOING_SUBTASK = "doing_subtask"
    CHECKING_SUBTASK = "checking_subtask"
    TRIAGING_SUBTASK = "triaging_subtask"
    # Post-implementation
    COMMITTING = "committing"
    PUSHING = "pushing"
    PR_OPENING = "pr_opening"
    POLLING_CI = "polling_ci"
    # Parallel-safe merge handling
    REBASING = "rebasing"  # rebasing onto current main; clean→back to polling
    CONFLICT_RESOLVING = "conflict_resolving"  # spawned resolver agent on a conflicted rebase
    # Intent-gap detection
    INTENT_REVIEWING = "intent_reviewing"  # checking if main has shifted under us in a way that breaks intent
    REPLANNING = "replanning"  # producing a new plan in light of intent conflict
    # Fixup decomposition: invoking the fixup planner on a CI / audit-gauntlet
    # / review failure to break the fixup into per-subtask slices.
    FIXUP_PLANNING = "fixup_planning"
    # ----- pre-PR pipeline (4-stage gate before _open_pr) -----
    # Per-subtask commits landed is no longer enough to open a PR — we run
    # the full local-CI suite and three audit agents first. Any failure
    # routes back through the fixup-planner with a different `kind` so the
    # subtask loop addresses the findings before we re-run the gate.
    LOCAL_CI_CHECKING = "local_ci_checking"  # `just ci` (or cfg-configurable) inside the dev container
    PRE_PR_AUDITING = "pre_pr_auditing"  # rubric + standards + behavior audits
    PRE_PR_TRIAGING = "pre_pr_triaging"  # merging audit findings → triage → fixup plan
    # ----- post-PR-open states (v3.5: split from the legacy AWAITING_MERGE) -----
    # The legacy AWAITING_MERGE was overloaded with three distinct conditions:
    #   1. PR submitted, CI still running → PENDING_CI
    #   2. CI green, no positive review approval (or recent activity) → AWAITING_REVIEW
    #   3. CI green, no unresolved threads, settled past quiet window → MERGE_READY
    # Splitting these surfaces the *real* readiness signal to the picker, the
    # operator, and the auto-merge gate, instead of bundling them under one
    # opaque "awaiting" state. The daemon's _poll_pr_loop drives the
    # transitions based on PR/CI/review signals.
    PENDING_CI = "pending_ci"
    AWAITING_REVIEW = "awaiting_review"
    MERGE_READY = "merge_ready"
    # ----- post-PR-open response states (v3.5: split from ADDRESSING_FEEDBACK) -----
    # Legacy ADDRESSING_FEEDBACK collapsed provisioning + Python triage +
    # planner + per-subtask doer/checker into one opaque ~14min state; an
    # operator could not tell which sub-step was running. Splitting:
    #   1. TRIAGING_FEEDBACK — Python-deterministic triage (CI log parse,
    #      review thread classifier). Fast (<60s typical). Decides which
    #      threads need a fix vs which can be auto-replied + resolved.
    #   2. ADDRESSING_FEEDBACK — fixup planner + per-subtask machinery.
    #      Carries the existing FIXUP_PLANNING / DOING_SUBTASK / CHECKING_SUBTASK
    #      sub-states for visibility.
    TRIAGING_FEEDBACK = "triaging_feedback"
    ADDRESSING_FEEDBACK = "addressing_feedback"
    # v3 Phase C: stacked diffs auto-rebase. When a parent task merges, any
    # child still carrying parent_pr_branch is rebased onto main and its PR
    # is retargeted. The transition is transient — children resume to their
    # pre-rebase state on success. The pre-rebase state is captured in
    # `tasks.pre_rebase_state` so the resumption is bookkeeping-driven, not
    # behavior-driven.
    REBASING_TO_MAIN = "rebasing_to_main"
    MERGED = "merged"
    BLOCKED = "blocked"  # exhausted retry budget; needs human triage
    FAILED = "failed"  # unrecoverable
    ABORTED = "aborted"


# States with no active worker (briefing's "in flight" excludes these).
TERMINAL = {
    State.MERGED,
    State.PENDING_CI,
    State.AWAITING_REVIEW,
    State.MERGE_READY,
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
    State.POLLING_CI,
    State.REBASING,
    State.CONFLICT_RESOLVING,
    State.INTENT_REVIEWING,
    State.REPLANNING,
    State.FIXUP_PLANNING,
    State.TRIAGING_FEEDBACK,
    State.ADDRESSING_FEEDBACK,
    State.LOCAL_CI_CHECKING,
    State.PRE_PR_AUDITING,
    State.PRE_PR_TRIAGING,
    State.REBASING_TO_MAIN,
}

# Convenience set: any state where the PR is open and waiting on something
# outside the worker (CI run, human/bot review, settle window). PENDING_CI
# is the most common — the worker just opened the PR and CI is running.
POST_PR_STATES = {State.PENDING_CI, State.AWAITING_REVIEW, State.MERGE_READY}


class SubtaskState(StrEnum):
    PENDING = "pending"
    DOING = "doing"
    CHECKING = "checking"
    TRIAGING = "triaging"
    DONE = "done"
    BLOCKED = "blocked"  # per-subtask retry budget exhausted; whole-spec final check still gets a shot
    SKIPPED = "skipped"  # explicitly skipped (e.g. dep blocked)


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    branch TEXT,
    worktree_path TEXT,
    container_id TEXT,
    pr_url TEXT,
    pr_number INTEGER,
    plan_text TEXT,
    last_error TEXT,
    ci_triage_retries INTEGER DEFAULT 0,
    last_pr_event_ts TEXT,
    -- v2 Phase A: track main HEAD at branch time + last successful rebase, so
    -- we can compute "what landed since" for conflict-resolver context.
    base_ref_sha TEXT,
    last_synced_main_sha TEXT,
    conflict_resolve_retries INTEGER DEFAULT 0,
    -- v2 Phase B: intent-gap detection. Orchestrator sets `needs_intent_review`
    -- when another task merges; worker checks at safe checkpoints.
    needs_intent_review INTEGER DEFAULT 0,
    last_intent_review_ts REAL,
    intent_review_count INTEGER DEFAULT 0,
    replan_count INTEGER DEFAULT 0,
    -- Resume marker: when 1, the worker skips the planner agent on next
    -- provision and reconstructs the Plan from the existing subtasks rows.
    -- Set by `quikode resume <id>`; cleared by the worker on consume.
    resume_from_existing_subtasks INTEGER DEFAULT 0,
    -- review_round counts how many human-driven review→respond cycles this
    -- task has gone through. intervention_request is a JSON blob carrying
    -- kind/message/comment-id/ts when the daemon needs human attention.
    -- draft_pr_number is the early draft PR (opened after S-01) distinct
    -- from pr_number.
    review_round INTEGER DEFAULT 0,
    intervention_request TEXT,
    draft_pr_number INTEGER,
    last_review_poll_ts REAL,
    -- v3 Phase C: stacked-diff auto-rebase. Stores the active state of the
    -- child task when it entered REBASING_TO_MAIN so the rebase worker can
    -- restore it on a successful rebase + retarget.
    pre_rebase_state TEXT,
    -- v3 stacked-diffs fix: orchestrator sets this when a parent merges
    -- while a child worker is mid-flight; worker checks at safe checkpoints
    -- and runs an inline rebase + PR retarget before continuing.
    needs_parent_rebase INTEGER DEFAULT 0,
    -- v3 polish: 1 when the daemon auto-merged this task's PR via
    -- `cfg.auto_merge_when_clean`. Audit-only — does not change FSM behavior.
    auto_merged INTEGER DEFAULT 0,
    -- rebase coalescing: timestamp of the most recent rebase trigger for
    -- this task. Used by `_schedule_rebase_to_main` to skip extra triggers
    -- within `cfg.rebase_coalesce_window_s`.
    last_rebase_scheduled_ts REAL,
    -- v3 settled-notification: when the daemon's review-watcher detects the
    -- task has been quiet (AWAITING_MERGE + green + no churn for
    -- cfg.notify_settled_after_s), it pings the configured channel and
    -- stamps this column. Re-pings are gated on the task having LEFT
    -- AWAITING_MERGE since the last notify (e.g. responded to a thread)
    -- so we don't spam on every poll tick.
    last_notified_settled_ts REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS state_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    note TEXT,
    ts REAL NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,             -- planner_output | doer_output | checker_output | triage_output | ci_log | review_comments
    content TEXT,                   -- inline text or path
    is_path INTEGER DEFAULT 0,
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    phase TEXT NOT NULL,             -- planner | doer | checker | triage | subtask_doer | subtask_checker | subtask_triage | final_checker
    cli TEXT NOT NULL,               -- claude | codex | opencode
    model TEXT,
    rc INTEGER,
    duration_s REAL,
    tokens_used INTEGER,             -- total tokens (input + output) — kept as a quick rollup
    -- v2.1 token detail (NULL when the provider doesn't report)
    tokens_input INTEGER,
    tokens_output INTEGER,
    tokens_cached_read INTEGER,
    tokens_cached_creation INTEGER,
    cost_usd REAL,
    subtask_id TEXT,                 -- v2: scope to a specific subtask if applicable
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_calls_task ON agent_calls(task_id, ts);

-- v2 Phase 0: subtasks emitted by the planner. The orchestrator drives a
-- per-subtask doer/checker loop in topological order before running the
-- whole-spec final checker.
CREATE TABLE IF NOT EXISTS subtasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    subtask_id TEXT NOT NULL,        -- e.g. "S-01-domain"
    title TEXT,
    depends_on TEXT,                 -- JSON array of subtask_ids
    files_to_touch TEXT,             -- JSON array
    boundary TEXT,
    acceptance TEXT,                 -- JSON array of acceptance bullet strings
    notes TEXT,
    -- v3 fixup decomposition: 'spec' for original planner output,
    -- 'fixup-final' for slices added when final-check fails, 'fixup-ci'
    -- for slices added when GitHub CI fails post-PR-open. Kind drives
    -- which prompt template the worker uses + how the operator reads
    -- progress in `quikode show`.
    kind TEXT NOT NULL DEFAULT 'spec',
    state TEXT NOT NULL,             -- SubtaskState value
    retries INTEGER DEFAULT 0,
    last_error TEXT,
    triage_notes TEXT,               -- latest triage output for this subtask
    -- v3 Phase A: per-subtask commits + transient/progress retries.
    -- commit_sha is set after a successful PASS+commit. transient_retries
    -- counts container/network/timeout failures separately from real verdict
    -- FAILs (which still bump retries). progress_check_count is how many
    -- times the progress-check agent ran; flatline_count is how many of
    -- those came back FLATLINED in a row. last_failure_root_cause_hash lets
    -- us detect "same failure repeating" without storing full triage notes.
    -- pre_commit_failures counts hook-gate rejections distinctly.
    commit_sha TEXT,
    transient_retries INTEGER DEFAULT 0,
    progress_check_count INTEGER DEFAULT 0,
    flatline_count INTEGER DEFAULT 0,
    last_failure_root_cause_hash TEXT,
    pre_commit_failures INTEGER DEFAULT 0,
    -- v3.5 retry-cause classification: JSON array of
    -- {attempt:int, ts:float, category:str, signature:str, transient:bool}.
    -- Each retry (real OR transient) appends an entry so `quikode show`
    -- can render a "why did this retry?" histogram per subtask.
    retry_reasons TEXT,
    -- v3.7 advisory scope review: comma-separated effective lane after
    -- the scope-reviewer accepted drift from `files_to_touch`. NULL when
    -- the actual diff matched the planner's declared lane exactly.
    accepted_files TEXT,
    created_at REAL,
    updated_at REAL,
    UNIQUE(task_id, subtask_id)
);
CREATE INDEX IF NOT EXISTS idx_subtasks_task ON subtasks(task_id);

-- v2 Resources: periodic samples of running container resource usage so we can
-- track high-water marks and tune mem_per_task_gb to actuals.
CREATE TABLE IF NOT EXISTS container_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    container_name TEXT,
    cpu_pct REAL,
    mem_bytes INTEGER,
    mem_pct REAL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_container_stats_task ON container_stats(task_id, ts);

-- v2 Phase B: intent-gap reviews. One row per review run.
CREATE TABLE IF NOT EXISTS intent_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    triggered_by_merge_of TEXT,    -- which task ID's merge triggered this (if any)
    main_sha_before TEXT,
    main_sha_after TEXT,
    verdict TEXT NOT NULL,         -- NO_DRIFT | MINOR_DRIFT | INTENT_CONFLICT
    explanation TEXT,
    affected_areas TEXT,
    raw_output TEXT,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intent_reviews_task ON intent_reviews(task_id, ts);

-- v3 Phase B: review threads tracked by GraphQL node id. The daemon's
-- review-watcher pass diffs the live thread list against this table to
-- discover new unresolved human comments worth responding to.
CREATE TABLE IF NOT EXISTS review_threads (
    task_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    is_resolved INTEGER NOT NULL,
    last_comment_ts REAL NOT NULL,
    last_comment_author TEXT,
    last_comment_is_bot INTEGER DEFAULT 0,
    addressed_in_commit_sha TEXT,
    first_seen_ts REAL NOT NULL,
    PRIMARY KEY (task_id, thread_id)
);
CREATE INDEX IF NOT EXISTS idx_review_threads_task ON review_threads(task_id);

-- v3 Phase A: progress-check audit. One row per progress-check invocation,
-- so we can later see why a subtask was blocked (or not) on flatline grounds.
CREATE TABLE IF NOT EXISTS progress_checks (
    task_id TEXT NOT NULL,
    subtask_id TEXT NOT NULL,
    ts REAL NOT NULL,
    attempts_at_check INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    rationale TEXT,
    PRIMARY KEY (task_id, subtask_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_progress_checks_subtask ON progress_checks(task_id, subtask_id, ts);

CREATE INDEX IF NOT EXISTS idx_state_log_task ON state_log(task_id, ts);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id, kind, ts);
"""


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        self.conn = sqlite3.connect(db_path, isolation_level=None, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # Serialize ALL connection access across worker threads. Without this,
        # parallel workers calling tx() collide on BEGIN IMMEDIATE, and reads
        # racing against an in-flight execute on the same connection raise
        # `InterfaceError: bad parameter or other API misuse`. Created before
        # the first execute so even setup runs under the lock.
        self._tx_lock = threading.RLock()
        with self._tx_lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns and tables that older DBs predate.

        `executescript(SCHEMA)` runs `CREATE TABLE IF NOT EXISTS`, which is a
        no-op when the table already exists — so columns added in later
        versions never land in older workspaces. This walks each table and
        ALTERs in any columns missing from the current shape, and CREATEs
        any whole-new tables that older DBs lack.

        Idempotent: running on an already-current DB is a fast no-op.
        """
        expected: dict[str, list[tuple[str, str]]] = {
            "tasks": [
                # v2 Phase A: parallel-safe merge handling
                ("base_ref_sha", "TEXT"),
                ("last_synced_main_sha", "TEXT"),
                ("conflict_resolve_retries", "INTEGER DEFAULT 0"),
                # v2 Phase B: intent-gap detection
                ("needs_intent_review", "INTEGER DEFAULT 0"),
                ("last_intent_review_ts", "REAL"),
                ("intent_review_count", "INTEGER DEFAULT 0"),
                ("replan_count", "INTEGER DEFAULT 0"),
                # Multi-parent stacking. JSON arrays of dependency task ids
                # and their branches. When a child has > 1 stack-ready parent
                # the worker constructs a synthetic merge-base branch
                # (`quikode/<id>-base-<6hex>`) and stores its sha in
                # parent_merge_base_sha; the worktree forks off that.
                ("parent_task_ids", "TEXT"),  # JSON array
                ("parent_branches", "TEXT"),  # JSON array (local refs)
                ("parent_pr_branches", "TEXT"),  # JSON array (remote refs)
                ("parent_merge_base_sha", "TEXT"),
                ("parent_merge_base_branch", "TEXT"),
                # v3.5 cascade-on-push: track the most recent remote tip
                # observed for this task's branch. When the daemon's poll
                # sees a different tip, every descendant whose merge-base
                # depended on this branch is queued for rebase.
                ("last_observed_branch_tip_sha", "TEXT"),
                # v3.6 BLOCKED-as-bug forensics: when a task transitions to
                # BLOCKED, dump a comprehensive snapshot (last checker
                # outputs, retry-reason histogram, progress-check verdicts,
                # peak rss, last subtask state timeline) so post-mortem
                # diagnosis doesn't require log-grepping. JSON column.
                ("block_forensics", "TEXT"),
                # v3.6 pre-PR pipeline summary: JSON object recording the
                # most recent cycle's per-stage outcomes. Updated
                # incrementally as each stage finishes so the TUI can
                # surface "rubric ✓ / standards … / behavior — / local_ci ✓"
                # while a cycle is mid-run. Shape:
                # {"cycle": int, "stages":
                #     [{"name": "local_ci", "passed": bool, "summary": str}],
                #  "ts": float}
                ("pre_pr_audit_summary", "TEXT"),
                # v2.2 resume: skip the planner agent on the next provision
                # and reconstruct Plan from the existing subtasks rows. Set by
                # `quikode resume <id>`; cleared by the worker on consume.
                ("resume_from_existing_subtasks", "INTEGER DEFAULT 0"),
                # v3 Phase A/B/C: review-loop + intervention + stacked-diffs
                ("review_round", "INTEGER DEFAULT 0"),
                ("intervention_request", "TEXT"),
                ("draft_pr_number", "INTEGER"),
                ("last_review_poll_ts", "REAL"),
                ("pre_rebase_state", "TEXT"),
                # v3 stacked-diffs fix: mid-flight parent-merge flag
                ("needs_parent_rebase", "INTEGER DEFAULT 0"),
                # v3 polish: auto-merge audit flag
                ("auto_merged", "INTEGER DEFAULT 0"),
                ("last_notified_settled_ts", "REAL"),
                # rebase coalescing: per-task last-trigger timestamp
                ("last_rebase_scheduled_ts", "REAL"),
            ],
            "subtasks": [
                # v3 Phase A: per-subtask commits + transient/progress retries
                ("commit_sha", "TEXT"),
                ("transient_retries", "INTEGER DEFAULT 0"),
                ("progress_check_count", "INTEGER DEFAULT 0"),
                ("flatline_count", "INTEGER DEFAULT 0"),
                ("last_failure_root_cause_hash", "TEXT"),
                ("pre_commit_failures", "INTEGER DEFAULT 0"),
                # v3 fixup decomposition: distinguishes original spec subtasks
                # from fixup slices added on final-check / CI failure. Default
                # 'spec' preserves behavior for older rows.
                ("kind", "TEXT NOT NULL DEFAULT 'spec'"),
                # v3.5 retry-cause classification: JSON array, see schema
                # docstring above.
                ("retry_reasons", "TEXT"),
                # v3.7 advisory scope review: comma-separated effective lane
                # after the scope-reviewer accepted drift from
                # `files_to_touch` (auto-gen outputs, refactor splits,
                # companion files). NULL when the actual diff matched the
                # planner's declared lane exactly. Surfaced in `quikode show`
                # so the operator can see how a subtask's scope evolved.
                ("accepted_files", "TEXT"),
            ],
            "agent_calls": [
                # v2 Phase 0: scope agent_calls to a specific subtask
                ("subtask_id", "TEXT"),
                # v2.1 token detail: input/output/cached split + cost
                ("tokens_input", "INTEGER"),
                ("tokens_output", "INTEGER"),
                ("tokens_cached_read", "INTEGER"),
                ("tokens_cached_creation", "INTEGER"),
                ("cost_usd", "REAL"),
            ],
        }
        for table, cols in expected.items():
            try:
                with self._tx_lock:
                    rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            except sqlite3.OperationalError:
                continue  # table missing — fresh-DB path created it via executescript
            existing = {r[1] for r in rows}
            for name, col_type in cols:
                if name not in existing:
                    with self._tx_lock:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")

        # v3 new tables. `executescript(SCHEMA)` already creates these on
        # fresh DBs via `CREATE TABLE IF NOT EXISTS`, but pre-v3 DBs that
        # were last touched before these definitions existed don't have them
        # — and the SCHEMA running every init is exactly the idempotent path
        # we want, so all that matters is the table-creation SQL is part of
        # SCHEMA. The block below verifies presence as a defensive measure
        # so any future v3+ code can rely on these tables existing after
        # _migrate() returns.
        new_tables: dict[str, str] = {
            "review_threads": (
                "CREATE TABLE IF NOT EXISTS review_threads ("
                "task_id TEXT NOT NULL,"
                "thread_id TEXT NOT NULL,"
                "is_resolved INTEGER NOT NULL,"
                "last_comment_ts REAL NOT NULL,"
                "last_comment_author TEXT,"
                "last_comment_is_bot INTEGER DEFAULT 0,"
                "addressed_in_commit_sha TEXT,"
                "first_seen_ts REAL NOT NULL,"
                "PRIMARY KEY (task_id, thread_id)"
                ")"
            ),
            "progress_checks": (
                "CREATE TABLE IF NOT EXISTS progress_checks ("
                "task_id TEXT NOT NULL,"
                "subtask_id TEXT NOT NULL,"
                "ts REAL NOT NULL,"
                "attempts_at_check INTEGER NOT NULL,"
                "verdict TEXT NOT NULL,"
                "rationale TEXT,"
                "PRIMARY KEY (task_id, subtask_id, ts)"
                ")"
            ),
        }
        with self._tx_lock:
            existing_tables = {
                r[0]
                for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
        for name, ddl in new_tables.items():
            if name not in existing_tables:
                with self._tx_lock:
                    self.conn.execute(ddl)

        # Drop retired columns from any existing tasks table. The codebase is
        # 2 days old, post-legacy-purge: scalar parent_* columns and the
        # legacy whole-spec retry counters (do_check_retries,
        # review_triage_retries) are gone. SQLite ≥ 3.35 supports DROP
        # COLUMN; Linux runtime has 3.45+.
        with self._tx_lock:
            existing_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(tasks)").fetchall()}
            retired_cols = (
                # Replaced by parent_task_ids / parent_branches / parent_pr_branches
                # JSON arrays (purge 2/4).
                "parent_task_id",
                "parent_branch",
                "parent_pr_branch",
                # v0.1 monolithic flow retry counters; the per-subtask flow
                # uses subtasks.retries / retry_reasons instead (purge 3/4).
                "do_check_retries",
                "review_triage_retries",
            )
            # Backfill parent JSON arrays from scalars one last time before
            # dropping, so rows written by older code don't lose linkage.
            if "parent_task_id" in existing_cols:
                self.conn.execute(
                    "UPDATE tasks SET parent_task_ids = json_array(parent_task_id) "
                    "WHERE parent_task_id IS NOT NULL AND parent_task_ids IS NULL"
                )
            if "parent_branch" in existing_cols:
                self.conn.execute(
                    "UPDATE tasks SET parent_branches = json_array(parent_branch) "
                    "WHERE parent_branch IS NOT NULL AND parent_branches IS NULL"
                )
            if "parent_pr_branch" in existing_cols:
                self.conn.execute(
                    "UPDATE tasks SET parent_pr_branches = json_array(parent_pr_branch) "
                    "WHERE parent_pr_branch IS NOT NULL AND parent_pr_branches IS NULL"
                )
            for col in retired_cols:
                if col in existing_cols:
                    self.conn.execute(f"ALTER TABLE tasks DROP COLUMN {col}")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._tx_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    # ----- task lifecycle -----

    def upsert_pending(self, task_id: str) -> None:
        now = time.time()
        with self.tx() as c:
            r = c.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if r is None:
                c.execute(
                    "INSERT INTO tasks (id, state, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (task_id, State.PENDING.value, now, now),
                )
                c.execute(
                    "INSERT INTO state_log (task_id, from_state, to_state, ts) VALUES (?, NULL, ?, ?)",
                    (task_id, State.PENDING.value, now),
                )

    def transition(self, task_id: str, new_state: State, note: str | None = None, **fields: Any) -> None:
        now = time.time()
        with self.tx() as c:
            r = c.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
            from_state = r["state"] if r else None
            sets = ["state = ?", "updated_at = ?"]
            vals: list[Any] = [new_state.value, now]
            for k, v in fields.items():
                sets.append(f"{k} = ?")
                vals.append(v)
            vals.append(task_id)
            c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals)
            c.execute(
                "INSERT INTO state_log (task_id, from_state, to_state, note, ts) VALUES (?, ?, ?, ?, ?)",
                (task_id, from_state, new_state.value, note, now),
            )
        # v3.6 BLOCKED-as-bug: every BLOCKED transition triggers a forensics
        # snapshot. Best-effort — a failure here must not crash the worker
        # (the BLOCK itself is what the operator needs first; the dump is
        # diagnostic, not load-bearing). We avoid re-capturing if the
        # `from_state` was already BLOCKED (defensive: re-blocking shouldn't
        # overwrite the original snapshot's framing).
        if new_state is State.BLOCKED and from_state != State.BLOCKED.value:
            try:
                self.capture_block_forensics(task_id)
            except Exception as e:
                log.warning("capture_block_forensics(%s) raised: %s — continuing", task_id, e)

    def get(self, task_id: str) -> TaskRow | None:
        # _tx_lock serializes ALL connection access (reads + writes), not
        # just transactions. With check_same_thread=False the sqlite3
        # module accepts concurrent calls from multiple threads, but a
        # second thread starting an `execute` while the first is mid-fetch
        # raises `InterfaceError: bad parameter or other API misuse`.
        # Wrapping reads in the same lock as writes prevents that race.
        with self._tx_lock:
            r = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(r) if r else None  # type: ignore[return-value]

    def all_tasks(self) -> list[TaskRow]:
        with self._tx_lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()]  # type: ignore[misc]

    def in_state(self, *states: State) -> list[TaskRow]:
        if not states:
            return []
        q = ",".join("?" * len(states))
        with self._tx_lock:
            return [
                dict(r)  # type: ignore[misc]
                for r in self.conn.execute(
                    f"SELECT * FROM tasks WHERE state IN ({q}) ORDER BY id",
                    tuple(s.value for s in states),
                ).fetchall()
            ]

    def last_entered_state_ts(self, task_id: str, state: State) -> float | None:
        """Most recent ts at which `task_id` transitioned INTO `state`, or None.

        Reads `state_log`. Used by the stacking-readiness gate to compute "how
        long has this parent been quietly in AWAITING_MERGE?" — a parent that
        flapped through ADDRESSING_FEEDBACK and back gets a fresh ts and
        falls back below the quiet threshold until it stabilizes.
        """
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT MAX(ts) FROM state_log WHERE task_id = ? AND to_state = ?",
                (task_id, state.value),
            ).fetchone()
        if r is None:
            return None
        ts = r[0]
        return float(ts) if ts is not None else None

    def subtask_progress(self, task_id: str) -> tuple[int, int]:
        """Return (done, total) subtask counts for `task_id`.

        Used by the resume-boost in `score_candidate`: a task with most
        subtasks already DONE that returned to PENDING (orphan recovery,
        explicit resume) should outrank a fresh PENDING root with no work.
        """
        with self._tx_lock:
            row = self.conn.execute(
                "SELECT "
                "  SUM(CASE WHEN state='done' THEN 1 ELSE 0 END) AS done, "
                "  COUNT(*) AS total "
                "FROM subtasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return (0, 0)
        return (int(row["done"] or 0), int(row["total"] or 0))

    def completed_ids(self) -> set[str]:
        with self._tx_lock:
            return {
                r["id"]
                for r in self.conn.execute(
                    "SELECT id FROM tasks WHERE state = ?", (State.MERGED.value,)
                ).fetchall()
            }

    def active_ids(self) -> set[str]:
        with self._tx_lock:
            return {
                r["id"]
                for r in self.conn.execute(
                    "SELECT id FROM tasks WHERE state NOT IN (?, ?, ?, ?, ?, ?)",
                    (
                        State.PENDING.value,
                        State.MERGED.value,
                        State.BLOCKED.value,
                        State.FAILED.value,
                        State.ABORTED.value,
                        State.PENDING_CI.value,  # tasks waiting on merge block dependents until merged
                    ),
                ).fetchall()
            }

    def record_agent_call(
        self,
        task_id: str,
        *,
        phase: str,
        cli: str,
        model: str | None,
        rc: int,
        duration_s: float,
        tokens_used: int | None,
        subtask_id: str | None = None,
        tokens_input: int | None = None,
        tokens_output: int | None = None,
        tokens_cached_read: int | None = None,
        tokens_cached_creation: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO agent_calls "
                "(task_id, phase, cli, model, rc, duration_s, tokens_used, "
                " tokens_input, tokens_output, tokens_cached_read, tokens_cached_creation, "
                " cost_usd, subtask_id, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    phase,
                    cli,
                    model,
                    rc,
                    duration_s,
                    tokens_used,
                    tokens_input,
                    tokens_output,
                    tokens_cached_read,
                    tokens_cached_creation,
                    cost_usd,
                    subtask_id,
                    time.time(),
                ),
            )

    # ----- v2 subtasks -----

    def upsert_subtasks(self, task_id: str, subtasks: list[dict]) -> None:
        """Replace any existing subtasks for this task with the given list."""
        now = time.time()
        with self.tx() as c:
            c.execute("DELETE FROM subtasks WHERE task_id = ?", (task_id,))
            for s in subtasks:
                c.execute(
                    "INSERT INTO subtasks "
                    "(task_id, subtask_id, title, depends_on, files_to_touch, boundary, "
                    " acceptance, notes, kind, state, retries, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (
                        task_id,
                        s["subtask_id"],
                        s.get("title", ""),
                        json.dumps(s.get("depends_on", [])),
                        json.dumps(s.get("files_to_touch", [])),
                        s.get("boundary", ""),
                        json.dumps(s.get("acceptance", [])),
                        s.get("notes", ""),
                        s.get("kind", "spec"),
                        SubtaskState.PENDING.value,
                        now,
                        now,
                    ),
                )

    def append_subtasks(self, task_id: str, subtasks: list[dict]) -> None:
        """Append new subtasks to the existing set for `task_id` without deleting.

        Used by the v3 fixup-decomposition flow: when final-check or CI fails,
        the fixup planner emits a small Plan of additive slices that need to
        run after the original spec subtasks have already settled DONE.
        Skips rows whose `subtask_id` already exists for the task — the
        planner is responsible for unique IDs (e.g. `F-final-1-line-budget`)
        but we double-guard so a planner repeat doesn't error mid-round.
        """
        now = time.time()
        with self.tx() as c:
            existing = {
                r[0]
                for r in c.execute("SELECT subtask_id FROM subtasks WHERE task_id = ?", (task_id,)).fetchall()
            }
            for s in subtasks:
                if s["subtask_id"] in existing:
                    continue
                c.execute(
                    "INSERT INTO subtasks "
                    "(task_id, subtask_id, title, depends_on, files_to_touch, boundary, "
                    " acceptance, notes, kind, state, retries, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (
                        task_id,
                        s["subtask_id"],
                        s.get("title", ""),
                        json.dumps(s.get("depends_on", [])),
                        json.dumps(s.get("files_to_touch", [])),
                        s.get("boundary", ""),
                        json.dumps(s.get("acceptance", [])),
                        s.get("notes", ""),
                        s.get("kind", "spec"),
                        SubtaskState.PENDING.value,
                        now,
                        now,
                    ),
                )

    def list_subtasks(self, task_id: str) -> list[SubtaskRow]:
        with self._tx_lock:
            rows = self.conn.execute(
                "SELECT * FROM subtasks WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]  # type: ignore[misc]

    def get_subtask(self, task_id: str, subtask_id: str) -> SubtaskRow | None:
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT * FROM subtasks WHERE task_id = ? AND subtask_id = ?",
                (task_id, subtask_id),
            ).fetchone()
        return dict(r) if r else None  # type: ignore[return-value]

    def update_subtask(self, task_id: str, subtask_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = [*list(fields.values()), time.time(), task_id, subtask_id]
        with self.tx() as c:
            c.execute(
                f"UPDATE subtasks SET {sets}, updated_at = ? WHERE task_id = ? AND subtask_id = ?",
                vals,
            )

    def record_container_stats(
        self,
        task_id: str,
        container_name: str,
        cpu_pct: float | None,
        mem_bytes: int | None,
        mem_pct: float | None,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO container_stats (task_id, container_name, cpu_pct, mem_bytes, mem_pct, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, container_name, cpu_pct, mem_bytes, mem_pct, time.time()),
            )

    def task_total_cost_usd(self, task_id: str) -> float | None:
        """Sum of `agent_calls.cost_usd` for a task. None when nothing
        has been logged yet (or every row has cost_usd NULL — providers
        that don't report cost). Used by `quikode briefing` to surface
        per-task spend."""
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT SUM(cost_usd) AS s FROM agent_calls WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if r is None:
            return None
        v = r["s"]
        return float(v) if v is not None else None

    def workspace_total_cost_usd(self) -> float | None:
        """Sum of `agent_calls.cost_usd` across all tasks. None when
        nothing's been logged yet."""
        with self._tx_lock:
            r = self.conn.execute("SELECT SUM(cost_usd) AS s FROM agent_calls").fetchone()
        if r is None:
            return None
        v = r["s"]
        return float(v) if v is not None else None

    def task_max_rss(self, task_id: str) -> int | None:
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT MAX(mem_bytes) AS m FROM container_stats WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return int(r["m"]) if r and r["m"] else None

    def mark_needs_intent_review(self, task_ids: list[str], triggered_by: str) -> None:
        if not task_ids:
            return
        with self.tx() as c:
            for tid in task_ids:
                c.execute(
                    "UPDATE tasks SET needs_intent_review = 1, updated_at = ? WHERE id = ?",
                    (time.time(), tid),
                )

    def clear_intent_review_flag(self, task_id: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET needs_intent_review = 0, last_intent_review_ts = ?, "
                "intent_review_count = COALESCE(intent_review_count, 0) + 1, updated_at = ? "
                "WHERE id = ?",
                (time.time(), time.time(), task_id),
            )

    def record_intent_review(
        self,
        task_id: str,
        *,
        triggered_by_merge_of: str | None,
        main_sha_before: str | None,
        main_sha_after: str | None,
        verdict: str,
        explanation: str,
        affected_areas: str,
        raw_output: str,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO intent_reviews "
                "(task_id, triggered_by_merge_of, main_sha_before, main_sha_after, "
                " verdict, explanation, affected_areas, raw_output, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    triggered_by_merge_of,
                    main_sha_before,
                    main_sha_after,
                    verdict,
                    explanation,
                    affected_areas,
                    raw_output,
                    time.time(),
                ),
            )

    def latest_container_stats(self, task_id: str) -> ContainerStatsRow | None:
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT * FROM container_stats WHERE task_id = ? ORDER BY ts DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return dict(r) if r else None  # type: ignore[return-value]

    def increment_subtask_retries(self, task_id: str, subtask_id: str) -> int:
        with self.tx() as c:
            c.execute(
                "UPDATE subtasks SET retries = COALESCE(retries, 0) + 1, updated_at = ? "
                "WHERE task_id = ? AND subtask_id = ?",
                (time.time(), task_id, subtask_id),
            )
            r = c.execute(
                "SELECT retries FROM subtasks WHERE task_id = ? AND subtask_id = ?",
                (task_id, subtask_id),
            ).fetchone()
            return int(r["retries"]) if r else 0

    def get_parent_task_ids(self, task_id: str) -> list[str]:
        """Read the JSON-array parent_task_ids for a task.

        Always returns a list (possibly empty)."""
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT parent_task_ids FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if r is None or not r["parent_task_ids"]:
            return []
        try:
            arr = json.loads(r["parent_task_ids"])
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(arr, list):
            return []
        return [str(x) for x in arr if x]

    def get_parent_branches(self, task_id: str) -> list[str]:
        """Read JSON-array parent_branches. Always returns a list."""
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT parent_branches FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if r is None or not r["parent_branches"]:
            return []
        try:
            arr = json.loads(r["parent_branches"])
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(arr, list):
            return []
        return [str(x) for x in arr if x]

    def set_parent_chain(
        self,
        task_id: str,
        *,
        parent_task_ids: list[str],
        parent_branches: list[str] | None = None,
        parent_pr_branches: list[str] | None = None,
    ) -> None:
        """Stamp the multi-parent linkage on a task. Pass empty lists (or
        None) to clear all parent linkage."""
        ids_json = json.dumps(list(parent_task_ids))
        branches_json = json.dumps(list(parent_branches or []))
        pr_branches_json = json.dumps(list(parent_pr_branches or []))
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET "
                "  parent_task_ids = ?, parent_branches = ?, parent_pr_branches = ?, "
                "  updated_at = ? "
                "WHERE id = ?",
                (ids_json, branches_json, pr_branches_json, time.time(), task_id),
            )

    def get_pre_pr_audit_summary(self, task_id: str) -> dict | None:
        """Read the most recent pre-PR audit summary for a task.

        Shape:
          {"cycle": int, "stages": [{"name": str, "passed": bool|None,
                                     "summary": str}], "ts": float}
        `passed=None` means the stage hasn't run yet in the current
        cycle (or is currently in flight). The TUI uses that to render
        a "…" indicator distinct from pass/fail.
        """
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT pre_pr_audit_summary FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if r is None or not r["pre_pr_audit_summary"]:
            return None
        try:
            data = json.loads(r["pre_pr_audit_summary"])
        except (json.JSONDecodeError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def begin_pre_pr_audit_cycle(self, task_id: str, cycle: int) -> None:
        """Reset the audit summary at the top of a new cycle so stale stage
        results from prior cycles don't bleed into the TUI display. The
        four stages are pre-seeded with `passed=None` (in-flight) so the
        operator sees a "queued" indicator before each stage actually runs.
        """
        seeded = {
            "cycle": cycle,
            "ts": time.time(),
            "stages": [
                {"name": "local_ci", "passed": None, "summary": "queued"},
                {"name": "rubric", "passed": None, "summary": "queued"},
                {"name": "standards", "passed": None, "summary": "queued"},
                {"name": "behavior", "passed": None, "summary": "queued"},
            ],
        }
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET pre_pr_audit_summary = ?, updated_at = ? WHERE id = ?",
                (json.dumps(seeded), time.time(), task_id),
            )

    def update_pre_pr_audit_stage(
        self,
        task_id: str,
        *,
        cycle: int,
        stage_name: str,
        passed: bool,
        summary: str,
    ) -> None:
        """Update one stage's outcome on the current cycle. Idempotent:
        re-calling with the same stage name overwrites. If the cycle on
        disk doesn't match the caller's cycle, no-op (defensive against
        a worker that re-entered the pipeline before clearing)."""
        existing = self.get_pre_pr_audit_summary(task_id)
        if existing is None or existing.get("cycle") != cycle:
            # Caller forgot to call begin_pre_pr_audit_cycle — seed lazily.
            self.begin_pre_pr_audit_cycle(task_id, cycle)
            existing = self.get_pre_pr_audit_summary(task_id)
            if existing is None:
                return
        stages = list(existing.get("stages") or [])
        replaced = False
        for s in stages:
            if s.get("name") == stage_name:
                s["passed"] = bool(passed)
                s["summary"] = str(summary)[:300]
                replaced = True
                break
        if not replaced:
            stages.append({"name": stage_name, "passed": bool(passed), "summary": str(summary)[:300]})
        existing["stages"] = stages
        existing["ts"] = time.time()
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET pre_pr_audit_summary = ?, updated_at = ? WHERE id = ?",
                (json.dumps(existing), time.time(), task_id),
            )

    def get_block_forensics(self, task_id: str) -> dict | None:
        """Read the BLOCKED-forensics JSON dump for a task. None when no
        block has occurred (or the column is empty)."""
        with self._tx_lock:
            r = self.conn.execute("SELECT block_forensics FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if r is None or not r["block_forensics"]:
            return None
        try:
            data = json.loads(r["block_forensics"])
        except (json.JSONDecodeError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def set_block_forensics(self, task_id: str, snapshot: dict) -> None:
        """Persist a forensics snapshot. Caller assembles the dict; this
        just stores it. Snapshot is JSON-serializable; non-serializable
        values are dropped via `default=str`."""
        try:
            blob = json.dumps(snapshot, default=str)[:200000]
        except (TypeError, ValueError):
            blob = json.dumps({"_serialization_error": True})
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET block_forensics = ?, updated_at = ? WHERE id = ?",
                (blob, time.time(), task_id),
            )

    def capture_block_forensics(self, task_id: str) -> dict:
        """Build + persist a comprehensive BLOCKED-forensics snapshot.

        Designed for the operator's "what should the system have done
        differently?" question — not just "what failed." Captures:

          - retry-reason histogram across all subtasks
          - last 5 distinct checker outputs (deduped on first 80 chars)
          - last 5 triage notes
          - last 3 progress-check verdicts
          - peak container RSS observed
          - last 20 state-log transitions
          - subtasks state distribution

        Returns the snapshot dict (caller can also read via
        `get_block_forensics`).
        """
        snapshot: dict = {"task_id": task_id, "captured_at": time.time()}

        # retry_reasons aggregate
        with self._tx_lock:
            sub_rows = self.conn.execute(
                "SELECT subtask_id, retries, transient_retries, flatline_count, "
                "pre_commit_failures, retry_reasons FROM subtasks WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        retry_summary: dict[str, int] = {}
        per_subtask_retries: list[dict] = []
        for sr in sub_rows:
            d = dict(sr)
            try:
                rr = json.loads(d.get("retry_reasons") or "[]")
            except (json.JSONDecodeError, TypeError):
                rr = []
            cats = {}
            for entry in rr:
                cat = entry.get("category", "other")
                cats[cat] = cats.get(cat, 0) + 1
                retry_summary[cat] = retry_summary.get(cat, 0) + 1
            per_subtask_retries.append(
                {
                    "subtask_id": d.get("subtask_id"),
                    "retries": d.get("retries") or 0,
                    "transient_retries": d.get("transient_retries") or 0,
                    "flatline_count": d.get("flatline_count") or 0,
                    "pre_commit_failures": d.get("pre_commit_failures") or 0,
                    "retry_categories": cats,
                    "recent_retry_examples": rr[-3:],
                }
            )
        snapshot["retry_categories_total"] = retry_summary
        snapshot["per_subtask"] = per_subtask_retries

        # Last 5 distinct checker outputs
        with self._tx_lock:
            arts = self.conn.execute(
                "SELECT kind, content FROM artifacts WHERE task_id = ? "
                "AND kind LIKE 'subtask_checker:%' ORDER BY id DESC LIMIT 20",
                (task_id,),
            ).fetchall()
        seen_starts: set[str] = set()
        last_checker_outputs: list[dict] = []
        for art in arts:
            content = (art["content"] or "")[:1500]
            head = content[:80]
            if head in seen_starts:
                continue
            seen_starts.add(head)
            last_checker_outputs.append({"kind": art["kind"], "excerpt": content})
            if len(last_checker_outputs) >= 5:
                break
        snapshot["last_checker_outputs"] = last_checker_outputs

        # Last 5 triage notes
        with self._tx_lock:
            tarts = self.conn.execute(
                "SELECT kind, content FROM artifacts WHERE task_id = ? "
                "AND kind LIKE 'subtask_triage:%' ORDER BY id DESC LIMIT 5",
                (task_id,),
            ).fetchall()
        snapshot["last_triage_notes"] = [
            {"kind": t["kind"], "excerpt": (t["content"] or "")[:1500]} for t in tarts
        ]

        # Last 3 progress-check verdicts
        with self._tx_lock:
            pc = self.conn.execute(
                "SELECT subtask_id, verdict, rationale, ts FROM progress_checks "
                "WHERE task_id = ? ORDER BY ts DESC LIMIT 3",
                (task_id,),
            ).fetchall()
        snapshot["last_progress_checks"] = [
            {
                "subtask_id": p["subtask_id"],
                "verdict": p["verdict"],
                "rationale": (p["rationale"] or "")[:500],
                "ts": p["ts"],
            }
            for p in pc
        ]

        # Peak RSS
        with self._tx_lock:
            rss = self.conn.execute(
                "SELECT MAX(mem_bytes) FROM container_stats WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        snapshot["peak_mem_bytes"] = int(rss[0]) if rss and rss[0] else None

        # Last 20 state transitions
        with self._tx_lock:
            sl = self.conn.execute(
                "SELECT from_state, to_state, ts, note FROM state_log "
                "WHERE task_id = ? ORDER BY ts DESC LIMIT 20",
                (task_id,),
            ).fetchall()
        snapshot["recent_state_log"] = [
            {
                "from_state": s["from_state"],
                "to_state": s["to_state"],
                "ts": s["ts"],
                "note": (s["note"] or "")[:200],
            }
            for s in sl
        ]

        # Subtask state distribution
        with self._tx_lock:
            sd = self.conn.execute(
                "SELECT state, COUNT(*) AS n FROM subtasks WHERE task_id = ? GROUP BY state",
                (task_id,),
            ).fetchall()
        snapshot["subtask_states"] = {row["state"]: int(row["n"]) for row in sd}

        self.set_block_forensics(task_id, snapshot)
        return snapshot

    def get_last_observed_branch_tip_sha(self, task_id: str) -> str | None:
        """Read the cascade-on-push tracker: the most recent remote-branch tip
        sha we observed for this task. None when never seen / column absent."""
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT last_observed_branch_tip_sha FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if r is None:
            return None
        v = r["last_observed_branch_tip_sha"]
        return str(v) if v else None

    def set_last_observed_branch_tip_sha(self, task_id: str, sha: str) -> None:
        """Stamp the most-recent remote-branch tip sha for cascade detection."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET last_observed_branch_tip_sha = ?, updated_at = ? WHERE id = ?",
                (sha, time.time(), task_id),
            )

    def set_parent_merge_base(self, task_id: str, *, branch: str | None, sha: str | None) -> None:
        """Record the synthetic merge-base branch + sha used for multi-parent
        stacking. Either argument may be None to clear."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET parent_merge_base_branch = ?, parent_merge_base_sha = ?, "
                "updated_at = ? WHERE id = ?",
                (branch, sha, time.time(), task_id),
            )

    def append_retry_reason(
        self,
        task_id: str,
        subtask_id: str,
        *,
        attempt: int,
        category: str,
        signature: str,
        transient: bool = False,
    ) -> None:
        """Record one retry's cause + fingerprint on the subtask row.

        `retry_reasons` is a JSON array of objects:
          [{"attempt": 3, "ts": 1777938xxx.xx,
            "category": "checker_fail", "signature": "verdict=FAIL",
            "transient": false}, ...]

        Bounded at the latest 50 entries — pathological retry storms
        (R-0019 F-1-1 saw 25+ in one stretch) shouldn't blow the column up
        unboundedly. The histogram in `quikode show` only needs counts; the
        signatures are kept for the most-recent entries to surface examples.
        """
        with self.tx() as c:
            r = c.execute(
                "SELECT retry_reasons FROM subtasks WHERE task_id = ? AND subtask_id = ?",
                (task_id, subtask_id),
            ).fetchone()
            try:
                existing = json.loads(r["retry_reasons"]) if r and r["retry_reasons"] else []
            except (json.JSONDecodeError, TypeError):
                existing = []
            if not isinstance(existing, list):
                existing = []
            existing.append(
                {
                    "attempt": int(attempt),
                    "ts": time.time(),
                    "category": str(category),
                    "signature": str(signature)[:200],
                    "transient": bool(transient),
                }
            )
            # Keep tail; counts are preserved by retry_reason_histogram.
            if len(existing) > 50:
                existing = existing[-50:]
            c.execute(
                "UPDATE subtasks SET retry_reasons = ?, updated_at = ? WHERE task_id = ? AND subtask_id = ?",
                (json.dumps(existing), time.time(), task_id, subtask_id),
            )

    def retry_reasons(self, task_id: str, subtask_id: str) -> list[dict]:
        """Read back the retry_reasons JSON array. Empty list when missing/malformed."""
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT retry_reasons FROM subtasks WHERE task_id = ? AND subtask_id = ?",
                (task_id, subtask_id),
            ).fetchone()
        if r is None or r["retry_reasons"] is None:
            return []
        try:
            data = json.loads(r["retry_reasons"])
        except (json.JSONDecodeError, TypeError):
            return []
        return list(data) if isinstance(data, list) else []

    def increment_subtask_pre_commit_failures(self, task_id: str, subtask_id: str) -> int:
        """Bump the pre-commit-failure counter for a subtask. Distinct from
        `retries` so the operator can tell hook-gate rejections apart from
        real verdict-FAILs in the briefing."""
        with self.tx() as c:
            c.execute(
                "UPDATE subtasks SET pre_commit_failures = COALESCE(pre_commit_failures, 0) + 1, "
                "updated_at = ? WHERE task_id = ? AND subtask_id = ?",
                (time.time(), task_id, subtask_id),
            )
            r = c.execute(
                "SELECT pre_commit_failures FROM subtasks WHERE task_id = ? AND subtask_id = ?",
                (task_id, subtask_id),
            ).fetchone()
            return int(r["pre_commit_failures"]) if r else 0

    def increment_subtask_flatline_count(self, task_id: str, subtask_id: str) -> int:
        """Bump consecutive-flatline counter. Reset to 0 by
        `reset_subtask_flatline_count` on any non-flatline progress verdict."""
        with self.tx() as c:
            c.execute(
                "UPDATE subtasks SET flatline_count = COALESCE(flatline_count, 0) + 1, "
                "progress_check_count = COALESCE(progress_check_count, 0) + 1, "
                "updated_at = ? WHERE task_id = ? AND subtask_id = ?",
                (time.time(), task_id, subtask_id),
            )
            r = c.execute(
                "SELECT flatline_count FROM subtasks WHERE task_id = ? AND subtask_id = ?",
                (task_id, subtask_id),
            ).fetchone()
            return int(r["flatline_count"]) if r else 0

    def reset_subtask_flatline_count(self, task_id: str, subtask_id: str) -> None:
        """Zero out the consecutive-flatline counter (still bumps total
        progress_check_count so the operator can see how often the agent
        ran)."""
        with self.tx() as c:
            c.execute(
                "UPDATE subtasks SET flatline_count = 0, "
                "progress_check_count = COALESCE(progress_check_count, 0) + 1, "
                "updated_at = ? WHERE task_id = ? AND subtask_id = ?",
                (time.time(), task_id, subtask_id),
            )

    def increment_subtask_transient_retries(self, task_id: str, subtask_id: str) -> int:
        """Bump the transient-retry counter for a subtask. Used for
        container/network/push-network glitches that don't burn the real
        retry budget."""
        with self.tx() as c:
            c.execute(
                "UPDATE subtasks SET transient_retries = COALESCE(transient_retries, 0) + 1, "
                "updated_at = ? WHERE task_id = ? AND subtask_id = ?",
                (time.time(), task_id, subtask_id),
            )
            r = c.execute(
                "SELECT transient_retries FROM subtasks WHERE task_id = ? AND subtask_id = ?",
                (task_id, subtask_id),
            ).fetchone()
            return int(r["transient_retries"]) if r else 0

    def record_progress_check(
        self,
        task_id: str,
        subtask_id: str,
        *,
        attempts_at_check: int,
        verdict: str,
        rationale: str | None,
    ) -> None:
        """Audit row for one progress-check agent invocation.

        Inserted every time the v3 progress-check agent fires (or fails to
        fire — `uncertain` rows from agent-transient errors land here too).
        Used by `quikode show` / TUI to show why a subtask was eventually
        blocked on flatline grounds, and by tests to verify cadence.
        """
        with self.tx() as c:
            c.execute(
                "INSERT INTO progress_checks "
                "(task_id, subtask_id, ts, attempts_at_check, verdict, rationale) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, subtask_id, time.time(), attempts_at_check, verdict, rationale),
            )

    def get_recent_progress_checks(
        self, task_id: str, subtask_id: str, *, limit: int = 10
    ) -> list[ProgressCheckRow]:
        """Return the most-recent progress-check audit rows for a subtask
        (newest first)."""
        with self._tx_lock:
            rows = self.conn.execute(
                "SELECT * FROM progress_checks WHERE task_id = ? AND subtask_id = ? ORDER BY ts DESC LIMIT ?",
                (task_id, subtask_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]  # type: ignore[misc]

    def recent_subtask_checker_outputs(self, task_id: str, subtask_id: str, *, limit: int = 5) -> list[str]:
        """Return the last N checker artifact bodies for a given subtask,
        oldest first. Used by the v3 progress-check agent to derive
        per-attempt root-cause history.

        We look at the artifact stream rather than agent_calls because the
        latter doesn't store agent stdout. The artifact `kind` for a subtask
        checker is `subtask_checker:<subtask_id>`.
        """
        with self._tx_lock:
            rows = self.conn.execute(
                "SELECT content FROM artifacts WHERE task_id = ? AND kind = ? ORDER BY ts DESC LIMIT ?",
                (task_id, f"subtask_checker:{subtask_id}", limit),
            ).fetchall()
        # Reverse so caller sees oldest-first (matches "attempt 1 ... attempt N").
        return [str(r["content"] or "") for r in reversed(rows)]

    def add_artifact(self, task_id: str, kind: str, content: str, is_path: bool = False) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO artifacts (task_id, kind, content, is_path, ts) VALUES (?, ?, ?, ?, ?)",
                (task_id, kind, content, 1 if is_path else 0, time.time()),
            )

    def increment(self, task_id: str, field: str) -> int:
        with self.tx() as c:
            c.execute(
                f"UPDATE tasks SET {field} = COALESCE({field}, 0) + 1, updated_at = ? WHERE id = ?",
                (time.time(), task_id),
            )
            r = c.execute(f"SELECT {field} FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return int(r[field]) if r else 0

    def reset_field(self, task_id: str, field: str, value: Any = 0) -> None:
        with self.tx() as c:
            c.execute(
                f"UPDATE tasks SET {field} = ?, updated_at = ? WHERE id = ?", (value, time.time(), task_id)
            )

    def set_field(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = [*list(fields.values()), time.time(), task_id]
        with self.tx() as c:
            c.execute(f"UPDATE tasks SET {sets}, updated_at = ? WHERE id = ?", vals)

    # ----- v3 Phase B: review-thread polling + response cycles -----

    def tasks_needing_review_poll(self, *, cutoff: float) -> list[TaskRow]:
        """Return tasks whose last review-poll is older than `cutoff` (or
        never polled). Used by the daemon's review-watcher pass to throttle
        GraphQL traffic.

        Includes:
        - PENDING_CI / AWAITING_REVIEW / MERGE_READY: the normal case — the
          PR is open and waiting on something; poll for new threads + merge.
          The poll itself drives transitions between these three sub-states
          based on CI + review-approval signals.
        - BLOCKED with a `pr_number`: the BLOCKED PR comment lists "reply
          with guidance as a review comment" as an intervention path; for
          that to actually work, the watcher keeps polling these PRs.
          On a new non-bot thread, the response cycle fires and (on
          success) transitions the task back to PENDING_CI.
        """
        post_pr_states = (
            State.PENDING_CI.value,
            State.AWAITING_REVIEW.value,
            State.MERGE_READY.value,
        )
        placeholders = ",".join("?" * len(post_pr_states))
        with self._tx_lock:
            rows = self.conn.execute(
                f"SELECT * FROM tasks "
                f"WHERE (state IN ({placeholders}) OR (state = ? AND pr_number IS NOT NULL)) "
                f"AND (last_review_poll_ts IS NULL OR last_review_poll_ts < ?) "
                f"ORDER BY id",
                (*post_pr_states, State.BLOCKED.value, cutoff),
            ).fetchall()
        return [dict(r) for r in rows]  # type: ignore[misc]

    def get_stored_review_threads(self, task_id: str) -> list[ReviewThreadRow]:
        with self._tx_lock:
            rows = self.conn.execute(
                "SELECT * FROM review_threads WHERE task_id = ? ORDER BY first_seen_ts",
                (task_id,),
            ).fetchall()
        return [dict(r) for r in rows]  # type: ignore[misc]

    def get_review_thread(self, task_id: str, thread_id: str) -> ReviewThreadRow | None:
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT * FROM review_threads WHERE task_id = ? AND thread_id = ?",
                (task_id, thread_id),
            ).fetchone()
        return dict(r) if r else None  # type: ignore[return-value]

    def upsert_review_thread(
        self,
        task_id: str,
        *,
        thread_id: str,
        is_resolved: bool,
        last_comment_ts: float,
        last_comment_author: str | None,
        last_comment_is_bot: bool,
    ) -> None:
        """Insert a new review_thread row or update an existing one. Preserves
        `addressed_in_commit_sha` from a prior row — that is set explicitly by
        `mark_thread_addressed` and must not be cleared by a poll refresh."""
        now = time.time()
        with self.tx() as c:
            existing = c.execute(
                "SELECT addressed_in_commit_sha, first_seen_ts FROM review_threads "
                "WHERE task_id = ? AND thread_id = ?",
                (task_id, thread_id),
            ).fetchone()
            if existing is None:
                c.execute(
                    "INSERT INTO review_threads "
                    "(task_id, thread_id, is_resolved, last_comment_ts, last_comment_author, "
                    " last_comment_is_bot, addressed_in_commit_sha, first_seen_ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
                    (
                        task_id,
                        thread_id,
                        1 if is_resolved else 0,
                        last_comment_ts,
                        last_comment_author,
                        1 if last_comment_is_bot else 0,
                        now,
                    ),
                )
            else:
                c.execute(
                    "UPDATE review_threads SET "
                    "is_resolved = ?, last_comment_ts = ?, last_comment_author = ?, "
                    "last_comment_is_bot = ? "
                    "WHERE task_id = ? AND thread_id = ?",
                    (
                        1 if is_resolved else 0,
                        last_comment_ts,
                        last_comment_author,
                        1 if last_comment_is_bot else 0,
                        task_id,
                        thread_id,
                    ),
                )

    def mark_thread_addressed(self, task_id: str, thread_id: str, commit_sha: str) -> None:
        """Record that the given thread was addressed by a specific commit.
        Subsequent polls compare incoming `last_comment_ts` against the row's
        existing `last_comment_ts` to decide whether the thread became
        unaddressed again (new comment after addressing)."""
        with self.tx() as c:
            c.execute(
                "UPDATE review_threads SET addressed_in_commit_sha = ? WHERE task_id = ? AND thread_id = ?",
                (commit_sha, task_id, thread_id),
            )

    # ----- v3 Phase C: stacked diffs / parent-merge rebase plumbing -----

    def children_of_parent_branch(self, parent_branch: str) -> list[TaskRow]:
        """Return non-terminal child tasks whose `parent_pr_branches` JSON
        array contains `parent_branch`.

        Used by the orchestrator to find every child that needs to rebase
        when the parent's PR merges or pushes a new commit. Excludes
        terminal states (MERGED, ABORTED, FAILED, BLOCKED) — there's
        nothing left to rebase for those — and PENDING (no work has
        begun, so the next provision will pick up the new base naturally).
        """
        terminal = (
            State.MERGED.value,
            State.ABORTED.value,
            State.FAILED.value,
            State.BLOCKED.value,
            State.PENDING.value,
        )
        q = ",".join("?" * len(terminal))
        with self._tx_lock:
            rows = self.conn.execute(
                f"SELECT * FROM tasks WHERE state NOT IN ({q}) "
                f"AND parent_pr_branches IS NOT NULL "
                f"AND EXISTS (SELECT 1 FROM json_each(parent_pr_branches) "
                f"            WHERE json_each.value = ?) "
                f"ORDER BY id",
                (*terminal, parent_branch),
            ).fetchall()
        return [dict(r) for r in rows]  # type: ignore[misc]

    def clear_parent_branch(self, task_id: str) -> None:
        """Clear stacked-diff parent-branch metadata. Called after a child
        successfully rebases onto main, OR when the parent's PR closed
        without merging (no longer a valid stack base either way)."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET "
                "  parent_task_ids = NULL, parent_branches = NULL, parent_pr_branches = NULL, "
                "  parent_merge_base_sha = NULL, parent_merge_base_branch = NULL, "
                "  needs_parent_rebase = 0, updated_at = ? WHERE id = ?",
                (time.time(), task_id),
            )

    def mark_needs_parent_rebase(self, task_id: str) -> None:
        """Set the mid-flight parent-merge flag. Worker checks at safe
        checkpoints and runs an inline rebase + retarget before proceeding."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET needs_parent_rebase = 1, updated_at = ? WHERE id = ?",
                (time.time(), task_id),
            )

    def clear_needs_parent_rebase(self, task_id: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET needs_parent_rebase = 0, updated_at = ? WHERE id = ?",
                (time.time(), task_id),
            )

    def children_with_parent_branch(self, parent_branch: str) -> list[TaskRow]:
        """Return ALL non-terminal tasks whose `parent_pr_branches` JSON
        array contains `parent_branch` — regardless of whether
        `_schedule_rebases_for_merged_parent` will also schedule a rebase
        future. Used to clear stale parent metadata when a parent closes
        without merging."""
        terminal = (
            State.MERGED.value,
            State.ABORTED.value,
            State.FAILED.value,
        )
        q = ",".join("?" * len(terminal))
        with self._tx_lock:
            rows = self.conn.execute(
                f"SELECT * FROM tasks WHERE state NOT IN ({q}) "
                f"AND parent_pr_branches IS NOT NULL "
                f"AND EXISTS (SELECT 1 FROM json_each(parent_pr_branches) "
                f"            WHERE json_each.value = ?) "
                f"ORDER BY id",
                (*terminal, parent_branch),
            ).fetchall()
        return [dict(r) for r in rows]  # type: ignore[misc]

    def set_pre_rebase_state(self, task_id: str, state: str) -> None:
        """Stash the pre-rebase active state on the row so the rebase worker
        can restore it after a successful rebase. Idempotent."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET pre_rebase_state = ?, updated_at = ? WHERE id = ?",
                (state, time.time(), task_id),
            )

    def get_pre_rebase_state(self, task_id: str) -> str | None:
        with self._tx_lock:
            r = self.conn.execute("SELECT pre_rebase_state FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if r is None:
            return None
        v = r["pre_rebase_state"]
        return str(v) if v is not None else None

    def recover_orphan_tasks(self) -> list[tuple[str, str, str]]:
        """Reset tasks that were mid-flight when the orchestrator died.

        Called once at `quikode run` startup before the orchestrator is
        constructed. Each task in an active state had a worker driving it;
        after a crash/SIGTERM nothing is, so the row would otherwise sit
        forever (the picker only picks PENDING).

        Recovery rules per state — see docs/design-stacked-diffs-fix.md §3:
        * provisioning → pending (clear branch/wt/cid)
        * planning → pending (preserve plan_text via resume marker if set)
        * doing/checking/triaging (legacy + subtask) → pending + resume marker
        * final_checking → pending + resume marker
        * committing/pushing → pending + resume marker
        * pr_opening → awaiting_merge (if pr_number set) else pending + resume
        * polling_ci → pending_ci (if pr_number) else pending + resume
        * triaging_feedback → pending_ci (let daemon re-detect)
        * addressing_feedback → pending_ci (let daemon re-detect)
        * rebasing/conflict_resolving → pending_ci (if pr_number) else pending + resume
        * intent_reviewing → pending_ci (if pr_number) else pending + resume
        * rebasing_to_main → pending_ci (if pr_number) else pending + resume
        * replanning → pending + resume

        All recovery transitions also reset retry counters so the next
        attempt has a fresh budget.

        PR-aware tasks land in PENDING_CI rather than the more specific
        AWAITING_REVIEW / MERGE_READY because the daemon's poll re-derives
        the right state from CI + review signals on its next tick.

        Returns list of (task_id, from_state, to_state) for caller logging.
        """
        active_states = {
            State.PROVISIONING,
            State.PLANNING,
            State.DOING_SUBTASK,
            State.CHECKING_SUBTASK,
            State.TRIAGING_SUBTASK,
            State.COMMITTING,
            State.PUSHING,
            State.PR_OPENING,
            State.POLLING_CI,
            State.REBASING,
            State.CONFLICT_RESOLVING,
            State.INTENT_REVIEWING,
            State.REPLANNING,
            State.FIXUP_PLANNING,
            State.TRIAGING_FEEDBACK,
            State.ADDRESSING_FEEDBACK,
            State.LOCAL_CI_CHECKING,
            State.PRE_PR_AUDITING,
            State.PRE_PR_TRIAGING,
            State.REBASING_TO_MAIN,
        }
        # Subset that stays in active phases mid-implementation; any of these
        # is safe to roll back to PENDING with the resume marker so the worker
        # picks up where it left off (subtask granularity).
        resume_to_pending = {
            State.PLANNING,
            State.DOING_SUBTASK,
            State.CHECKING_SUBTASK,
            State.TRIAGING_SUBTASK,
            State.COMMITTING,
            State.PUSHING,
            State.REPLANNING,
            State.FIXUP_PLANNING,
        }
        # States where falling back to a post-PR state only makes sense if the
        # PR has actually been opened. Otherwise resume to PENDING.
        # Lands in PENDING_CI; the daemon's poll re-classifies based on
        # current CI + review signals.
        pr_aware = {
            State.PR_OPENING,
            State.POLLING_CI,
            State.LOCAL_CI_CHECKING,
            State.PRE_PR_AUDITING,
            State.PRE_PR_TRIAGING,
            State.TRIAGING_FEEDBACK,
            State.ADDRESSING_FEEDBACK,
            State.REBASING,
            State.CONFLICT_RESOLVING,
            State.INTENT_REVIEWING,
            State.REBASING_TO_MAIN,
        }
        retry_reset_fields = {
            "ci_triage_retries": 0,
            "conflict_resolve_retries": 0,
            "needs_intent_review": 0,
            "needs_parent_rebase": 0,
            "last_error": None,
        }

        recovered: list[tuple[str, str, str]] = []
        for row in self.all_tasks():
            try:
                cur = State(row["state"])
            except ValueError:
                continue
            if cur not in active_states:
                continue

            from_state = cur.value
            extras: dict[str, Any] = dict(retry_reset_fields)

            if cur is State.PROVISIONING:
                # No work to preserve — clear partial provision artifacts.
                extras.update(branch=None, worktree_path=None, container_id=None)
                target = State.PENDING
            elif cur in resume_to_pending:
                # Plan + subtasks (if any) survive on disk; the worker will
                # rebuild the in-memory Plan from `subtasks` when this marker
                # is set.
                extras["resume_from_existing_subtasks"] = 1
                target = State.PENDING
            elif cur in pr_aware:
                if row.get("pr_number"):
                    target = State.PENDING_CI
                else:
                    extras["resume_from_existing_subtasks"] = 1
                    target = State.PENDING
            else:
                # Unhandled active state — be safe and resume to pending.
                extras["resume_from_existing_subtasks"] = 1
                target = State.PENDING

            self.transition(row["id"], target, note=f"orphan recovery from {from_state}", **extras)
            recovered.append((row["id"], from_state, target.value))
        return recovered

    def get_last_rebase_scheduled_ts(self, task_id: str) -> float | None:
        """Read the most recent rebase-trigger timestamp for a task, or
        None when never set. Used by the orchestrator's coalescing window
        check in `_schedule_rebase_to_main`."""
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT last_rebase_scheduled_ts FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if r is None:
            return None
        v = r["last_rebase_scheduled_ts"]
        return float(v) if v is not None else None

    def set_last_rebase_scheduled(self, task_id: str, ts: float) -> None:
        """Stamp the most-recent rebase-trigger timestamp on a task. Caller
        is responsible for the coalescing-window comparison; this just
        persists the value."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET last_rebase_scheduled_ts = ?, updated_at = ? WHERE id = ?",
                (ts, time.time(), task_id),
            )

    def increment_review_round(self, task_id: str) -> int:
        """Bump the human-driven review→respond cycle counter for a task."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET review_round = COALESCE(review_round, 0) + 1, updated_at = ? WHERE id = ?",
                (time.time(), task_id),
            )
            r = c.execute("SELECT review_round FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return int(r["review_round"]) if r else 0
