"""Phase C stacked-diff scheduler logic."""

from __future__ import annotations

import json
from itertools import pairwise
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


def test_stacking_off_blocks_dependent_until_merge(tmp_path):
    dag = _make_dag(tmp_path, [("A", []), ("B", ["A"])])
    o = _orch(tmp_path, dag, stacking_strategy="off")
    o.store.upsert_pending("A")
    o.store.upsert_pending("B")
    o.store.transition("A", State.POLLING_CI)
    # With stacking off, B is not ready while A is still in flight (not merged)
    assert o._pick_next({"A", "B"}, set()) is None
    o.store.transition("A", State.MERGED)
    # Now B is ready
    assert o._pick_next({"A", "B"}, set()) == "B"


def test_stacking_within_milestone_picks_in_flight_dep(tmp_path):
    dag = _make_dag(tmp_path, [("A", []), ("B", ["A"])])
    o = _orch(tmp_path, dag, stacking_strategy="within-milestone")
    o.store.upsert_pending("A")
    o.store.upsert_pending("B")
    o.store.transition("A", State.POLLING_CI)
    # B's dep is in POLLING_CI (stack-ready) and same milestone → ready
    assert o._pick_next({"A", "B"}, set()) == "B"


def test_stacking_aggressive_works_across_milestones(tmp_path):
    nodes = [
        {
            "id": "A",
            "kind": "behavior",
            "milestone": "M-1",
            "title": "A",
            "scope": "x",
            "depends_on": [],
            "completes_behaviors": [],
            "supports_behaviors": [],
            "boundary_with_neighbors": "",
            "expected_evidence": [],
            "playbook": [],
            "rationale": "",
            "risks": [],
        },
        {
            "id": "B",
            "kind": "behavior",
            "milestone": "M-2",
            "title": "B",
            "scope": "x",
            "depends_on": ["A"],
            "completes_behaviors": [],
            "supports_behaviors": [],
            "boundary_with_neighbors": "",
            "expected_evidence": [],
            "playbook": [],
            "rationale": "",
            "risks": [],
        },
    ]
    p = tmp_path / "dag.json"
    p.write_text(
        json.dumps(
            {
                "schema": "test",
                "milestones": [
                    {"id": "M-1", "title": "x", "goal": "x", "status": "planned"},
                    {"id": "M-2", "title": "y", "goal": "y", "status": "planned"},
                ],
                "nodes": nodes,
            }
        )
    )
    dag = DAG.load(p)
    cfg = Config(repo_path=tmp_path, dag_path=p, stacking_strategy="within-milestone")
    store = Store(tmp_path / "q.db")
    o = Orchestrator(cfg, dag, store)
    store.upsert_pending("A")
    store.upsert_pending("B")
    store.transition("A", State.POLLING_CI)
    # within-milestone: B in M-2, A in M-1 → not stackable
    assert o._pick_next({"A", "B"}, set()) is None
    # aggressive: stackable across milestones
    cfg2 = Config(repo_path=tmp_path, dag_path=p, stacking_strategy="aggressive")
    o2 = Orchestrator(cfg2, dag, store)
    assert o2._pick_next({"A", "B"}, set()) == "B"


def test_stack_depth_cap(tmp_path):
    # A → B → C → D chain. With max_depth=2, only A and B can stack; C waits for B's merge.
    dag = _make_dag(tmp_path, [("A", []), ("B", ["A"]), ("C", ["B"]), ("D", ["C"])])
    o = _orch(tmp_path, dag, stacking_strategy="within-milestone", stacking_max_depth=2)
    for nid in ("A", "B", "C", "D"):
        o.store.upsert_pending(nid)
    o.store.transition("A", State.POLLING_CI)
    o.store.transition("B", State.POLLING_CI)
    o.store.set_field("B", parent_task_id="A")
    # C wants to stack on B (which is stacked on A) → depth 2 would push past cap
    next_id = o._pick_next({"A", "B", "C", "D"}, set())
    # depth check via _stack_depth("B") = 2 already → C not picked
    # In practice, our implementation uses _stack_depth(unmet[0]) where unmet[0] is "B".
    # B's stack depth = 1 (B → A → none). So C's effective depth would be 2.
    # max_depth=2 means depth >= 2 is rejected.
    assert next_id is None  # C blocked by depth cap


