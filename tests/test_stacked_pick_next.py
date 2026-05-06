"""v3 Phase C stacked-diff scheduler: child tasks pick parent_pr_branch
when scheduled alongside an in-flight parent.

The orchestrator's `_pick_next` is the gate. When stacking is enabled and
a parent is in a stack-ready state (PENDING_CI, ADDRESSING_FEEDBACK,
PR_OPENING, PENDING_CI), the child becomes pickable AND has its
`parent_pr_branch` + `parent_branch` stamped before scheduling so the
worker's `_provision_worktree` branches off the parent.
"""

from __future__ import annotations

import json
from pathlib import Path

from quikode.config import Config, StackingStrategy
from quikode.dag import DAG
from quikode.orchestrator import Orchestrator
from quikode.state import State, Store


def _make_dag(tmp_path: Path, edges: list[tuple[str, list[str]]]) -> DAG:
    nodes = []
    for nid, deps in edges:
        nodes.append(
            {
                "id": nid,
                "kind": "behavior",
                "milestone": "M-1",
                "title": nid,
                "scope": "x",
                "depends_on": deps,
                "completes_behaviors": [],
                "supports_behaviors": [],
                "boundary_with_neighbors": "",
                "expected_evidence": [],
                "playbook": [],
                "rationale": "",
                "risks": [],
            }
        )
    p = tmp_path / "dag.json"
    p.write_text(
        json.dumps(
            {
                "schema": "test",
                "milestones": [{"id": "M-1", "title": "x", "goal": "x", "status": "planned"}],
                "nodes": nodes,
            }
        )
    )
    return DAG.load(p)


def _orch(tmp_path: Path, dag: DAG, **cfg_kw) -> Orchestrator:
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json", **cfg_kw)
    store = Store(tmp_path / "q.db")
    return Orchestrator(cfg, dag, store)


def test_pick_next_stamps_parent_pr_branch_when_parent_pending_ci(tmp_path):
    """A child whose dep is PENDING_CI → picked + parent_pr_branch
    stamped from the parent's branch."""
    dag = _make_dag(tmp_path, [("A", []), ("B", ["A"])])
    o = _orch(tmp_path, dag, stacking_strategy=StackingStrategy.WITHIN_MILESTONE)
    o.store.upsert_pending("A")
    o.store.upsert_pending("B")
    # Parent A is PENDING_CI with a branch.
    o.store.transition("A", State.PENDING_CI, branch="quikode/a-abc123")

    nxt = o._pick_next({"A", "B"}, set())
    assert nxt == "B"
    row_b = o.store.get("B")
    assert json.loads(row_b["parent_pr_branches"]) == ["quikode/a-abc123"]
    assert json.loads(row_b["parent_branches"]) == ["quikode/a-abc123"]
    o.store.conn.close()


def test_pick_next_clears_parent_branch_when_parent_merged(tmp_path):
    """A child whose dep is MERGED → picked WITHOUT parent_pr_branch
    (clears any prior stale value)."""
    dag = _make_dag(tmp_path, [("A", []), ("B", ["A"])])
    o = _orch(tmp_path, dag, stacking_strategy=StackingStrategy.WITHIN_MILESTONE)
    o.store.upsert_pending("A")
    o.store.upsert_pending("B")
    # Pre-stamp B with stale stacking metadata (e.g., from a prior tick
    # where A was AWAITING_REVIEW). The merge should clear it.
    o.store.set_parent_chain(
        "B",
        parent_task_ids=["A"],
        parent_branches=["quikode/a-old"],
        parent_pr_branches=["quikode/a-old"],
    )
    o.store.transition("A", State.MERGED, branch="quikode/a-abc123")

    nxt = o._pick_next({"A", "B"}, set())
    assert nxt == "B"
    assert o.store.get_parent_task_ids("B") == []
    assert o.store.get_parent_branches("B") == []
    o.store.conn.close()


def test_pick_next_does_not_return_child_when_parent_pending(tmp_path):
    """Parent in PENDING (no work done) → child waits regardless of
    stacking strategy. There's no branch yet to stack on."""
    dag = _make_dag(tmp_path, [("A", []), ("B", ["A"])])
    o = _orch(tmp_path, dag, stacking_strategy=StackingStrategy.WITHIN_MILESTONE)
    o.store.upsert_pending("A")
    o.store.upsert_pending("B")
    # A stays in PENDING — no branch stamped yet.

    nxt = o._pick_next({"A", "B"}, set())
    # _pick_next should return A (deepest unmet dep is itself), not B.
    assert nxt == "A"
    o.store.conn.close()


