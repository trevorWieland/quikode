"""Plan 32: worker class factory.

Picks `MergeNodeWorker` for `kind="merge"` rows, `TaskWorker` for everything
else. Lives in its own module to avoid the import cycle between
`task_worker` (TaskWorker base class) and `merge_node_worker`
(MergeNodeWorker, which subclasses TaskWorker).
"""

from __future__ import annotations

from typing import Any

from quikode.config import Config
from quikode.dag import Node
from quikode.state import Store
from quikode.workers.merge_node_worker import MergeNodeWorker
from quikode.workers.task_worker import (
    TaskWorker,
    synthesize_node_for_runtime_task,
)


def build_task_worker(cfg: Config, dag: Any, store: Store, node: Node | str) -> TaskWorker:
    """Plan 32: factory that picks the worker class based on the task's `kind`.

    `kind="merge"` rows route to `MergeNodeWorker` (deterministic git
    integration + local-CI gate; PR-A 3/3 skips the audit gauntlet, PR-B
    will add it). Everything else gets the standard `TaskWorker`.

    Accepts either a DAG `Node` (for spec tasks present in the seed DAG)
    or a task id string (for merge-nodes — they have no DAG node since
    they're materialized at runtime). For id-string callers we synthesize
    a minimal Node-like object from the store row.
    """
    if isinstance(node, str):
        synthesized = synthesize_node_for_runtime_task(store, node)
        row = store.get(node)
        kind = (row or {}).get("kind") or "spec"
        if kind == "merge":
            return MergeNodeWorker(cfg, dag, store, synthesized)
        return TaskWorker(cfg, dag, store, synthesized)
    row = store.get(node.id)
    kind = (row or {}).get("kind") or "spec"
    if kind == "merge":
        return MergeNodeWorker(cfg, dag, store, node)
    return TaskWorker(cfg, dag, store, node)
