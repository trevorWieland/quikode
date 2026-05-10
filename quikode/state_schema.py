"""SQLite schema DDL + plan-28 idempotent migrations.

Fresh-install columns live in `SCHEMA`; existing-DB column adds + state
renames live in `apply_migrations`, which is called once during `Store.__init__`
right after `executescript(SCHEMA)`. Migrations are idempotent — re-running on
an already-migrated DB is a no-op.
"""

from __future__ import annotations

import logging
import re
import sqlite3

log = logging.getLogger("quikode.state_schema")

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
    failure_reason TEXT,
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
    -- Plan 30 review-ready notification: ts of the most recent ntfy fire
    -- announcing a task reached AWAITING_REVIEW (CI green, ready for human
    -- review). Column name is a plan-28 carry-over (the original v3
    -- "settled" notification surface retired with MERGE_READY); SQLite
    -- column-drop requires a table rebuild so the name was retained and
    -- repurposed. See `store_review.get_last_review_ready_notified_ts`.
    last_notified_settled_ts REAL,
    -- Plan 28: most recent GitHub Review id we've already routed into
    -- ADDRESSING_FEEDBACK. Prevents re-trigger on the same CHANGES_REQUESTED
    -- after a daemon restart. NULL until first non-bot review observed.
    last_processed_review_id TEXT,
    -- Plan 32: row kind. 'spec' = regular DAG-seeded task (default).
    -- 'merge' = synthetic merge-node integrating multiple parents into a
    -- shared base branch. Merge-nodes have no PR; their lifecycle ends in
    -- MERGE_NODE_READY (serving as a base) or MERGE_NODE_RETIRED (all
    -- source parents merged to main). Plan-22 carry-forward, plan-30
    -- review-ready settle, plan-31 rebase semantics all key off `kind`.
    kind TEXT NOT NULL DEFAULT 'spec',
    parent_task_ids TEXT,
    parent_branches TEXT,
    parent_pr_branches TEXT,
    parent_merge_base_sha TEXT,
    parent_merge_base_branch TEXT,
    last_observed_branch_tip_sha TEXT,
    block_forensics TEXT,
    pre_pr_audit_summary TEXT,
    seed_source TEXT,
    seed_evidence TEXT,
    seeded_at REAL,
    -- Plan 52: hint set by `qk replan-cycle` so the worker / operator can
    -- see which planning cycle was reset and (eventually) why it re-fired.
    -- JSON: {"cycle": int, "kind": str, "ts": float}. Cleared by the
    -- worker once the replanned cycle's subtasks have been re-emitted.
    replan_cycle_marker TEXT,
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
    -- Plan 38 PR-C: when the worker invokes an agent it FIRST inserts a
    -- start-marker row with `started_at = now`, leaving rc/duration_s
    -- NULL. On return the worker UPDATEs the same row with rc/duration_s
    -- + token/cost detail. The TUI distinguishes "agent in-flight" from
    -- "agent idle" by `rc IS NULL AND duration_s IS NULL` on the
    -- newest row per task. Single-call inserts (no start-marker) set
    -- started_at = ts so the rollup queries stay unaffected.
    started_at REAL,
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
    -- Plan 52: planning provenance per row. `planning_cycle` is the
    -- 1-based ordinal of the planner invocation that emitted this row;
    -- `planning_kind` is the kind of planner (initial / fixup / replan
    -- / merge). Together they let `qk replan-cycle` target only the
    -- most-recent cycle's rows, preserving every earlier cycle's
    -- committed work + retry counters. Defaults match the initial
    -- planner so pre-plan-52 rows backfill cleanly.
    planning_cycle INTEGER NOT NULL DEFAULT 1,
    planning_kind TEXT NOT NULL DEFAULT 'initial',
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


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Idempotent migrations for existing DBs.

    Plan 28: add `tasks.last_processed_review_id` if missing, and rename the
    two retired post-PR states (`merge_ready`, `triaging_feedback`) to their
    streamlined replacements. Both UPDATEs are no-ops on fresh installs and on
    re-runs.
    """
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN last_processed_review_id TEXT")
    except sqlite3.OperationalError:
        # Column already present (fresh install via SCHEMA, or re-run).
        pass
    # Plan 32: add `kind` column (spec | merge). Default 'spec' for any
    # pre-existing rows.
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'spec'")
    except sqlite3.OperationalError:
        pass
    # Plan 28 state migration. `merge_ready` re-evaluates on next poll;
    # `triaging_feedback` was always a transient classifier state — recovering
    # rows there land in PENDING_CI which is what the daemon would do anyway
    # via `recover_after_crash`.
    conn.execute("UPDATE tasks SET state='awaiting_review' WHERE state='merge_ready'")
    conn.execute("UPDATE tasks SET state='pending_ci' WHERE state='triaging_feedback'")
    # Plan 38 PR-C: start-marker for in-flight agent_call detection.
    # Existing rows backfill to started_at = ts (call finished instantly
    # in our prior single-INSERT semantics) so historical queries are
    # unaffected.
    try:
        conn.execute("ALTER TABLE agent_calls ADD COLUMN started_at REAL")
    except sqlite3.OperationalError:
        pass
    conn.execute("UPDATE agent_calls SET started_at = ts WHERE started_at IS NULL")
    # Plan 38 calibration: planner validator/parse failures pass a compact
    # machine-readable failure reason through the generic transition path.
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN failure_reason TEXT")
    except sqlite3.OperationalError:
        pass
    # Plan 52: subtask planning provenance + per-task replan marker.
    _apply_plan52_migration(conn)


# Plan 52 backfill heuristic: subtask-id naming convention encodes the
# planner that emitted the row. `S-NN-*` and `Z-99-*` are initial-cycle.
# `F-N-...-*` (where N is a digit) indicates fixup-cycle N+1 — the +1
# offsets the initial planner's cycle 1. `F-CI-*` is CI-driven fixup;
# we lump those into a synthetic `(2, "fixup_ci")` because we don't have
# a per-task ordinal at backfill time. Anything that doesn't match falls
# back to (1, "initial") so the field stays well-defined for the new
# `qk replan-cycle` primitive without rewriting historical truth.
_FIXUP_NUMERIC_RE = re.compile(r"^F-(\d+)-")
_FIXUP_CI_RE = re.compile(r"^F-CI-")
_INITIAL_PREFIX_RE = re.compile(r"^(S-\d+|Z-99|R-\d+)-?")


def _infer_planning_provenance(subtask_id: str) -> tuple[int, str]:
    """Map a subtask id to (planning_cycle, planning_kind).

    Heuristic only — used for backfill of pre-plan-52 rows. New rows are
    populated explicitly by each planner call site so the CLI's cycle
    targeting stays exact.
    """
    if _INITIAL_PREFIX_RE.match(subtask_id):
        return (1, "initial")
    m = _FIXUP_NUMERIC_RE.match(subtask_id)
    if m:
        try:
            cycle = int(m.group(1)) + 1
        except ValueError:
            return (1, "initial")
        return (max(cycle, 2), "fixup")
    if _FIXUP_CI_RE.match(subtask_id):
        return (2, "fixup_ci")
    return (1, "initial")


def _apply_plan52_migration(conn: sqlite3.Connection) -> None:
    """Add subtasks.planning_cycle / planning_kind + tasks.replan_cycle_marker.

    Backfills existing subtask rows via `_infer_planning_provenance`. A
    failure to parse a single subtask id MUST NOT prevent daemon
    startup; we fall back to the (1, "initial") default and warn.
    """
    try:
        conn.execute("ALTER TABLE subtasks ADD COLUMN planning_cycle INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE subtasks ADD COLUMN planning_kind TEXT NOT NULL DEFAULT 'initial'")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN replan_cycle_marker TEXT")
    except sqlite3.OperationalError:
        pass
    # Backfill: only rows that still hold the column defaults. Re-runs are
    # no-ops because the WHERE narrows to default rows only.
    try:
        rows = conn.execute(
            "SELECT id, subtask_id FROM subtasks WHERE planning_cycle = 1 AND planning_kind = 'initial'"
        ).fetchall()
    except sqlite3.OperationalError:
        return
    for row in rows:
        sid = row["subtask_id"] if isinstance(row, sqlite3.Row) else row[1]
        rowid = row["id"] if isinstance(row, sqlite3.Row) else row[0]
        try:
            cycle, kind = _infer_planning_provenance(str(sid))
        except Exception as exc:
            log.warning(
                "plan52 backfill: could not infer planning provenance for "
                "subtask id %r (rowid=%s): %s — leaving defaults",
                sid,
                rowid,
                exc,
            )
            continue
        if cycle == 1 and kind == "initial":
            # Defaults already match — skip the UPDATE for ID-prefix
            # matches that resolved to the default values.
            continue
        try:
            conn.execute(
                "UPDATE subtasks SET planning_cycle = ?, planning_kind = ? WHERE id = ?",
                (cycle, kind, rowid),
            )
        except sqlite3.OperationalError as exc:
            log.warning(
                "plan52 backfill: UPDATE failed for subtask rowid=%s id=%r: %s",
                rowid,
                sid,
                exc,
            )