# ----- v3 _all_done semantics -----


def test_all_done_requires_truly_terminal_states(tmp_path):
    """v3 regression: orchestrator must keep ticking when tasks are
    AWAITING_MERGE (so the review-watcher can poll). _all_done returns True
    only when every task is in {MERGED, BLOCKED, FAILED, ABORTED}.
    """
    dag = _make_dag(tmp_path, [("A", []), ("B", [])])
    o = _orch(tmp_path, dag)
    o.store.upsert_pending("A")
    o.store.upsert_pending("B")
    scope = {"A", "B"}

    # Both AWAITING_MERGE — NOT done (review watcher must keep polling)
    o.store.transition("A", State.AWAITING_MERGE)
    o.store.transition("B", State.AWAITING_MERGE)
    assert o._all_done(scope) is False

    # One MERGED, one AWAITING_MERGE — still NOT done
    o.store.transition("A", State.MERGED)
    assert o._all_done(scope) is False

    # Both terminal — done
    o.store.transition("B", State.MERGED)
    assert o._all_done(scope) is True


def test_all_done_responding_to_review_blocks_exit(tmp_path):
    dag = _make_dag(tmp_path, [("A", [])])
    o = _orch(tmp_path, dag)
    o.store.upsert_pending("A")
    o.store.transition("A", State.RESPONDING_TO_REVIEW)
    assert o._all_done({"A"}) is False


def test_all_done_failed_counts_as_terminal(tmp_path):
    dag = _make_dag(tmp_path, [("A", [])])
    o = _orch(tmp_path, dag)
    o.store.upsert_pending("A")
    o.store.transition("A", State.FAILED)
    assert o._all_done({"A"}) is True


# ----- Item 3: deep stacks + cycle + breadth defenses -----


def _seed_chain_in_polling(o, ids):
    """Put `ids[:-1]` in POLLING_CI, mark each as parent_task_id of the next.
    Leaves `ids[-1]` PENDING so `_pick_next` is the test subject."""
    for nid in ids:
        o.store.upsert_pending(nid)
    for parent in ids[:-1]:
        o.store.transition(parent, State.POLLING_CI, branch=f"quikode/{parent.lower()}-x")
    # Wire parent_task_id chain for the in-flight tasks (skip the last
    # PENDING node — its parent gets stamped by _pick_next).
    for parent, child in pairwise(ids[:-1]):
        o.store.set_field(child, parent_task_id=parent)


def test_stack_depth_3_chain_allowed_at_default(tmp_path):
    dag = _make_dag(tmp_path, [("A", []), ("B", ["A"]), ("C", ["B"]), ("D", ["C"])])
    o = _orch(tmp_path, dag, stacking_strategy="within-milestone")  # default max_depth=6
    _seed_chain_in_polling(o, ["A", "B", "C", "D"])
    # D's dep is C; C's stack depth is 2 (C→B→A→None), under cap of 6.
    assert o._pick_next({"A", "B", "C", "D"}, set()) == "D"


def test_stack_depth_5_chain_allowed_at_default(tmp_path):
    chain = ["A", "B", "C", "D", "E", "F"]
    edges = [(chain[0], [])] + [(chain[i], [chain[i - 1]]) for i in range(1, len(chain))]
    dag = _make_dag(tmp_path, edges)
    o = _orch(tmp_path, dag, stacking_strategy="within-milestone")
    _seed_chain_in_polling(o, chain)
    # F is the only PENDING; its dep E has depth 4 → allowed under cap 6.
    assert o._pick_next(set(chain), set()) == "F"


