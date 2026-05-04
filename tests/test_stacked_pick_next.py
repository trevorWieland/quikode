"""v3 Phase C stacked-diff scheduler: child tasks pick parent_pr_branch
when scheduled alongside an in-flight parent.

The orchestrator's `_pick_next` is the gate. When stacking is enabled and
a parent is in a stack-ready state (AWAITING_MERGE, RESPONDING_TO_REVIEW,
PR_OPENING, POLLING_CI), the child becomes pickable AND has its
`parent_pr_branch` + `parent_branch` stamped before scheduling so the
worker's `_provision_worktree` branches off the parent.
"""

from __future__ import annotations

import json
from pathlib import Path

from quikode.config import Config
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


def test_pick_next_stamps_parent_pr_branch_when_parent_awaiting_merge(tmp_path):
    """A child whose dep is AWAITING_MERGE → picked + parent_pr_branch
    stamped from the parent's branch."""
    dag = _make_dag(tmp_path, [("A", []), ("B", ["A"])])
    o = _orch(tmp_path, dag, stacking_strategy="within-milestone")
    o.store.upsert_pending("A")
    o.store.upsert_pending("B")
    # Parent A is AWAITING_MERGE with a branch.
    o.store.transition("A", State.AWAITING_MERGE, branch="quikode/a-abc123")

    nxt = o._pick_next({"A", "B"}, set())
    assert nxt == "B"
    row_b = o.store.get("B")
    assert row_b["parent_pr_branch"] == "quikode/a-abc123"
    assert row_b["parent_branch"] == "quikode/a-abc123"
    o.store.conn.close()


def test_pick_next_clears_parent_branch_when_parent_merged(tmp_path):
    """A child whose dep is MERGED → picked WITHOUT parent_pr_branch
    (clears any prior stale value)."""
    dag = _make_dag(tmp_path, [("A", []), ("B", ["A"])])
    o = _orch(tmp_path, dag, stacking_strategy="within-milestone")
    o.store.upsert_pending("A")
    o.store.upsert_pending("B")
    # Pre-stamp B with stale stacking metadata (e.g., from a prior tick
    # where A was AWAITING_MERGE). The merge should clear it.
    o.store.set_field("B", parent_pr_branch="quikode/a-old", parent_branch="quikode/a-old")
    o.store.transition("A", State.MERGED, branch="quikode/a-abc123")

    nxt = o._pick_next({"A", "B"}, set())
    assert nxt == "B"
    row_b = o.store.get("B")
    assert row_b["parent_pr_branch"] is None
    assert row_b["parent_branch"] is None
    o.store.conn.close()


def test_pick_next_does_not_return_child_when_parent_pending(tmp_path):
    """Parent in PENDING (no work done) → child waits regardless of
    stacking strategy. There's no branch yet to stack on."""
    dag = _make_dag(tmp_path, [("A", []), ("B", ["A"])])
    o = _orch(tmp_path, dag, stacking_strategy="within-milestone")
    o.store.upsert_pending("A")
    o.store.upsert_pending("B")
    # A stays in PENDING — no branch stamped yet.

    nxt = o._pick_next({"A", "B"}, set())
    # _pick_next should return A (deepest unmet dep is itself), not B.
    assert nxt == "A"
    o.store.conn.close()


def test_pick_next_responding_to_review_is_stack_ready(tmp_path):
    """Parent in RESPONDING_TO_REVIEW → child can be stacked (same
    semantics as AWAITING_MERGE)."""
    dag = _make_dag(tmp_path, [("A", []), ("B", ["A"])])
    o = _orch(tmp_path, dag, stacking_strategy="within-milestone")
    o.store.upsert_pending("A")
    o.store.upsert_pending("B")
    o.store.transition("A", State.RESPONDING_TO_REVIEW, branch="quikode/a-resp")

    nxt = o._pick_next({"A", "B"}, set())
    assert nxt == "B"
    row_b = o.store.get("B")
    assert row_b["parent_pr_branch"] == "quikode/a-resp"
    o.store.conn.close()


def test_pick_next_stacking_off_blocks_until_merged(tmp_path):
    """With stacking_strategy=off, child waits for MERGED only — not
    AWAITING_MERGE."""
    dag = _make_dag(tmp_path, [("A", []), ("B", ["A"])])
    o = _orch(tmp_path, dag, stacking_strategy="off")
    o.store.upsert_pending("A")
    o.store.upsert_pending("B")
    o.store.transition("A", State.AWAITING_MERGE, branch="quikode/a-merge")
    # With stacking off, B is NOT pickable while A is unmerged.
    assert o._pick_next({"A", "B"}, set()) is None
    # Sanity: if we mark A as MERGED, B becomes pickable.
    o.store.transition("A", State.MERGED)
    assert o._pick_next({"A", "B"}, set()) == "B"
    o.store.conn.close()


def test_pick_next_polling_ci_still_stack_ready(tmp_path):
    """Backward-compat: POLLING_CI is still in the stack-ready set."""
    dag = _make_dag(tmp_path, [("A", []), ("B", ["A"])])
    o = _orch(tmp_path, dag, stacking_strategy="within-milestone")
    o.store.upsert_pending("A")
    o.store.upsert_pending("B")
    o.store.transition("A", State.POLLING_CI, branch="quikode/a-ci")

    nxt = o._pick_next({"A", "B"}, set())
    assert nxt == "B"
    row_b = o.store.get("B")
    assert row_b["parent_pr_branch"] == "quikode/a-ci"
    o.store.conn.close()
