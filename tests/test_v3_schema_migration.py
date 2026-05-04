"""v3 schema migration: new columns on tasks/subtasks + new tables for
review threads and progress-check audit.

Mirrors the v2 migration tests. Pre-v3 DBs (built from the post-v2 shape but
without the v3 columns/tables) must round-trip cleanly through `Store._migrate`.
"""

from __future__ import annotations

import sqlite3
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from quikode.config import Config
from quikode.state import (
    ProgressCheckRow,
    ReviewThreadRow,
    State,
    Store,
    SubtaskRow,
    TaskRow,
)

# ----- expected v3 additions -----

_V3_TASK_COLUMNS = {
    "review_round",
    "intervention_request",
    "parent_pr_branch",
    "draft_pr_number",
    "last_review_poll_ts",
}
_V3_SUBTASK_COLUMNS = {
    "commit_sha",
    "transient_retries",
    "progress_check_count",
    "flatline_count",
    "last_failure_root_cause_hash",
    "pre_commit_failures",
}
_V3_NEW_TABLES = {"review_threads", "progress_checks"}

_REVIEW_THREADS_COLUMNS = {
    "task_id",
    "thread_id",
    "is_resolved",
    "last_comment_ts",
    "last_comment_author",
    "last_comment_is_bot",
    "addressed_in_commit_sha",
    "first_seen_ts",
}
_PROGRESS_CHECKS_COLUMNS = {
    "task_id",
    "subtask_id",
    "ts",
    "attempts_at_check",
    "verdict",
    "rationale",
}


