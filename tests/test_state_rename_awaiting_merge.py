"""Legacy state-name migrations applied at Store init.

Three rewrites apply on every open (idempotent):

  * `awaiting_human`        → `pending_ci`  (pre-v3, AWAITING_HUMAN was renamed
                                              to AWAITING_MERGE then later split)
  * `awaiting_merge`        → `pending_ci`  (v3.5 split: legacy AWAITING_MERGE
                                              defaults to PENDING_CI; daemon's
                                              poll re-classifies on next tick)
  * `responding_to_review`  → `addressing_feedback`  (v3.5 semantic rename of
                                                      the post-PR fixup state)

Pre-v3 / pre-v3.5 workspaces holding rows at any of those values get rewritten
on the next open, so existing data comes back valid against the new enum.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from quikode.state import State, Store


def test_state_enum_no_legacy_values():
    """The retired enum entries should be gone entirely."""
    assert not hasattr(State, "AWAITING_HUMAN")
    assert not hasattr(State, "AWAITING_MERGE")
    assert not hasattr(State, "RESPONDING_TO_REVIEW")
    # New post-PR + feedback states exist
    assert State.PENDING_CI.value == "pending_ci"
    assert State.AWAITING_REVIEW.value == "awaiting_review"
    assert State.MERGE_READY.value == "merge_ready"
    assert State.TRIAGING_FEEDBACK.value == "triaging_feedback"
    assert State.ADDRESSING_FEEDBACK.value == "addressing_feedback"


def _seed_legacy_db(db_path: Path) -> None:
    """Build a tiny SQLite DB containing rows at retired state values so
    the next Store(...) call has something to migrate."""
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            pre_rebase_state TEXT
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
    seed_rows = [
        ("R-0001", "awaiting_human", "polling_ci"),
        ("R-0002", "awaiting_merge", "pr_opening"),
        ("R-0003", "responding_to_review", "awaiting_merge"),
        ("R-0004", "doing", None),  # untouched
    ]
    for tid, state, pre_rebase in seed_rows:
        conn.execute(
            "INSERT INTO tasks (id, state, pre_rebase_state, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (tid, state, pre_rebase, now, now),
        )
    conn.execute(
        "INSERT INTO state_log (task_id, from_state, to_state, ts) VALUES (?, ?, ?, ?)",
        ("R-0001", "polling_ci", "awaiting_human", now),
    )
    conn.execute(
        "INSERT INTO state_log (task_id, from_state, to_state, ts) VALUES (?, ?, ?, ?)",
        ("R-0002", "awaiting_merge", "responding_to_review", now),
    )
    conn.execute(
        "INSERT INTO state_log (task_id, from_state, to_state, ts) VALUES (?, ?, ?, ?)",
        ("R-0003", "responding_to_review", "awaiting_merge", now),
    )
    conn.close()


def test_migrate_rewrites_legacy_state_values(tmp_path: Path):
    db = tmp_path / "q.db"
    _seed_legacy_db(db)

    store = Store(db)

    rows = {r["id"]: r["state"] for r in store.all_tasks()}
    assert rows["R-0001"] == "pending_ci"  # awaiting_human → pending_ci
    assert rows["R-0002"] == "pending_ci"  # awaiting_merge → pending_ci
    assert rows["R-0003"] == "addressing_feedback"  # responding_to_review → addressing_feedback
    assert rows["R-0004"] == "doing"  # untouched

    # No retired value should remain in tasks.state.
    assert "awaiting_human" not in rows.values()
    assert "awaiting_merge" not in rows.values()
    assert "responding_to_review" not in rows.values()

    # state_log audit trail is also rewritten so historical reads don't surface
    # stale state names.
    log_states = set()
    for r in store.conn.execute("SELECT from_state, to_state FROM state_log").fetchall():
        if r["from_state"]:
            log_states.add(r["from_state"])
        log_states.add(r["to_state"])
    assert "awaiting_human" not in log_states
    assert "awaiting_merge" not in log_states
    assert "responding_to_review" not in log_states
    assert "pending_ci" in log_states
    assert "addressing_feedback" in log_states

    # pre_rebase_state is also migrated.
    pre_rebase = {r["id"]: r.get("pre_rebase_state") for r in store.all_tasks()}
    assert pre_rebase["R-0002"] == "pr_opening"  # never legacy → unchanged
    assert pre_rebase["R-0003"] == "pending_ci"  # awaiting_merge → pending_ci


def test_migrate_idempotent(tmp_path: Path):
    """Running migration twice on the same DB should be a no-op the second time."""
    db = tmp_path / "q.db"
    _seed_legacy_db(db)
    Store(db)
    # Second open hits a fully-migrated DB; should not error or double-rewrite.
    store2 = Store(db)
    rows = {r["id"]: r["state"] for r in store2.all_tasks()}
    assert rows["R-0001"] == "pending_ci"
    assert rows["R-0002"] == "pending_ci"
    assert rows["R-0003"] == "addressing_feedback"


def test_migrate_no_op_on_clean_db(tmp_path: Path):
    """Fresh DB with no legacy rows: migration runs fine, leaves DB clean."""
    db = tmp_path / "q.db"
    store = Store(db)
    store.upsert_pending("T-001")
    store.transition("T-001", State.PENDING_CI)
    rows = {r["id"]: r["state"] for r in store.all_tasks()}
    assert rows["T-001"] == "pending_ci"

    # Reopen — migration runs again; T-001 stays put.
    store2 = Store(db)
    rows2 = {r["id"]: r["state"] for r in store2.all_tasks()}
    assert rows2["T-001"] == "pending_ci"
