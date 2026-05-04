"""Render tests — glyphs, colors, critical-path overlay."""

from __future__ import annotations

import json
from pathlib import Path

from quikode.dag import DAG
from quikode.tui.dag_view.layout import columns, edges, ranks
from quikode.tui.dag_view.render import (
    Filter,
    ascii_canvas,
    critical_path_from,
    grid_to_lines,
)


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


def _layout(d: DAG):
    r = ranks(d)
    c = columns(d, r)
    e = edges(d, r, c)
    return r, c, e


def test_ascii_canvas_renders_node_glyphs(tmp_path):
    p = _write_dag(tmp_path, [_node("A"), _node("B", ["A"])])
    d = DAG.load(p)
    r, c, e = _layout(d)
    states = {"A": "merged", "B": "doing"}
    grid = ascii_canvas(d, r, c, e, states=states)
    lines = grid_to_lines(grid)
    blob = "\n".join(lines)
    # State glyphs (✓ for merged, ▶ for doing) appear, with the id following.
    assert "✓" in blob
    assert "▶" in blob
    assert "A" in blob and "B" in blob


def test_pending_node_uses_dim_glyph(tmp_path):
    p = _write_dag(tmp_path, [_node("A")])
    d = DAG.load(p)
    r, c, e = _layout(d)
    grid = ascii_canvas(d, r, c, e, states={})
    blob = "\n".join(grid_to_lines(grid))
    assert "⋯" in blob  # unseeded glyph


def test_blocked_state_renders_x(tmp_path):
    p = _write_dag(tmp_path, [_node("A"), _node("B", ["A"])])
    d = DAG.load(p)
    r, c, e = _layout(d)
    grid = ascii_canvas(d, r, c, e, states={"A": "merged", "B": "blocked"})
    blob = "\n".join(grid_to_lines(grid))
    assert "✗" in blob


def test_critical_path_includes_anchor_and_unmerged_chain(tmp_path):
    # Chain A->B->C->D, B and C are unmerged. Critical path from D should
    # include B, C, D.
    p = _write_dag(
        tmp_path,
        [_node("A"), _node("B", ["A"]), _node("C", ["B"]), _node("D", ["C"])],
    )
    d = DAG.load(p)
    states = {"A": "merged", "B": "pending", "C": "pending", "D": "pending"}
    chain = critical_path_from(d, "D", states)
    assert chain == {"B", "C", "D"}


def test_critical_path_skips_merged_deps(tmp_path):
    # A merged → B unmerged → C unmerged. From C, path is {B, C}.
    p = _write_dag(
        tmp_path,
        [_node("A"), _node("B", ["A"]), _node("C", ["B"])],
    )
    d = DAG.load(p)
    chain = critical_path_from(d, "C", {"A": "merged", "B": "doing", "C": "pending"})
    assert chain == {"B", "C"}


def test_filter_blocked_includes_descendants(tmp_path):
    # A merged, B blocked, C depends on B. With filter=blocked,
    # only B and C should be in the canvas (not A).
    p = _write_dag(
        tmp_path,
        [_node("A"), _node("B", ["A"]), _node("C", ["B"])],
    )
    d = DAG.load(p)
    r, c, e = _layout(d)
    grid = ascii_canvas(
        d,
        r,
        c,
        e,
        states={"A": "merged", "B": "blocked", "C": "pending"},
        filter=Filter(kind="blocked"),
    )
    blob = "\n".join(grid_to_lines(grid))
    assert "B" in blob
    assert "C" in blob
    # A should NOT appear (it's not blocked, and it's an ancestor of blocked B,
    # but the filter is "blocked + descendants" — ancestors aren't included).
    # Use a uniqueness check rather than substring, since "A" is a single char.
    # The 'A' label is "⋯A" or similar — check the char doesn't appear at all.
    # Since "A" can match 'A' inside markup, count node-ids by glyph presence.
    # We rely on the fact that the layout would put A on rank 0; if it's
    # filtered out, rank 0 should be entirely whitespace.
    # The merged glyph is ✓, which only A would have here.
    assert "✓" not in blob


def test_filter_ready_only_keeps_ready_nodes(tmp_path):
    # A merged, B and C depend on A. With filter=ready, B and C are kept.
    p = _write_dag(
        tmp_path,
        [_node("A"), _node("B", ["A"]), _node("C", ["A"])],
    )
    d = DAG.load(p)
    r, c, e = _layout(d)
    grid = ascii_canvas(d, r, c, e, states={"A": "merged"}, filter=Filter(kind="ready"))
    blob = "\n".join(grid_to_lines(grid))
    # A is merged so not in "ready"; B and C are ready.
    assert "B" in blob and "C" in blob
    assert "✓" not in blob  # A is not painted


def test_critical_path_paints_cyan_in_grid(tmp_path):
    p = _write_dag(
        tmp_path,
        [_node("A"), _node("B", ["A"])],
    )
    d = DAG.load(p)
    r, c, e = _layout(d)
    cp = critical_path_from(d, "B", {"A": "pending", "B": "pending"})
    grid = ascii_canvas(d, r, c, e, states={"A": "pending", "B": "pending"}, critical_path=cp)
    # Every cell that contains a node glyph (e.g. ⋯, A, B) should have the
    # cyan style applied.
    cyan_node_cells = sum(
        1 for row in grid for cell in row if cell.style == "cyan" and cell.glyph in {"⋯", "A", "B"}
    )
    # Both nodes' cells should be cyan (anchor + path).
    assert cyan_node_cells >= 4
