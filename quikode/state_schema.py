"""SQLite schema DDL."""

from __future__ import annotations

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
    -- task has been quiet (MERGE_READY + green + no churn for
    -- cfg.notify_settled_after_s), it pings the configured channel and
    -- stamps this column. Re-pings are gated on the task having LEFT
    -- MERGE_READY since the last notify (e.g. responded to a thread)
    -- so we don't spam on every poll tick.
    last_notified_settled_ts REAL,
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