def _create_pre_v3_schema(db_path) -> None:
    """Build a SQLite DB with the post-v2 shape but missing all v3 columns
    and the v3 tables. Models the state of a workspace last touched before
    this batch landed."""
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("""
        CREATE TABLE tasks (
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
            base_ref_sha TEXT,
            last_synced_main_sha TEXT,
            conflict_resolve_retries INTEGER DEFAULT 0,
            needs_intent_review INTEGER DEFAULT 0,
            last_intent_review_ts REAL,
            intent_review_count INTEGER DEFAULT 0,
            replan_count INTEGER DEFAULT 0,
            parent_task_id TEXT,
            parent_branch TEXT,
            resume_from_existing_subtasks INTEGER DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE state_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT NOT NULL,
            note TEXT,
            ts REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            content TEXT,
            is_path INTEGER DEFAULT 0,
            ts REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE agent_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            phase TEXT NOT NULL,
            cli TEXT NOT NULL,
            model TEXT,
            rc INTEGER,
            duration_s REAL,
            tokens_used INTEGER,
            tokens_input INTEGER,
            tokens_output INTEGER,
            tokens_cached_read INTEGER,
            tokens_cached_creation INTEGER,
            cost_usd REAL,
            subtask_id TEXT,
            ts REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE subtasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            subtask_id TEXT NOT NULL,
            title TEXT,
            depends_on TEXT,
            files_to_touch TEXT,
            boundary TEXT,
            acceptance TEXT,
            notes TEXT,
            state TEXT NOT NULL,
            retries INTEGER DEFAULT 0,
            last_error TEXT,
            triage_notes TEXT,
            created_at REAL,
            updated_at REAL,
            UNIQUE(task_id, subtask_id)
        )
    """)
    conn.execute("""
        CREATE TABLE container_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            container_name TEXT,
            cpu_pct REAL,
            mem_bytes INTEGER,
            mem_pct REAL,
            ts REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE intent_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            triggered_by_merge_of TEXT,
            main_sha_before TEXT,
            main_sha_after TEXT,
            verdict TEXT NOT NULL,
            explanation TEXT,
            affected_areas TEXT,
            raw_output TEXT,
            ts REAL NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO tasks (id, state, branch, created_at, updated_at) "
        "VALUES ('R-LEGACY', 'pending', 'legacy-branch', 100.0, 200.0)"
    )
    conn.execute(
        "INSERT INTO subtasks (task_id, subtask_id, state, retries, created_at, updated_at) "
        "VALUES ('R-LEGACY', 'S-01-leg', 'pending', 0, 100.0, 200.0)"
    )
    conn.close()


def _table_columns(db_path, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    conn.close()
    return {r[1] for r in rows}


def _table_names(db_path) -> set[str]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    conn.close()
    return {r[0] for r in rows}


# ----- fresh-DB tests -----


def test_fresh_db_has_v3_task_columns(tmp_path):
    db = tmp_path / "fresh.db"
    Store(db).conn.close()
    cols = _table_columns(db, "tasks")
    missing = _V3_TASK_COLUMNS - cols
    assert not missing, f"fresh DB missing v3 task columns: {missing}"


def test_fresh_db_has_v3_subtask_columns(tmp_path):
    db = tmp_path / "fresh.db"
    Store(db).conn.close()
    cols = _table_columns(db, "subtasks")
    missing = _V3_SUBTASK_COLUMNS - cols
    assert not missing, f"fresh DB missing v3 subtask columns: {missing}"


def test_fresh_db_has_v3_new_tables(tmp_path):
    db = tmp_path / "fresh.db"
    Store(db).conn.close()
    tables = _table_names(db)
    missing = _V3_NEW_TABLES - tables
    assert not missing, f"fresh DB missing v3 tables: {missing}"


def test_fresh_review_threads_columns(tmp_path):
    db = tmp_path / "fresh.db"
    Store(db).conn.close()
    cols = _table_columns(db, "review_threads")
    assert cols == _REVIEW_THREADS_COLUMNS, (
        f"review_threads schema drift: extra={cols - _REVIEW_THREADS_COLUMNS} "
        f"missing={_REVIEW_THREADS_COLUMNS - cols}"
    )


def test_fresh_progress_checks_columns(tmp_path):
    db = tmp_path / "fresh.db"
    Store(db).conn.close()
    cols = _table_columns(db, "progress_checks")
    assert cols == _PROGRESS_CHECKS_COLUMNS, (
        f"progress_checks schema drift: extra={cols - _PROGRESS_CHECKS_COLUMNS} "
        f"missing={_PROGRESS_CHECKS_COLUMNS - cols}"
    )


# ----- pre-v3 migration tests -----


def test_migration_adds_v3_task_columns(tmp_path):
    db = tmp_path / "old.db"
    _create_pre_v3_schema(db)
    cols_before = _table_columns(db, "tasks")
    assert _V3_TASK_COLUMNS - cols_before == _V3_TASK_COLUMNS  # all missing pre-migrate

    Store(db).conn.close()

    cols_after = _table_columns(db, "tasks")
    missing = _V3_TASK_COLUMNS - cols_after
    assert not missing, f"migration didn't add v3 task columns: {missing}"


def test_migration_adds_v3_subtask_columns(tmp_path):
    db = tmp_path / "old.db"
    _create_pre_v3_schema(db)
    cols_before = _table_columns(db, "subtasks")
    assert _V3_SUBTASK_COLUMNS - cols_before == _V3_SUBTASK_COLUMNS

    Store(db).conn.close()

    cols_after = _table_columns(db, "subtasks")
    missing = _V3_SUBTASK_COLUMNS - cols_after
    assert not missing, f"migration didn't add v3 subtask columns: {missing}"


def test_migration_creates_v3_new_tables(tmp_path):
    db = tmp_path / "old.db"
    _create_pre_v3_schema(db)
    tables_before = _table_names(db)
    assert _V3_NEW_TABLES & tables_before == set()  # neither table existed pre-migrate

    Store(db).conn.close()

    tables_after = _table_names(db)
    missing = _V3_NEW_TABLES - tables_after
    assert not missing, f"migration didn't create v3 tables: {missing}"


def test_migration_preserves_existing_rows(tmp_path):
    db = tmp_path / "old.db"
    _create_pre_v3_schema(db)
    s = Store(db)
    row = s.get("R-LEGACY")
    assert row is not None
    assert row["state"] == State.PENDING.value
    assert row["branch"] == "legacy-branch"
    # New v3 columns default to 0 (INTEGER DEFAULT 0) or None (TEXT/REAL/INTEGER no default)
    assert row["review_round"] in (0, None)
    assert row["intervention_request"] is None
    assert row["parent_pr_branch"] is None
    assert row["draft_pr_number"] is None
    assert row["last_review_poll_ts"] is None
    s.conn.close()


def test_migration_idempotent(tmp_path):
    """Run _migrate on the same DB twice; the second run is a no-op and the
    schema matches itself column-for-column, table-for-table."""
    db = tmp_path / "old.db"
    _create_pre_v3_schema(db)

    s1 = Store(db)
    cols_tasks_1 = _table_columns(db, "tasks")
    cols_subtasks_1 = _table_columns(db, "subtasks")
    cols_rt_1 = _table_columns(db, "review_threads")
    cols_pc_1 = _table_columns(db, "progress_checks")
    tables_1 = _table_names(db)
    s1.conn.close()

    # second run via fresh Store wraps another _migrate() pass on the same file
    s2 = Store(db)
    # also call _migrate explicitly to confirm in-process idempotency
    s2._migrate()
    cols_tasks_2 = _table_columns(db, "tasks")
    cols_subtasks_2 = _table_columns(db, "subtasks")
    cols_rt_2 = _table_columns(db, "review_threads")
    cols_pc_2 = _table_columns(db, "progress_checks")
    tables_2 = _table_names(db)
    s2.conn.close()

    assert cols_tasks_1 == cols_tasks_2
    assert cols_subtasks_1 == cols_subtasks_2
    assert cols_rt_1 == cols_rt_2
    assert cols_pc_1 == cols_pc_2
    assert tables_1 == tables_2


def test_in_memory_double_migrate_pragmas(tmp_path):
    """Open a fresh Store, call _migrate twice, then verify each of the
    four tables of interest has the expected columns. This is the manual
    schema-verification check called for in the exit criteria."""
    db = tmp_path / "fresh.db"
    s = Store(db)
    s._migrate()
    s._migrate()  # third pass overall (init runs once); must not error

    tasks_cols = {r[1] for r in s.conn.execute("PRAGMA table_info(tasks)").fetchall()}
    subtasks_cols = {r[1] for r in s.conn.execute("PRAGMA table_info(subtasks)").fetchall()}
    rt_cols = {r[1] for r in s.conn.execute("PRAGMA table_info(review_threads)").fetchall()}
    pc_cols = {r[1] for r in s.conn.execute("PRAGMA table_info(progress_checks)").fetchall()}

    assert tasks_cols >= _V3_TASK_COLUMNS
    assert subtasks_cols >= _V3_SUBTASK_COLUMNS
    assert rt_cols == _REVIEW_THREADS_COLUMNS
    assert pc_cols == _PROGRESS_CHECKS_COLUMNS

    s.conn.close()


# ----- TypedDict surface -----


def test_task_row_has_v3_keys():
    hints = get_type_hints(TaskRow, include_extras=False)
    for key in _V3_TASK_COLUMNS:
        assert key in hints, f"TaskRow missing v3 key: {key}"


def test_subtask_row_has_v3_keys():
    hints = get_type_hints(SubtaskRow, include_extras=False)
    for key in _V3_SUBTASK_COLUMNS:
        assert key in hints, f"SubtaskRow missing v3 key: {key}"


def test_review_thread_row_typed_dict_shape():
    hints = get_type_hints(ReviewThreadRow, include_extras=False)
    assert set(hints.keys()) >= _REVIEW_THREADS_COLUMNS
    # smoke: instantiate a literal dict that conforms
    row: ReviewThreadRow = {
        "task_id": "R-1",
        "thread_id": "RT_kwx",
        "is_resolved": 0,
        "last_comment_ts": 1.0,
        "first_seen_ts": 0.5,
    }
    assert row["task_id"] == "R-1"


def test_progress_check_row_typed_dict_shape():
    hints = get_type_hints(ProgressCheckRow, include_extras=False)
    assert set(hints.keys()) >= _PROGRESS_CHECKS_COLUMNS
    row: ProgressCheckRow = {
        "task_id": "R-1",
        "subtask_id": "S-01",
        "ts": 1.0,
        "attempts_at_check": 4,
        "verdict": "progressing",
    }
    assert row["verdict"] == "progressing"


# ----- Config defaults -----


def test_config_v3_defaults():
    """Phase A retry knobs + review knobs land with the documented defaults."""
    cfg = Config(repo_path="/tmp/x", dag_path="/tmp/x/dag.json")  # type: ignore[arg-type]
    assert cfg.subtask_hard_max_attempts == 30
    assert cfg.subtask_progress_check_after == 4
    assert cfg.subtask_progress_check_every == 3
    assert cfg.subtask_flatline_block_count == 2
    assert cfg.subtask_transient_max_retries == 5
    assert cfg.pre_commit_runner == "auto"
    assert cfg.review_poll_interval_s == 60
    assert cfg.respond_to_bot_reviews is True


def test_config_pre_commit_runner_literal_validates():
    """The Literal["auto","lefthook","pre-commit","none"] field rejects bad
    values and accepts each documented option."""
    for v in ("auto", "lefthook", "pre-commit", "none"):
        cfg = Config(repo_path="/tmp/x", dag_path="/tmp/x/dag.json", pre_commit_runner=v)  # type: ignore[arg-type]
        assert cfg.pre_commit_runner == v
    with pytest.raises(ValidationError):
        Config(
            repo_path="/tmp/x",
            dag_path="/tmp/x/dag.json",
            pre_commit_runner="bogus",  # type: ignore[arg-type]
        )