def test_stack_depth_6_at_cap_blocks(tmp_path):
    chain = ["A", "B", "C", "D", "E", "F", "G"]
    edges = [(chain[0], [])] + [(chain[i], [chain[i - 1]]) for i in range(1, len(chain))]
    dag = _make_dag(tmp_path, edges)
    o = _orch(tmp_path, dag, stacking_strategy="within-milestone", stacking_max_depth=6)
    _seed_chain_in_polling(o, chain)
    # G's dep F has depth 5; cap is 6 → would be 6 → rejected (depth >= cap).
    # Wait, depth is computed for unmet[0] (F), and F's depth is 5. 5 >= 6 → False.
    # Actually let me recheck _pick_next: `depth >= cfg.stacking_max_depth`.
    # F has depth 5. 5 >= 6 → False → allowed. So G IS picked. Adjust expectation.
    # The cap means: depth (of dep) must be < max_depth. To block G we need
    # F at depth 6+. So extend chain by one more:
    chain2 = [*chain, "H"]
    edges2 = [*edges, ("H", ["G"])]
    sub = tmp_path / "two"
    sub.mkdir(parents=True, exist_ok=True)
    dag2 = _make_dag(sub, edges2)
    o2 = _orch(sub, dag2, stacking_strategy="within-milestone", stacking_max_depth=6)
    _seed_chain_in_polling(o2, chain2)
    # H's dep G has depth 6 → 6 >= 6 → REJECTED.
    assert o2._pick_next(set(chain2), set()) is None


def test_stack_depth_higher_default_allows_chain_of_4(tmp_path):
    """At default depth (6), a 4-deep chain should pick up fine."""
    chain = ["A", "B", "C", "D", "E"]
    edges = [(chain[0], [])] + [(chain[i], [chain[i - 1]]) for i in range(1, len(chain))]
    dag = _make_dag(tmp_path, edges)
    o = _orch(tmp_path, dag, stacking_strategy="within-milestone")
    _seed_chain_in_polling(o, chain)
    assert o._pick_next(set(chain), set()) == "E"


def test_cycle_in_parent_task_id_rejects_stack(tmp_path):
    """Synthetic: A→B→A cycle in parent_task_id metadata. _stack_depth
    must short-circuit and report depth past the cap."""
    dag = _make_dag(tmp_path, [("A", []), ("B", ["A"]), ("C", ["B"])])
    o = _orch(tmp_path, dag, stacking_strategy="within-milestone", stacking_max_depth=6)
    o.store.upsert_pending("A")
    o.store.upsert_pending("B")
    o.store.upsert_pending("C")
    o.store.transition("A", State.POLLING_CI, branch="quikode/a-x")
    o.store.transition("B", State.POLLING_CI, branch="quikode/b-x")
    # Inject cycle: A's parent is B; B's parent is A
    o.store.set_field("A", parent_task_id="B")
    o.store.set_field("B", parent_task_id="A")
    # depth of B walks B→A→B (cycle); should exceed cap → C blocked.
    assert o._pick_next({"A", "B", "C"}, set()) is None


def test_breadth_per_root_blocks_excess(tmp_path):
    """Build a fan-out: ROOT in POLLING_CI, with N already-stacked children.
    Adding another child must be refused once breadth cap is hit."""
    nodes = [("ROOT", [])]
    for i in range(8):
        nodes.append((f"C{i}", ["ROOT"]))
    nodes.append(("LATE", ["ROOT"]))
    dag = _make_dag(tmp_path, nodes)
    o = _orch(
        tmp_path,
        dag,
        stacking_strategy="within-milestone",
        stacking_max_breadth_per_root=4,
    )
    for n, _ in nodes:
        o.store.upsert_pending(n)
    o.store.transition("ROOT", State.POLLING_CI, branch="quikode/root-x")
    # Pre-stack 5 children under ROOT. With cap 4, adding "LATE" must fail.
    for i in range(5):
        cid = f"C{i}"
        o.store.transition(cid, State.POLLING_CI, branch=f"quikode/{cid.lower()}-x")
        o.store.set_field(cid, parent_task_id="ROOT")
    # 1 root + 5 children = 6 > 4 → LATE rejected.
    assert o._pick_next({n for n, _ in nodes}, set()) is None
