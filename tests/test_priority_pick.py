"""v3 priority pick: among all eligible candidates at slot-free moments, the
orchestrator picks the highest-priority one rather than first-by-sorted-id.

Rationale: under sustained Phase 3+ parallelism the picker is called several
times per slot-free moment, and naive id-order ignores the chain-unlock
value of each candidate (a stacked child or a high-fan-out root contributes
more downstream throughput than a fresh leaf root). Reviews and CI-fixups
don't compete here — they're dispatched separately.
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


def test_picks_high_fan_out_over_low_fan_out(tmp_path):
    """Both R-001 (4 dependents) and R-099 (0 dependents) have all deps
    merged → fresh roots. R-001 should win because it unblocks more
    downstream work even though both are ready."""
    edges = [
        ("R-001", []),
        ("R-099", []),
        # Dependents of R-001 — these are in scope but not picked here.
        ("R-010", ["R-001"]),
        ("R-011", ["R-001"]),
        ("R-012", ["R-001"]),
        ("R-013", ["R-001"]),
    ]
    dag = _make_dag(tmp_path, edges)
    o = _orch(tmp_path, dag)
    for nid, _ in edges:
        o.store.upsert_pending(nid)
    scope = {nid for nid, _ in edges}
    nxt = o._pick_next(scope, set())
    assert nxt == "R-001"


def test_id_tiebreak_when_no_other_signal(tmp_path):
    """Two equal-fan-out fresh roots → lower-numbered id wins on tiebreak.
    Preserves the rough milestone ordering operators expect."""
    edges = [
        ("R-005", []),
        ("R-001", []),
        # No dependents to discriminate.
    ]
    dag = _make_dag(tmp_path, edges)
    o = _orch(tmp_path, dag)
    for nid, _ in edges:
        o.store.upsert_pending(nid)
    nxt = o._pick_next({"R-001", "R-005"}, set())
    assert nxt == "R-001"


def test_stacked_child_beats_fresh_root_with_no_dependents(tmp_path):
    """A stacked R-002 (parent AWAITING_MERGE) should be picked over a fresh
    R-099 root. Stacking advances chain throughput; finishing a chain
    unblocks more downstream work than starting a new one."""
    edges = [
        ("R-001", []),
        ("R-002", ["R-001"]),
        ("R-099", []),
    ]
    dag = _make_dag(tmp_path, edges)
    o = _orch(tmp_path, dag, stacking_strategy="within-milestone")
    o.store.upsert_pending("R-001")
    o.store.upsert_pending("R-002")
    o.store.upsert_pending("R-099")
    o.store.transition("R-001", State.AWAITING_MERGE, branch="quikode/r-001-abc")

    nxt = o._pick_next({"R-001", "R-002", "R-099"}, set())
    assert nxt == "R-002"


def test_high_fan_out_root_beats_stacked_with_no_unblock(tmp_path):
    """If a fresh root unblocks far more downstream work than a stacked
    child, fan-out beats stacking. The +50 stacking_boost is meaningful
    but not so dominant that it overrides large fan-out signals (1 stacked
    boost = ~10 dependents)."""
    edges = [
        # Big-fan-out root: R-100 with 25 dependents in scope.
        ("R-100", []),
        # Stacked R-002 → no further dependents.
        ("R-001", []),
        ("R-002", ["R-001"]),
    ]
    # Add 25 dependents on R-100.
    for i in range(200, 225):
        edges.append((f"R-{i:03d}", ["R-100"]))
    dag = _make_dag(tmp_path, edges)
    o = _orch(tmp_path, dag, stacking_strategy="within-milestone")
    for nid, _ in edges:
        o.store.upsert_pending(nid)
    o.store.transition("R-001", State.AWAITING_MERGE, branch="quikode/r-001-abc")
    scope = {nid for nid, _ in edges}

    nxt = o._pick_next(scope, set())
    # 25 deps × 5 = 125 unblock_boost vs. 50 stacking_boost. R-100 wins.
    assert nxt == "R-100"


def test_returns_none_when_no_eligible(tmp_path):
    edges = [("R-001", []), ("R-002", ["R-001"])]
    dag = _make_dag(tmp_path, edges)
    o = _orch(tmp_path, dag)
    o.store.upsert_pending("R-001")
    o.store.upsert_pending("R-002")
    # R-001 in_flight, R-002 has unmet dep — neither pickable.
    nxt = o._pick_next({"R-001", "R-002"}, in_flight={"R-001"})
    assert nxt is None
