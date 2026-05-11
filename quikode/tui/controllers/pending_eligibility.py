"""Scheduler-parity eligibility predicate for TUI's `pending` count.

The TUI header surfaces "pending N" — tasks the scheduler would
actually pick up RIGHT NOW if a slot opened.

Plan 59 fix C: this module is no longer an approximation. It calls
`scheduler.collect_pick_candidates` directly — the same function the
orchestrator's `_pick_next` uses — feeding it the standalone
`stacking_helpers` (`stack_depth` / `stack_root` /
`stack_size_under_root` / `would_form_cycle`). The result is then
filtered through `prefer_primary_candidates` so primary-tier
candidates eclipse stacked ones (matching plan 30 tiering). The TUI
count is by definition exact: the number of candidates the scheduler
would consider on this tick.

The TUI holds a read-only SQLite connection rather than a full Store,
so this module exposes a small `_ReadOnlyStoreAdapter` that mirrors
the exact Store methods `collect_pick_candidates` + the stacking
helpers consume. No write paths; no schema migrations.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from quikode.config import Config
from quikode.dag import DAG
from quikode.orchestration import scheduler, stacking_helpers
from quikode.state import State


class _ReadOnlyStoreAdapter:
    """Read-only duck-typed Store for `collect_pick_candidates`.

    Exposes the exact subset of Store methods the scheduler +
    stacking_helpers consume:
      - `completed_ids` / `active_ids` / `get` / `all_tasks`
      - `most_recent_awaiting_review_entry_ts`
      - `subtask_progress`
      - `get_parent_task_ids`

    The TUI uses a read-only sqlite3 connection so the daemon's
    writes don't deadlock against the poller. This adapter satisfies
    `scheduler.collect_pick_candidates`'s `store: Store` parameter
    without instantiating a real Store (which would try to apply
    schema migrations against a `?mode=ro` URI).
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def completed_ids(self) -> set[str]:
        return {
            r["id"]
            for r in self._conn.execute(
                "SELECT id FROM tasks WHERE state = ?", (State.MERGED.value,)
            ).fetchall()
        }

    def active_ids(self) -> set[str]:
        # Same predicate as `Store.active_ids` — every state that isn't
        # PENDING / MERGED / BLOCKED / FAILED / ABORTED / PENDING_CI.
        rows = self._conn.execute(
            "SELECT id FROM tasks WHERE state NOT IN (?, ?, ?, ?, ?, ?)",
            (
                State.PENDING.value,
                State.MERGED.value,
                State.BLOCKED.value,
                State.FAILED.value,
                State.ABORTED.value,
                State.PENDING_CI.value,
            ),
        ).fetchall()
        return {r["id"] for r in rows}

    def get(self, task_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def all_tasks(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()]

    def most_recent_awaiting_review_entry_ts(self, task_id: str) -> float | None:
        row = self._conn.execute(
            "SELECT MAX(ts) AS t FROM state_log WHERE task_id = ? AND to_state = ?",
            (task_id, State.AWAITING_REVIEW.value),
        ).fetchone()
        return float(row["t"]) if row and row["t"] is not None else None

    def subtask_progress(self, task_id: str) -> tuple[int, int]:
        row = self._conn.execute(
            "SELECT "
            "  SUM(CASE WHEN state='done' THEN 1 ELSE 0 END) AS done, "
            "  COUNT(*) AS total "
            "FROM subtasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return (0, 0)
        return (int(row["done"] or 0), int(row["total"] or 0))

    def get_parent_task_ids(self, task_id: str) -> list[str]:
        row = self._conn.execute("SELECT parent_task_ids FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or row["parent_task_ids"] is None:
            return []
        try:
            data = json.loads(row["parent_task_ids"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [str(x) for x in data]


def count_pending_eligible(
    *,
    c: sqlite3.Connection,
    cfg: Config,
    dag: DAG,
    rows: list[sqlite3.Row],
) -> int:
    """Plan 59 fix C: exact scheduler-parity pending count.

    Builds a read-only Store adapter over `c`, then invokes
    `scheduler.collect_pick_candidates` with the standalone
    `stacking_helpers` functions bound to the adapter. The resulting
    list is filtered through `prefer_primary_candidates` (plan 30
    tiering) before counting — the same pipeline the orchestrator's
    `_pick_next` runs. `rows` is unused here (kept on the signature
    to avoid churn at the call site); the adapter reads directly
    from `c` so the count always reflects the latest committed state.
    """
    _ = rows  # adapter reads directly from `c`; rows kept for signature stability
    store = _ReadOnlyStoreAdapter(c)
    scope = set(dag.nodes.keys())
    # Bind the stacking helpers to (store, dag) so the scheduler can
    # call them as the plain `fn(task_id)` shape it expects.
    candidates = scheduler.collect_pick_candidates(
        cfg=cfg,
        dag=dag,
        store=store,
        scope=scope,
        in_flight=set(),
        stack_depth_fn=lambda tid: stacking_helpers.stack_depth(
            store, dag, tid, max_depth_sentinel=cfg.stacking_max_depth
        ),
        stack_root_fn=lambda tid: stacking_helpers.stack_root(store, dag, tid),
        stack_size_under_root_fn=lambda root: stacking_helpers.stack_size_under_root(store, dag, root),
        would_form_cycle_fn=lambda child, parent: stacking_helpers.would_form_cycle(
            store, dag, child, parent
        ),
    )
    return len(scheduler.prefer_primary_candidates(candidates))
