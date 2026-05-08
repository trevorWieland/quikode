"""Plan 32: merge-node first-class entity.

A merge-node is a synthetic task (`kind="merge"` in the `tasks` table) that
integrates N source spec parents into one stable branch. Multiple downstream
children depending on the same parent set share the merge-node — it
materializes once, gets audited, serves as their effective base. From
each child's perspective, multi-parent dependency reduces to single-parent
dependency on the merge-node.

Lifecycle:

  PENDING → PROVISIONING → PLANNING → DOING_SUBTASK (← merge-planner emits
  integration subtasks; for trivial deterministic merges this is one
  subtask running the conflict-resolver subloop) → CHECKING_SUBTASK →
  COMMITTING → PUSHING → LOCAL_CI_CHECKING → PRE_PR_AUDITING (gauntlet runs
  with merge_node_mode=True, gating local_ci + behavior; rubric/standards
  re-enabled when integration subtasks ran) → MERGE_NODE_READY (terminal-ish).

  On any source parent advancing: MERGE_NODE_READY → PENDING (re-merge cycle).
  On all source parents merging to main: MERGE_NODE_READY → MERGE_NODE_RETIRED.

Branch shape: `quikode/merge/<sorted-parent-ids>` (deterministic, force-pushed
on each update). Children's PR base = this branch; stays stable across
re-merges.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from quikode.fsm import Event, State

log = logging.getLogger("quikode.merge_node")


def compute_merge_node_id(parent_task_ids: list[str]) -> str:
    """Deterministic merge-node id from sorted source parent ids.

    Hash the sorted tuple → 8 hex chars → `M-<hex>`. Two tasks with the
    same parent set get the same merge-node, regardless of arrival order.
    """
    if not parent_task_ids:
        raise ValueError("compute_merge_node_id: empty parent_task_ids")
    sig = ",".join(sorted(parent_task_ids))
    h = hashlib.blake2b(sig.encode("utf-8"), digest_size=4).hexdigest()
    return f"M-{h}"


def merge_node_branch_name(merge_node_id: str) -> str:
    """Stable branch name. Force-pushed across re-merges so children's PR
    base remains valid."""
    return f"quikode/merge/{merge_node_id.lower()}"


def lookup_or_create_merge_node(
    store: Any,
    parent_task_ids: list[str],
    parent_branches: list[str],
) -> str:
    """Plan 32: idempotent lookup-or-create. Returns the merge-node id.

    First call materializes a `kind="merge"` row in PENDING with the given
    parent set. Subsequent calls with the same parents return the existing id.
    Used at scheduler / provisioning time when a multi-parent child needs
    to resolve its effective base.
    """
    mn_id = compute_merge_node_id(parent_task_ids)
    existing = store.get(mn_id)
    if existing is not None:
        return mn_id
    branch = merge_node_branch_name(mn_id)
    store.create_merge_node(
        merge_node_id=mn_id,
        parent_task_ids=parent_task_ids,
        parent_branches=parent_branches,
        branch=branch,
    )
    log.info(
        "created merge-node %s for parents=%s (branch=%s)",
        mn_id,
        parent_task_ids,
        branch,
    )
    return mn_id


def propagate_parent_advanced(store: Any, parent_task_id: str) -> list[str]:
    """When a source parent advances (push, not merge), every merge-node
    that lists it among its parents needs a re-merge cycle.

    Transitions affected merge-nodes from MERGE_NODE_READY → PENDING with
    a `parent_advanced` resume marker. Non-READY merge-nodes are left
    alone (they're either still being built or already PENDING from a
    prior advance).

    Returns the list of merge-node ids that were transitioned.
    """
    affected = store.merge_nodes_with_parent(parent_task_id)
    transitioned: list[str] = []
    for mn_row in affected:
        if mn_row.get("state") != State.MERGE_NODE_READY.value:
            continue
        mn_id = str(mn_row["id"])
        store.apply_event(
            mn_id,
            Event.PARENT_ADVANCED,
            note=f"source parent {parent_task_id} advanced; re-merge cycle",
            resume_from_existing_subtasks=1,
        )
        transitioned.append(mn_id)
    if transitioned:
        log.info(
            "parent %s advanced → %d merge-node(s) reset to PENDING for re-merge: %s",
            parent_task_id,
            len(transitioned),
            transitioned,
        )
    return transitioned


def propagate_parent_merged(store: Any, parent_task_id: str) -> list[str]:
    """When a source parent merges to main, drop it from every merge-node's
    parent set. If a merge-node's parent set becomes empty (all sources
    merged), retire it via ALL_PARENTS_MERGED — its branch is no longer
    needed, downstream children rebase onto main directly.

    Returns the list of (merge_node_id, action) pairs as a flat list of
    merge-node ids that were touched.
    """
    affected = store.merge_nodes_with_parent(parent_task_id)
    touched: list[str] = []
    for mn_row in affected:
        mn_id = str(mn_row["id"])
        remaining = store.prune_merge_node_parent(mn_id, parent_task_id)
        touched.append(mn_id)
        if not remaining:
            # All sources merged → retire the merge-node.
            current_state = mn_row.get("state")
            if current_state == State.MERGE_NODE_READY.value:
                store.apply_event(
                    mn_id,
                    Event.ALL_PARENTS_MERGED,
                    note=f"all source parents merged to main (last: {parent_task_id})",
                )
                log.info("merge-node %s retired (all parents merged)", mn_id)
            else:
                log.info(
                    "merge-node %s last source parent merged but state=%s; "
                    "leaving as-is, ALL_PARENTS_MERGED requires READY",
                    mn_id,
                    current_state,
                )
        # Still has un-merged parents → trigger a re-merge cycle so the
        # merge-node's branch reflects "main + remaining_parents".
        elif mn_row.get("state") == State.MERGE_NODE_READY.value:
            store.apply_event(
                mn_id,
                Event.PARENT_ADVANCED,
                note=(
                    f"source parent {parent_task_id} merged; re-merge "
                    f"against remaining {len(remaining)} parent(s)"
                ),
                resume_from_existing_subtasks=1,
            )
            log.info(
                "merge-node %s parent %s merged; re-merging against %d remaining parent(s)",
                mn_id,
                parent_task_id,
                len(remaining),
            )
    return touched


def is_merge_node(row: dict[str, Any]) -> bool:
    """Plan 32: True iff the row's kind is 'merge'."""
    return (row.get("kind") or "spec") == "merge"


def merge_node_age_seconds(store: Any, merge_node_id: str) -> float | None:
    """Plan 32: time since the merge-node entered MERGE_NODE_READY most
    recently. Used by `is_parent_stack_ready` for the settled-readiness
    gate (mirroring `most_recent_awaiting_review_entry_ts` for spec tasks).

    Returns None when the merge-node has never been READY.
    """
    with store._tx_lock:
        r = store.conn.execute(
            "SELECT MAX(ts) AS ts FROM state_log WHERE task_id = ? AND to_state = ?",
            (merge_node_id, State.MERGE_NODE_READY.value),
        ).fetchone()
    if r is None or r["ts"] is None:
        return None
    return time.time() - float(r["ts"])
