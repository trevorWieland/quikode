"""Plan 59 fix C: standalone stacking-depth / cycle / breadth helpers.

These were previously methods on `Orchestrator` (`_stack_depth`,
`_stack_root`, `_stack_size_under_root`, `_would_form_cycle`) and
unreachable from the TUI's pending-eligibility controller. Moving them
into a standalone module lets both the orchestrator and the TUI feed
the same helpers into `scheduler.collect_pick_candidates`, so the
"pending N" header count reflects the exact set of candidates the
scheduler would actually pick up.

Every helper takes `(store, dag, ...)` plus optional `cfg` instead of
`self`; no Orchestrator instance is needed. The orchestrator wires its
methods through to these via thin shims so existing call sites keep
working without churn. The `store` arg is typed as a `StoreLike`
protocol so the TUI's read-only `_ReadOnlyStoreAdapter` can plug in
without subclassing `Store`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from quikode.dag import DAG


@runtime_checkable
class StoreLike(Protocol):
    """Minimal duck-typed Store interface the stacking helpers need.

    `Store` (production) and `_ReadOnlyStoreAdapter` (TUI read-only) both
    satisfy this; using a Protocol keeps `quikode/state.py` and the TUI
    adapter independent without forcing one to import the other.
    """

    def get_parent_task_ids(self, task_id: str) -> list[str]: ...
    def all_tasks(self) -> list[Any]: ...


log = logging.getLogger("quikode.orchestration.stacking_helpers")


def parents_of(store: StoreLike, task_id: str) -> list[str]:
    """Return the multi-parent list for `task_id` — single source of
    truth for the stack-walk helpers (replaces `Orchestrator._parents_of`).
    """
    return store.get_parent_task_ids(task_id)


def stack_depth(store: StoreLike, dag: DAG, task_id: str, *, max_depth_sentinel: int) -> int:
    """Compute how deep the stacking DAG is starting from `task_id`.

    Mirrors the prior `Orchestrator._stack_depth` semantics exactly:
    walks `parent_task_ids` upward, taking the maximum depth across all
    paths. Depth is INCLUSIVE of the starting task (root → 1; one
    parent → 2). On cycle detection returns a sentinel above
    `max_depth_sentinel` so the caller's `depth >= cfg.stacking_max_depth`
    check rejects.
    """
    _ = dag  # accepted for symmetry / future use; current implementation reads only `store`
    on_stack: set[str] = set()
    cycle_detected = [False]

    def _depth(node: str) -> int:
        if node in on_stack:
            cycle_detected[0] = True
            return 0
        on_stack.add(node)
        try:
            parents = parents_of(store, node)
            if not parents:
                return 1
            return 1 + max(_depth(p) for p in parents)
        finally:
            on_stack.discard(node)

    depth = _depth(task_id)
    if cycle_detected[0]:
        log.warning("stacking cycle detected from %s — refusing further stacking", task_id)
        return max(depth, max_depth_sentinel + 1)
    return depth


def stack_root(store: StoreLike, dag: DAG, task_id: str) -> str:
    """Find a stacking root for `task_id` — the topmost non-stacked
    ancestor. With multi-parent stacking the DAG can have multiple
    roots; return the lexicographically lowest one for determinism
    (matches `Orchestrator._stack_root`). Cycle-safe via visited set.
    """
    _ = dag  # accepted for symmetry; current implementation reads only `store`
    seen: set[str] = set()
    frontier = [task_id]
    roots: list[str] = []
    while frontier:
        cur = frontier.pop()
        if cur in seen:
            continue
        seen.add(cur)
        parents = parents_of(store, cur)
        if not parents:
            roots.append(cur)
            continue
        for p in parents:
            if p not in seen:
                frontier.append(p)
    if not roots:
        return task_id
    return min(roots)


def stack_size_under_root(store: StoreLike, dag: DAG, root_task_id: str) -> int:
    """How many tasks (across the whole tree) are stacked off this root,
    including the root itself? Used for the breadth-cap check (matches
    `Orchestrator._stack_size_under_root`). Defensive linear scan —
    workspaces have <300 tasks total in practice.
    """
    count = 0
    for r in store.all_tasks():
        if stack_root(store, dag, str(r["id"])) == root_task_id:
            count += 1
    return count


def would_form_cycle(store: StoreLike, dag: DAG, child_id: str, prospective_parent_id: str) -> bool:
    """Multi-parent cycle detection. Walking the parent DAG from
    `prospective_parent_id` upward, would we re-encounter `child_id`?
    BFS over `parent_task_ids` (matches `Orchestrator._would_form_cycle`).
    """
    _ = dag  # accepted for symmetry; current implementation reads only `store`
    if child_id == prospective_parent_id:
        return True
    seen: set[str] = set()
    frontier = [prospective_parent_id]
    while frontier:
        cur = frontier.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if cur == child_id:
            return True
        for p in parents_of(store, cur):
            if p not in seen:
                frontier.append(p)
    return False


__all__ = [
    "parents_of",
    "stack_depth",
    "stack_root",
    "stack_size_under_root",
    "would_form_cycle",
]
