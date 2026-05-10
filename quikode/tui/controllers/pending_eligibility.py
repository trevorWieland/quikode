"""Scheduler-mirroring eligibility predicate for TUI's `pending` count.

The TUI header surfaces "pending N" — tasks the scheduler would actually
pick up RIGHT NOW if a slot opened. This module mirrors
`is_parent_stack_ready` semantics from `quikode.orchestration.scheduler`
directly (including the `stacking_readiness="settled"` parent-settle-time
threshold via a state_log query) so the displayed count reflects current
scheduler reality, not an approximation.

Skips the stack depth / breadth / cycle bounds (rarely-hit secondary
gates in `collect_pick_candidates`); the primary "deps ready" predicate
is the operator signal here.
"""

from __future__ import annotations

import sqlite3
import time

from quikode.config import Config
from quikode.dag import DAG
from quikode.orchestration.scheduler import STACK_READY_STATES
from quikode.state import State


def count_pending_eligible(
    *,
    c: sqlite3.Connection,
    cfg: Config,
    dag: DAG,
    rows: list[sqlite3.Row],
) -> int:
    """Return the count of pending tasks whose deps are all scheduler-ready."""
    state_by_id = {r["id"]: r["state"] for r in rows}
    merged_ids = {nid for nid, s in state_by_id.items() if s == State.MERGED.value}
    stacking_strategy = cfg.stacking_strategy.value
    stacking_readiness = getattr(cfg, "stacking_readiness", "speculative")
    settle_threshold = int(getattr(cfg, "review_ready_settle_s", 0)) if stacking_readiness == "settled" else 0
    now = time.time()
    ready_cache: dict[str, bool] = {}

    def _settle_ts(pid: str) -> float | None:
        row = c.execute(
            "SELECT MAX(ts) AS t FROM state_log WHERE task_id = ? AND to_state = ?",
            (pid, State.AWAITING_REVIEW.value),
        ).fetchone()
        return float(row["t"]) if row and row["t"] is not None else None

    def _is_dep_ready(pid: str) -> bool:
        if pid in merged_ids:
            return True
        if stacking_strategy == "off":
            return False
        cached = ready_cache.get(pid)
        if cached is not None:
            return cached
        parent_state = state_by_id.get(pid)
        ready: bool
        if parent_state is None:
            ready = False
        elif stacking_readiness == "speculative":
            ready = parent_state in STACK_READY_STATES
        elif parent_state != State.AWAITING_REVIEW.value:
            ready = False
        elif settle_threshold <= 0:
            ready = True
        else:
            ts = _settle_ts(pid)
            ready = ts is not None and (now - ts) >= settle_threshold
        ready_cache[pid] = ready
        return ready

    count = 0
    for r in rows:
        if r["state"] != State.PENDING.value:
            continue
        node = dag.nodes.get(r["id"])
        if node is None:
            continue
        if all(_is_dep_ready(dep) for dep in node.depends_on):
            count += 1
    return count
