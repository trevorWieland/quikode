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
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any, NotRequired, TypedDict

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
    do_check_retries: NotRequired[int | None]
    ci_triage_retries: NotRequired[int | None]
    review_triage_retries: NotRequired[int | None]
    last_pr_event_ts: NotRequired[str | None]
    base_ref_sha: NotRequired[str | None]
    last_synced_main_sha: NotRequired[str | None]
    conflict_resolve_retries: NotRequired[int | None]
    needs_intent_review: NotRequired[int | None]
    last_intent_review_ts: NotRequired[float | None]
    intent_review_count: NotRequired[int | None]
    replan_count: NotRequired[int | None]
    parent_task_id: NotRequired[str | None]
    parent_branch: NotRequired[str | None]
    resume_from_existing_subtasks: NotRequired[int | None]
    # v3 Phase A/B/C: review-loop + intervention + stacked-diffs metadata
    review_round: NotRequired[int | None]
    intervention_request: NotRequired[str | None]  # JSON {kind, message, posted_pr_comment_id, ts}
    parent_pr_branch: NotRequired[str | None]  # stacked-diffs base
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
    # v0.1 monolithic flow
    DOING = "doing"
    CHECKING = "checking"
    TRIAGING = "triaging"
    # v2 Phase 0: subtask flow
    DOING_SUBTASK = "doing_subtask"
    CHECKING_SUBTASK = "checking_subtask"
    TRIAGING_SUBTASK = "triaging_subtask"
    FINAL_CHECKING = "final_checking"  # whole-spec checker after all subtasks
    # post-implementation
    COMMITTING = "committing"
    PUSHING = "pushing"
    PR_OPENING = "pr_opening"
    POLLING_CI = "polling_ci"
    # v2 Phase A: parallel-safe merge handling
    REBASING = "rebasing"  # rebasing onto current main; clean→back to polling
    CONFLICT_RESOLVING = "conflict_resolving"  # spawned resolver agent on a conflicted rebase
    # v2 Phase B: intent-gap detection
    INTENT_REVIEWING = "intent_reviewing"  # checking if main has shifted under us in a way that breaks intent
    REPLANNING = "replanning"  # producing a new plan in light of intent conflict
    # v3 fixup decomposition: invoking the fixup planner on a final-check
    # or CI failure to break the fixup into per-subtask slices instead of a
    # monolithic doer attempt. Brief transitional state (entered from
    # triaging / polling_ci, exited to doing_subtask once subtasks are
    # appended). Distinct from REPLANNING because the original spec plan
    # is preserved — fixup slices are *additive*, not replacements.
    FIXUP_PLANNING = "fixup_planning"
    AWAITING_MERGE = "awaiting_merge"
    # v3 Phase B: a review thread came in while the PR was awaiting merge —
    # the daemon submitted a fresh worker slot to address it. Distinct from
    # the normal active states because the worker is reusing the existing
    # worktree/branch/PR, not provisioning anything new.
    RESPONDING_TO_REVIEW = "responding_to_review"
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


