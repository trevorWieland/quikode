"""Schema migration: older workspaces must auto-add v2 columns on Store init.

`CREATE TABLE IF NOT EXISTS` is a no-op on existing tables, so columns
added in v2 (needs_intent_review, parent_task_id, etc.) never land via
the SCHEMA executescript alone. Store._migrate() ALTERs them in.

Regression for the `OperationalError: no such column: needs_intent_review`
that surfaced when the TUI poller queried a pre-v2 tanren workspace.
"""

from __future__ import annotations

import sqlite3

from quikode.config import DEFAULT_CONFIG_TOML
from quikode.state import State, Store
from quikode.tui.controllers.store_polls import StorePoller

# All v2 columns that must be present after migration.
_V2_TASK_COLUMNS = {
    "base_ref_sha",
    "last_synced_main_sha",
    "conflict_resolve_retries",
    "needs_intent_review",
    "last_intent_review_ts",
    "intent_review_count",
    "replan_count",
    # parent_* columns are JSON arrays only (scalar variants dropped in
    # the legacy purge). The on-disk column names are the plural forms.
    "parent_task_ids",
    "parent_branches",
    "parent_pr_branches",
}
_V2_AGENT_CALLS_COLUMNS = {"subtask_id"}


def _create_v01_schema(db_path) -> None:
    """Build a SQLite DB with the v0.1 (pre-v2) schema."""
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
            ts REAL NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO tasks (id, state, branch, do_check_retries, "
        "ci_triage_retries, review_triage_retries, created_at, updated_at) "
        "VALUES ('R-EXISTING', 'merged', 'old-branch', 1, 2, 0, 100.0, 200.0)"
    )
    conn.close()


def _table_columns(db_path, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    conn.close()
    return {r[1] for r in rows}


def test_migration_adds_v2_task_columns(tmp_path):
    db = tmp_path / "old.db"
    _create_v01_schema(db)
    cols_before = _table_columns(db, "tasks")
    assert _V2_TASK_COLUMNS - cols_before == _V2_TASK_COLUMNS  # all missing pre-migrate

    Store(db).conn.close()

    cols_after = _table_columns(db, "tasks")
    missing = _V2_TASK_COLUMNS - cols_after
    assert not missing, f"migration didn't add: {missing}"


def test_migration_adds_subtask_id_to_agent_calls(tmp_path):
    db = tmp_path / "old.db"
    _create_v01_schema(db)
    assert "subtask_id" not in _table_columns(db, "agent_calls")

    Store(db).conn.close()

    assert "subtask_id" in _table_columns(db, "agent_calls")


def test_migration_preserves_existing_rows(tmp_path):
    db = tmp_path / "old.db"
    _create_v01_schema(db)
    s = Store(db)
    row = s.get("R-EXISTING")
    assert row is not None
    assert row["state"] == State.MERGED.value
    assert row["branch"] == "old-branch"
    assert row["do_check_retries"] == 1
    # New columns default to None (TEXT/REAL) or 0 (INTEGER DEFAULT 0)
    assert row["needs_intent_review"] in (0, None)
    assert row["parent_task_ids"] is None
    s.conn.close()


def test_migration_idempotent(tmp_path):
    db = tmp_path / "old.db"
    _create_v01_schema(db)
    Store(db).conn.close()
    cols_after_first = _table_columns(db, "tasks")
    Store(db).conn.close()
    cols_after_second = _table_columns(db, "tasks")
    assert cols_after_first == cols_after_second


def test_fresh_db_has_all_columns(tmp_path):
    """The full SCHEMA still produces a complete table on fresh init —
    migration is a no-op for new workspaces."""
    db = tmp_path / "fresh.db"
    Store(db).conn.close()
    cols = _table_columns(db, "tasks")
    missing = _V2_TASK_COLUMNS - cols
    assert not missing


def test_tui_poller_query_works_post_migration(tmp_path):
    """The exact query that crashed in the user's workspace must succeed
    after migration. Regression for the OperationalError trace."""
    qkdir = tmp_path / ".quikode"
    qkdir.mkdir()
    (qkdir / "config.toml").write_text(
        DEFAULT_CONFIG_TOML.format(repo_path=str(tmp_path), dag_path=str(tmp_path / "dag.json"))
    )
    db = qkdir / "quikode.db"
    _create_v01_schema(db)
    Store(db).conn.close()  # migrate

    poller = StorePoller(workspace=tmp_path)
    snap = poller.poll()
    # No crash. The R-EXISTING row is MERGED, which the panel filters out;
    # but it counts in the header and that's what we care about post-migration.
    assert snap.error is None
    assert snap.header.merged == 1
