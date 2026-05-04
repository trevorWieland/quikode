"""DAG loader and scheduler tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quikode.dag import DAG


def _write_dag(tmp_path: Path, nodes: list[dict], milestones: list[dict] | None = None) -> Path:
    p = tmp_path / "dag.json"
    p.write_text(
        json.dumps(
            {
                "schema": "test",
                "milestones": milestones or [{"id": "M-1", "title": "x", "goal": "x", "status": "planned"}],
                "nodes": nodes,
            }
        )
    )
    return p


def _node(nid: str, deps: list[str] | None = None, **kw) -> dict:
    return {
        "id": nid,
        "kind": "behavior",
        "milestone": "M-1",
        "title": nid,
        "scope": "scope",
        "depends_on": deps or [],
        "completes_behaviors": [],
        "supports_behaviors": [],
        "boundary_with_neighbors": "",
        "expected_evidence": [],
        "playbook": [],
        "rationale": "",
        "risks": [],
        **kw,
    }


def test_load_simple_dag(tmp_path):
    p = _write_dag(tmp_path, [_node("R-1"), _node("R-2", ["R-1"])])
    d = DAG.load(p)
    assert set(d.nodes) == {"R-1", "R-2"}


def test_topo_layers_groups_by_depth(tmp_path):
    p = _write_dag(
        tmp_path,
        [
            _node("A"),
            _node("B", ["A"]),
            _node("C", ["A"]),
            _node("D", ["B", "C"]),
            _node("E"),
        ],
    )
    d = DAG.load(p)
    layers = d.topo_layers()
    assert {"A", "E"} == set(layers[0])
    assert {"B", "C"} == set(layers[1])
    assert {"D"} == set(layers[2])


def test_cycle_detection_raises(tmp_path):
    p = _write_dag(tmp_path, [_node("X", ["Y"]), _node("Y", ["X"])])
    d = DAG.load(p)
    with pytest.raises(ValueError, match="cycle"):
        d.topo_layers()


def test_ready_nodes_respects_completed_and_active(tmp_path):
    p = _write_dag(tmp_path, [_node("A"), _node("B", ["A"]), _node("C", ["A"])])
    d = DAG.load(p)
    assert {n.id for n in d.ready_nodes(set(), set())} == {"A"}
    # A merged → B and C unblock
    assert {n.id for n in d.ready_nodes({"A"}, set())} == {"B", "C"}
    # B in progress → only C ready
    assert {n.id for n in d.ready_nodes({"A"}, {"B"})} == {"C"}


def test_descendants_and_ancestors(tmp_path):
    p = _write_dag(
        tmp_path,
        [
            _node("A"),
            _node("B", ["A"]),
            _node("C", ["B"]),
            _node("D", ["A"]),
        ],
    )
    d = DAG.load(p)
    assert d.descendants_of("A") == {"B", "C", "D"}
    assert d.descendants_of("C") == set()
    assert d.ancestors_of("C") == {"A", "B"}
    assert d.ancestors_of("A") == set()


def test_filter_includes_transitive_deps(tmp_path):
    p = _write_dag(
        tmp_path,
        [
            _node("A"),
            _node("B", ["A"]),
            _node("C", ["B"]),
            _node("D"),
        ],
    )
    d = DAG.load(p)
    # Asking for C should include B and A
    assert d.filter(["C"]) == {"A", "B", "C"}
    # No filter → all
    assert d.filter() == {"A", "B", "C", "D"}


def test_milestone_filter(tmp_path):
    p = _write_dag(
        tmp_path,
        [
            _node("A", milestone="M-1"),
            _node("B", milestone="M-2"),
        ],
        milestones=[
            {"id": "M-1", "title": "x", "goal": "x", "status": "planned"},
            {"id": "M-2", "title": "y", "goal": "y", "status": "planned"},
        ],
    )
    d = DAG.load(p)
    assert d.filter(milestone="M-2") == {"B"}


def test_real_tanren_dag_loads():
    """Smoke test against the real tanren dag if it's locally available."""
    p = Path("/home/trevor/github/tanren/docs/roadmap/dag.json")
    if not p.exists():
        pytest.skip("tanren dag not available")
    d = DAG.load(p)
    s = d.stats()
    assert s["node_count"] >= 100
    assert s["depth"] >= 5
    # F-0001 is the foundation
    assert "F-0001" in d.nodes
    assert d.nodes["F-0001"].kind == "foundation"