TERMINAL = {State.MERGED, State.AWAITING_MERGE, State.BLOCKED, State.FAILED, State.ABORTED}
ACTIVE = {
    State.PROVISIONING,
    State.PLANNING,
    State.DOING,
    State.CHECKING,
    State.TRIAGING,
    State.DOING_SUBTASK,
    State.CHECKING_SUBTASK,
    State.TRIAGING_SUBTASK,
    State.FINAL_CHECKING,
    State.COMMITTING,
    State.PUSHING,
    State.PR_OPENING,
    State.POLLING_CI,
    State.REBASING,
    State.CONFLICT_RESOLVING,
    State.INTENT_REVIEWING,
    State.REPLANNING,
    State.FIXUP_PLANNING,
    State.RESPONDING_TO_REVIEW,
    State.REBASING_TO_MAIN,
}


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
    do_check_retries INTEGER DEFAULT 0,
    ci_triage_retries INTEGER DEFAULT 0,
    review_triage_retries INTEGER DEFAULT 0,
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
    -- v2 Phase C: stacked diffs. If this task was branched off another task's
    -- in-flight branch instead of main, parent_task_id points to that dep.
    parent_task_id TEXT,
    parent_branch TEXT,
    -- v2.2 resume: when 1, the worker skips the planner agent on next
    -- provision and reconstructs the Plan from the existing subtasks rows.
    -- Set by `quikode resume <id>`; cleared by the worker on consume.
    resume_from_existing_subtasks INTEGER DEFAULT 0,
    -- v3 Phase A/B/C: review-loop + intervention + stacked-diffs metadata.
    -- review_round counts how many human-driven review→respond cycles this
    -- task has gone through. intervention_request is a JSON blob carrying
    -- kind/message/comment-id/ts when the daemon needs human attention.
    -- parent_pr_branch is the stacking base for child tasks; draft_pr_number
    -- is the early draft PR (opened after S-01) distinct from pr_number.
    review_round INTEGER DEFAULT 0,
    intervention_request TEXT,
    parent_pr_branch TEXT,
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
                # v2 Phase C: stacked diffs
                ("parent_task_id", "TEXT"),
                ("parent_branch", "TEXT"),
                # v2.2 resume: skip the planner agent on the next provision
                # and reconstruct Plan from the existing subtasks rows. Set by
                # `quikode resume <id>`; cleared by the worker on consume.
                ("resume_from_existing_subtasks", "INTEGER DEFAULT 0"),
                # v3 Phase A/B/C: review-loop + intervention + stacked-diffs
                ("review_round", "INTEGER DEFAULT 0"),
                ("intervention_request", "TEXT"),
                ("parent_pr_branch", "TEXT"),
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

        # v3 Phase B: rename `awaiting_human` rows to `awaiting_merge`. The
        # State enum lost AWAITING_HUMAN entirely; pre-v3 DBs may still hold
        # rows at the old value. Idempotent — UPDATE matches zero rows on
        # already-migrated DBs.
        with self._tx_lock:
            self.conn.execute("UPDATE tasks SET state = 'awaiting_merge' WHERE state = 'awaiting_human'")
            self.conn.execute(
                "UPDATE state_log SET to_state = 'awaiting_merge' WHERE to_state = 'awaiting_human'"
            )
            self.conn.execute(
                "UPDATE state_log SET from_state = 'awaiting_merge' WHERE from_state = 'awaiting_human'"
            )

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
                        State.AWAITING_MERGE.value,  # tasks waiting on merge block dependents until merged
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
        - AWAITING_MERGE: the normal case — poll for new threads + merged.
        - BLOCKED with a `pr_number`: the BLOCKED PR comment lists "reply
          with guidance as a review comment" as an intervention path; for
          that to actually work, the watcher keeps polling these PRs.
          On a new non-bot thread, the response cycle fires and (on
          success) transitions the task back to AWAITING_MERGE.
        """
        with self._tx_lock:
            rows = self.conn.execute(
                "SELECT * FROM tasks "
                "WHERE (state = ? OR (state = ? AND pr_number IS NOT NULL)) "
                "AND (last_review_poll_ts IS NULL OR last_review_poll_ts < ?) "
                "ORDER BY id",
                (State.AWAITING_MERGE.value, State.BLOCKED.value, cutoff),
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
        """Return non-terminal child tasks whose parent_pr_branch matches.

        Used by the orchestrator to find every child that needs to rebase
        when the parent's PR merges. Excludes terminal states (MERGED,
        ABORTED, FAILED, BLOCKED) — there's nothing left to rebase for
        those — and PENDING (no work has begun, so the next provision
        will pick up the new base naturally if parent_pr_branch is
        cleared).
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
                f"SELECT * FROM tasks WHERE parent_pr_branch = ? AND state NOT IN ({q}) ORDER BY id",
                (parent_branch, *terminal),
            ).fetchall()
        return [dict(r) for r in rows]  # type: ignore[misc]

    def clear_parent_branch(self, task_id: str) -> None:
        """Clear stacked-diff parent-branch metadata. Called after a child
        successfully rebases onto main, OR when the parent's PR closed
        without merging (no longer a valid stack base either way)."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET parent_pr_branch = NULL, parent_branch = NULL, "
                "needs_parent_rebase = 0, updated_at = ? WHERE id = ?",
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
        """Return ALL non-terminal tasks whose parent_pr_branch matches —
        regardless of whether `_schedule_rebases_for_merged_parent` will
        also schedule a rebase future. Used to clear stale parent metadata
        when a parent closes without merging."""
        terminal = (
            State.MERGED.value,
            State.ABORTED.value,
            State.FAILED.value,
        )
        q = ",".join("?" * len(terminal))
        with self._tx_lock:
            rows = self.conn.execute(
                f"SELECT * FROM tasks WHERE parent_pr_branch = ? AND state NOT IN ({q}) ORDER BY id",
                (parent_branch, *terminal),
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
        * polling_ci → awaiting_merge (if pr_number) else pending + resume
        * responding_to_review → awaiting_merge (let watcher re-detect)
        * rebasing/conflict_resolving → awaiting_merge (if pr_number) else pending + resume
        * intent_reviewing → awaiting_merge (if pr_number) else pending + resume
        * rebasing_to_main → awaiting_merge (if pr_number) else pending + resume
        * replanning → pending + resume

        All recovery transitions also reset retry counters so the next
        attempt has a fresh budget.

        Returns list of (task_id, from_state, to_state) for caller logging.
        """
        active_states = {
            State.PROVISIONING,
            State.PLANNING,
            State.DOING,
            State.CHECKING,
            State.TRIAGING,
            State.DOING_SUBTASK,
            State.CHECKING_SUBTASK,
            State.TRIAGING_SUBTASK,
            State.FINAL_CHECKING,
            State.COMMITTING,
            State.PUSHING,
            State.PR_OPENING,
            State.POLLING_CI,
            State.REBASING,
            State.CONFLICT_RESOLVING,
            State.INTENT_REVIEWING,
            State.REPLANNING,
            State.FIXUP_PLANNING,
            State.RESPONDING_TO_REVIEW,
            State.REBASING_TO_MAIN,
        }
        # Subset that stays in active phases mid-implementation; any of these
        # is safe to roll back to PENDING with the resume marker so the worker
        # picks up where it left off (subtask granularity).
        resume_to_pending = {
            State.PLANNING,
            State.DOING,
            State.CHECKING,
            State.TRIAGING,
            State.DOING_SUBTASK,
            State.CHECKING_SUBTASK,
            State.TRIAGING_SUBTASK,
            State.FINAL_CHECKING,
            State.COMMITTING,
            State.PUSHING,
            State.REPLANNING,
            State.FIXUP_PLANNING,
        }
        # States where falling back to AWAITING_MERGE only makes sense if the
        # PR has actually been opened. Otherwise resume to PENDING.
        pr_aware = {
            State.PR_OPENING,
            State.POLLING_CI,
            State.RESPONDING_TO_REVIEW,
            State.REBASING,
            State.CONFLICT_RESOLVING,
            State.INTENT_REVIEWING,
            State.REBASING_TO_MAIN,
        }
        retry_reset_fields = {
            "do_check_retries": 0,
            "ci_triage_retries": 0,
            "review_triage_retries": 0,
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
                    target = State.AWAITING_MERGE
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
