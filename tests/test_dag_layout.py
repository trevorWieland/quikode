"""Layout tests — rank assignment, column placement, edge routing."""

from __future__ import annotations

import json
from pathlib import Path

from quikode.dag import DAG
from quikode.tui.dag_view.layout import columns, count_crossings, edges, ranks


def _write_dag(tmp_path: Path, nodes: list[dict]) -> Path:
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
    return p


def _node(nid: str, deps: list[str] | None = None, **kw) -> dict:
    return {
        "id": nid,
        "kind": "behavior",
        "milestone": "M-1",
        "title": nid,
        "scope": "",
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


def test_ranks_three_node_chain(tmp_path):
    p = _write_dag(
        tmp_path,
        [_node("A"), _node("B", ["A"]), _node("C", ["B"])],
    )
    d = DAG.load(p)
    r = ranks(d)
    assert r == {"A": 0, "B": 1, "C": 2}


def test_ranks_diamond(tmp_path):
    # A -> B, A -> C, B -> D, C -> D — D should be rank 2.
    p = _write_dag(
        tmp_path,
        [
            _node("A"),
            _node("B", ["A"]),
            _node("C", ["A"]),
            _node("D", ["B", "C"]),
            _node("E", ["D"]),
        ],
    )
    d = DAG.load(p)
    r = ranks(d)
    assert r["A"] == 0
    assert r["B"] == 1
    assert r["C"] == 1
    assert r["D"] == 2
    assert r["E"] == 3


def test_ranks_multi_root(tmp_path):
    # 10-node DAG with two roots.
    nodes = [
        _node("R1"),
        _node("R2"),
        _node("A", ["R1"]),
        _node("B", ["R1"]),
        _node("C", ["R2"]),
        _node("D", ["A", "B"]),
        _node("E", ["C"]),
        _node("F", ["D", "E"]),
        _node("G", ["F"]),
        _node("H", ["G"]),
    ]
    p = _write_dag(tmp_path, nodes)
    d = DAG.load(p)
    r = ranks(d)
    assert r["R1"] == 0
    assert r["R2"] == 0
    assert r["D"] == 2
    assert r["F"] == 3
    assert r["H"] == 5


def test_columns_unique_per_rank(tmp_path):
    p = _write_dag(
        tmp_path,
        [
            _node("A"),
            _node("B", ["A"]),
            _node("C", ["A"]),
            _node("D", ["B", "C"]),
        ],
    )
    d = DAG.load(p)
    r = ranks(d)
    cols = columns(d, r)
    # Each rank's columns must be unique (no two nodes in same cell).
    by_rank: dict[int, set[int]] = {}
    for nid, rank in r.items():
        by_rank.setdefault(rank, set())
        assert cols[nid] not in by_rank[rank], f"collision at rank {rank}: {nid}"
        by_rank[rank].add(cols[nid])


def test_edges_short_have_no_intermediate_cells(tmp_path):
    p = _write_dag(tmp_path, [_node("A"), _node("B", ["A"])])
    d = DAG.load(p)
    r = ranks(d)
    cols = columns(d, r)
    es = edges(d, r, cols)
    assert len(es) == 1
    assert es[0].source == "A"
    assert es[0].target == "B"
    assert es[0].cells == []  # rank 0 -> rank 1, no dummy cells


def test_edges_long_get_dummy_cells(tmp_path):
    # A->B->C->D plus a long edge A->D that skips two ranks.
    p = _write_dag(
        tmp_path,
        [
            _node("A"),
            _node("B", ["A"]),
            _node("C", ["B"]),
            _node("D", ["C", "A"]),
        ],
    )
    d = DAG.load(p)
    r = ranks(d)
    cols = columns(d, r)
    es = edges(d, r, cols)
    a_to_d = next(e for e in es if e.source == "A" and e.target == "D")
    # rank(A)=0, rank(D)=3 — span 3, so 2 intermediate cells.
    assert len(a_to_d.cells) == 2
    # Intermediate ranks are 1 and 2.
    assert {c[0] for c in a_to_d.cells} == {1, 2}


def test_columns_minimize_crossings_diamond(tmp_path):
    # In a diamond, B and C should land at columns that don't cross.
    p = _write_dag(
        tmp_path,
        [
            _node("A"),
            _node("B", ["A"]),
            _node("C", ["A"]),
            _node("D", ["B", "C"]),
        ],
    )
    d = DAG.load(p)
    r = ranks(d)
    cols = columns(d, r)
    # Diamond has zero adjacent-rank crossings if barycenter places
    # B and C consistently between A and D.
    assert count_crossings(d, r, cols) == 0


def test_tanren_like_dag_layout_bounds():
    """Always-on layout smoke for a checked-in Tanren-like DAG."""
    d = DAG.load(Path(__file__).parent / "fixtures" / "tanren_dag.json")
    r = ranks(d)
    cols = columns(d, r)
    max_rank = max(r.values())
    max_col = max(cols.values())
    assert max_rank == 2
    assert max_col < 4
