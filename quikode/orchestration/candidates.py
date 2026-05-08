"""Plan 32: candidate-collection mixin for the Orchestrator.

Splits the eligibility logic out of `runner.py` so the production module
stays under the 600-line architecture-guard budget. The mixin owns:

  - `_collect_pick_candidates`: enumerate every pickable candidate
    (DAG-resident spec tasks + runtime-created merge-nodes).
  - `_candidate_for_node`: per-DAG-node eligibility, with multi-parent →
    merge-node reduction.
  - `_resolve_multi_parent_merge_node`: lookup-or-create merge-nodes for
    multi-parent children, gate on MERGE_NODE_READY.
  - `_pending_merge_node_rows`: enumerate runtime-created merge-nodes
    needing scheduling.

The mixin reads `self.cfg`, `self.dag`, `self.store`, `self._stack_depth`,
`self._stack_root`, `self._stack_size_under_root`, `self._would_form_cycle`
— all owned by the Orchestrator.
"""

from __future__ import annotations

import logging
from typing import Any

from quikode import merge_node
from quikode.orchestration import scheduler
from quikode.state import State, TaskRow

log = logging.getLogger("quikode.orchestrator.candidates")


class CandidatesMixin:
    def _collect_pick_candidates(self: Any, scope: set[str], in_flight: set[str]) -> list[dict]:
        """Enumerate all tasks currently eligible for scheduling. No side
        effects. Each candidate carries the metadata `_apply_pick_side_effects`
        and `_score_candidate` need so they can act without re-deriving.

        Stacking eligibility honors `cfg.stacking_readiness` via
        `scheduler.is_parent_stack_ready`. Resume signals (has_open_pr,
        subtask done/total) are pulled per candidate via
        `scheduler._resume_signals` and consumed by `_score_candidate`.

        Plan 32: multi-parent (`len(unmet) > 1`) children resolve via a
        merge-node. When all source parents are stack-ready we look up
        (or create) the merge-node; the child becomes eligible only when
        the merge-node is `MERGE_NODE_READY`, with `unmet=[mn_id]`. The
        merge-node itself appears as a primary candidate while in PENDING.
        """
        completed = self.store.completed_ids() & scope
        active = self.store.active_ids() & scope
        candidates: list[dict] = []
        for nid in sorted(scope):
            if nid in completed or nid in active or nid in in_flight:
                continue
            n = self.dag.nodes.get(nid)
            if n is None:
                continue
            row = self.store.get(nid)
            if row and row["state"] not in (State.PENDING.value,):
                continue
            cand = self._candidate_for_node(nid, n, row, completed)
            if cand is not None:
                candidates.append(cand)
        for mn_row in self._pending_merge_node_rows(in_flight):
            mn_id = str(mn_row["id"])
            candidates.append(
                {
                    "task_id": mn_id,
                    "is_stacked": False,
                    "unmet": [],
                    "row": mn_row,
                    "has_open_pr": False,
                    "subtask_done": 0,
                    "subtask_total": 0,
                }
            )
        return candidates

    def _candidate_for_node(
        self: Any,
        nid: str,
        node: Any,
        row: TaskRow | None,
        completed: set[str],
    ) -> dict | None:
        """Build the candidate dict for one DAG node, or None if not eligible.

        Plan 32: multi-parent (`len(unmet) > 1`) reduces via merge-node;
        single-parent passes through the existing stacking gates.
        """
        in_scope_deps = [d for d in node.depends_on if d in self.dag.nodes]
        unmet = [d for d in in_scope_deps if d not in completed]
        has_open_pr, sub_done, sub_total = scheduler._resume_signals(row, self.store)
        base_meta = {
            "row": row,
            "has_open_pr": has_open_pr,
            "subtask_done": sub_done,
            "subtask_total": sub_total,
        }
        if not unmet:
            return {"task_id": nid, "is_stacked": False, "unmet": [], **base_meta}
        if not self._stacking_eligible(node, unmet):
            return None
        if len(unmet) > 1:
            mn_unmet = self._resolve_multi_parent_merge_node(nid, unmet)
            if mn_unmet is None:
                return None
            return {"task_id": nid, "is_stacked": True, "unmet": mn_unmet, **base_meta}
        return self._single_parent_candidate(nid, unmet, base_meta)

    def _stacking_eligible(self: Any, node: Any, unmet: list[str]) -> bool:
        """Check the per-config stacking gates: strategy off, parent
        stack-readiness, and within-milestone scoping."""
        if self.cfg.stacking_strategy == "off":
            return False
        unmet_states = {d: (self.store.get(d) or {}).get("state") for d in unmet}
        if not all(
            scheduler.is_parent_stack_ready(cfg=self.cfg, parent_state=s, parent_id=d, store=self.store)
            for d, s in unmet_states.items()
        ):
            return False
        return not (
            self.cfg.stacking_strategy == "within-milestone"
            and not all(self.dag.nodes[d].milestone == node.milestone for d in unmet)
        )

    def _single_parent_candidate(self: Any, nid: str, unmet: list[str], base_meta: dict) -> dict | None:
        """Apply depth/cycle/breadth gates for the single-parent stacking case."""
        depth = self._stack_depth(unmet[0])
        if depth >= self.cfg.stacking_max_depth:
            return None
        if self._would_form_cycle(nid, unmet[0]):
            log.warning(
                "refusing to stack %s on %s — would form parent_task_id cycle",
                nid,
                unmet[0],
            )
            return None
        root = self._stack_root(unmet[0])
        if self._stack_size_under_root(root) >= self.cfg.stacking_max_breadth_per_root:
            log.warning(
                "task %s would exceed stacking_max_breadth_per_root (%d) under root %s",
                nid,
                self.cfg.stacking_max_breadth_per_root,
                root,
            )
            return None
        return {"task_id": nid, "is_stacked": True, "unmet": unmet, **base_meta}

    def _resolve_multi_parent_merge_node(
        self: Any, child_id: str, unmet_source_parents: list[str]
    ) -> list[str] | None:
        """Plan 32: look up or create the merge-node for a multi-parent child.

        Returns:
          - `[merge_node_id]` when the merge-node is MERGE_NODE_READY (child
            eligible to schedule with the merge-node as its single effective
            parent).
          - `None` when the merge-node isn't ready yet (child must wait;
            merge-node itself will be picked up via the PENDING enumeration).
        """
        sorted_ids = sorted(unmet_source_parents)
        parent_branches: list[str] = []
        for pid in sorted_ids:
            pr = self.store.get(pid) or {}
            br = pr.get("branch")
            if not br:
                return None
            parent_branches.append(str(br))
        mn_id = merge_node.lookup_or_create_merge_node(self.store, sorted_ids, parent_branches)
        mn_row = self.store.get(mn_id)
        if mn_row is None:
            log.warning(
                "merge-node %s for child %s missing from store immediately after create",
                mn_id,
                child_id,
            )
            return None
        if str(mn_row.get("state") or "") != State.MERGE_NODE_READY.value:
            return None
        return [mn_id]

    def _pending_merge_node_rows(self: Any, in_flight: set[str]) -> list[dict]:
        """Return all `kind="merge"` rows currently in PENDING (modulo in_flight).

        Plan 32: merge-nodes don't live in the DAG, so the regular
        scope-driven enumeration skips them. Pull them directly from the
        store. Limited to PENDING — the worker drives them through the
        rest of the lifecycle on its own.
        """
        with self.store._tx_lock:
            rows = self.store.conn.execute(
                "SELECT * FROM tasks WHERE kind = 'merge' AND state = ? ORDER BY id",
                (State.PENDING.value,),
            ).fetchall()
        return [dict(r) for r in rows if str(r["id"]) not in in_flight]
