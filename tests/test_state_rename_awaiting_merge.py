"""v3 Phase B migration: AWAITING_HUMAN → AWAITING_MERGE.

Pre-v3 workspaces hold rows with `state='awaiting_human'`. The State enum
no longer has that value; the rows must be renamed during `Store._migrate()`
so the existing data comes back valid against the new enum.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from quikode.state import State, Store


def test_state_enum_no_awaiting_human():
    """The old enum value should be gone entirely."""
    assert not hasattr(State, "AWAITING_HUMAN")
    assert State.AWAITING_MERGE.value == "awaiting_merge"


def _seed_pre_v3_db(db_path: Path) -> None:
    """Build a tiny SQLite DB containing a row at state='awaiting_human' so
    the next Store(...) call has something to migrate."""
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
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
    now = time.time()
    conn.execute(
        "INSERT INTO tasks (id, state, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("R-0001", "awaiting_human", now, now),
    )
    conn.execute(
        "INSERT INTO tasks (id, state, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("R-0002", "doing", now, now),
    )
    conn.execute(
        "INSERT INTO state_log (task_id, from_state, to_state, ts) VALUES (?, ?, ?, ?)",
        ("R-0001", "polling_ci", "awaiting_human", now),
    )
    conn.execute(
        "INSERT INTO state_log (task_id, from_state, to_state, ts) VALUES (?, ?, ?, ?)",
        ("R-0003", "awaiting_human", "merged", now),
    )
    conn.close()


def test_migrate_renames_awaiting_human_to_awaiting_merge(tmp_path: Path):
    db = tmp_path / "q.db"
    _seed_pre_v3_db(db)

    store = Store(db)

    rows = {r["id"]: r["state"] for r in store.all_tasks()}
    assert rows["R-0001"] == "awaiting_merge"
    assert rows["R-0002"] == "doing"
    # No row should still hold the old value.
    assert "awaiting_human" not in rows.values()

    # state_log entries also rewritten so audit-trail pretty-printing doesn't
    # show stale state names.
    log_to = {r["to_state"] for r in store.conn.execute("SELECT to_state FROM state_log").fetchall()}
    log_from = {
        r["from_state"]
        for r in store.conn.execute("SELECT from_state FROM state_log").fetchall()
        if r["from_state"]
    }
    assert "awaiting_human" not in log_to
    assert "awaiting_human" not in log_from
    assert "awaiting_merge" in log_to
    assert "awaiting_merge" in log_from


def test_migrate_idempotent(tmp_path: Path):
    """Running migration twice on the same DB should be a no-op the second time."""
    db = tmp_path / "q.db"
    _seed_pre_v3_db(db)
    Store(db)
    # Second open hits a fully-migrated DB; should not error or double-rename.
    store2 = Store(db)
    rows = {r["id"]: r["state"] for r in store2.all_tasks()}
    assert rows["R-0001"] == "awaiting_merge"
    assert rows["R-0002"] == "doing"


def test_migrate_no_op_on_clean_db(tmp_path: Path):
    """Fresh DB with no awaiting_human rows: migration runs fine, leaves DB clean."""
    db = tmp_path / "q.db"
    store = Store(db)
    store.upsert_pending("T-001")
    store.transition("T-001", State.AWAITING_MERGE)
    rows = {r["id"]: r["state"] for r in store.all_tasks()}
    assert rows["T-001"] == "awaiting_merge"

    # Reopen — migration runs again; T-001 stays put.
    store2 = Store(db)
    rows2 = {r["id"]: r["state"] for r in store2.all_tasks()}
    assert rows2["T-001"] == "awaiting_merge"