def test_pick_next_addressing_feedback_is_stack_ready(tmp_path):
    """Parent in ADDRESSING_FEEDBACK → child can be stacked (same
    semantics as PENDING_CI)."""
    dag = _make_dag(tmp_path, [("A", []), ("B", ["A"])])
    o = _orch(tmp_path, dag, stacking_strategy=StackingStrategy.WITHIN_MILESTONE)
    o.store.upsert_pending("A")
    o.store.upsert_pending("B")
    o.store.transition("A", State.ADDRESSING_FEEDBACK, branch="quikode/a-resp")

    nxt = o._pick_next({"A", "B"}, set())
    assert nxt == "B"
    row_b = o.store.get("B")
    assert json.loads(row_b["parent_pr_branches"]) == ["quikode/a-resp"]
    o.store.conn.close()


def test_pick_next_provisioning_is_stack_ready(tmp_path):
    """Parent transiently in PROVISIONING (during review-response container
    creation) → child can still be stacked. The parent's remote branch is
    unchanged throughout PROVISIONING; only the container is being recreated."""
    dag = _make_dag(tmp_path, [("A", []), ("B", ["A"])])
    o = _orch(tmp_path, dag, stacking_strategy=StackingStrategy.WITHIN_MILESTONE)
    o.store.upsert_pending("A")
    o.store.upsert_pending("B")
    o.store.transition("A", State.PROVISIONING, branch="quikode/a-prov")

    nxt = o._pick_next({"A", "B"}, set())
    assert nxt == "B"
    row_b = o.store.get("B")
    assert json.loads(row_b["parent_pr_branches"]) == ["quikode/a-prov"]
    o.store.conn.close()


def test_pick_next_fixup_planning_is_stack_ready(tmp_path):
    """Parent in FIXUP_PLANNING (mid review/CI fixup planner call) → child
    can still be stacked. Same rationale as PROVISIONING — the parent's
    remote branch is stable."""
    dag = _make_dag(tmp_path, [("A", []), ("B", ["A"])])
    o = _orch(tmp_path, dag, stacking_strategy=StackingStrategy.WITHIN_MILESTONE)
    o.store.upsert_pending("A")
    o.store.upsert_pending("B")
    o.store.transition("A", State.FIXUP_PLANNING, branch="quikode/a-fix")

    nxt = o._pick_next({"A", "B"}, set())
    assert nxt == "B"
    row_b = o.store.get("B")
    assert json.loads(row_b["parent_pr_branches"]) == ["quikode/a-fix"]
    o.store.conn.close()


def test_pick_next_stacking_off_blocks_until_merged(tmp_path):
    """With stacking_strategy=off, child waits for MERGED only — not
    PENDING_CI."""
    dag = _make_dag(tmp_path, [("A", []), ("B", ["A"])])
    o = _orch(tmp_path, dag, stacking_strategy=StackingStrategy.OFF)
    o.store.upsert_pending("A")
    o.store.upsert_pending("B")
    o.store.transition("A", State.PENDING_CI, branch="quikode/a-merge")
    # With stacking off, B is NOT pickable while A is unmerged.
    assert o._pick_next({"A", "B"}, set()) is None
    # Sanity: if we mark A as MERGED, B becomes pickable.
    o.store.transition("A", State.MERGED)
    assert o._pick_next({"A", "B"}, set()) == "B"
    o.store.conn.close()


def test_pick_next_pending_ci_is_stack_ready(tmp_path):
    """Backward-compat: PENDING_CI is still in the stack-ready set."""
    dag = _make_dag(tmp_path, [("A", []), ("B", ["A"])])
    o = _orch(tmp_path, dag, stacking_strategy=StackingStrategy.WITHIN_MILESTONE)
    o.store.upsert_pending("A")
    o.store.upsert_pending("B")
    o.store.transition("A", State.PENDING_CI, branch="quikode/a-ci")

    nxt = o._pick_next({"A", "B"}, set())
    assert nxt == "B"
    row_b = o.store.get("B")
    assert json.loads(row_b["parent_pr_branches"]) == ["quikode/a-ci"]
    o.store.conn.close()
