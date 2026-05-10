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

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from quikode.state_schema import SCHEMA, apply_migrations
from quikode.state_types import (
    ACTIVE,
    POST_PR_STATES,
    TERMINAL,
    ContainerStatsRow,
    ProgressCheckRow,
    ReviewThreadRow,
    State,
    SubtaskRow,
    SubtaskState,
    TaskRow,
)
from quikode.store_forensics import StoreForensicsMixin
from quikode.store_planning_cycle import StorePlanningCycleMixin
from quikode.store_review import StoreReviewMixin
from quikode.store_subtasks import StoreSubtaskMixin
from quikode.store_tasks import StoreTaskMixin

log = logging.getLogger("quikode.state")

__all__ = [
    "ACTIVE",
    "POST_PR_STATES",
    "TERMINAL",
    "ContainerStatsRow",
    "ProgressCheckRow",
    "ReviewThreadRow",
    "State",
    "Store",
    "SubtaskRow",
    "SubtaskState",
    "TaskRow",
]


class Store(
    StoreTaskMixin,
    StoreSubtaskMixin,
    StorePlanningCycleMixin,
    StoreForensicsMixin,
    StoreReviewMixin,
):
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
            apply_migrations(self.conn)
        self.validate_runtime_states()

    def validate_runtime_states(self) -> None:
        """Reject task rows whose state is outside the canonical FSM."""

        with self._tx_lock:
            rows = self.conn.execute("SELECT id, state FROM tasks").fetchall()
        allowed = {s.value for s in State}
        invalid = [(r["id"], r["state"]) for r in rows if r["state"] not in allowed]
        if invalid:
            detail = ", ".join(f"{task_id}={state}" for task_id, state in invalid[:10])
            raise ValueError(f"workspace has invalid task state(s): {detail}")

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

    def close(self) -> None:
        with self._tx_lock:
            self.conn.close()

    # ----- task lifecycle -----

    # ----- v2 subtasks -----

    # ----- v3 Phase B: review-thread polling + response cycles -----

    # ----- v3 Phase C: stacked diffs / parent-merge rebase plumbing -----
