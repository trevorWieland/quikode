"""ASCII canvas rendering for the DAG viewer.

Pure: input is the layout (ranks/columns/edges) plus a state map; output
is a 2D grid of `Cell` objects (glyph + textual markup style). The screen
widget renders strips by walking the grid.

Layout convention:
- Each (rank, col) takes a fixed cell of width `NODE_W` and height
  `NODE_H = 1` row of node, with `RANK_GAP` blank rows between ranks for
  edge routing. Compact mode (zoom -) drops `RANK_GAP` to 0; loose mode
  (zoom +) bumps to 2.
- Node glyph: a single character (✓ ▶ ⏸ ✗ ⋯) plus the task id rendered
  to the right when the column is wide enough. We use a 9-wide cell so
  short ids ("R-0001") fit with a leading state glyph.

The canvas is sized to fit *all* columns and ranks; the viewport is the
caller's responsibility (the screen widget pans the camera over this).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from quikode.dag import DAG

from .layout import Edge

# State -> markup style. Edge variants are computed in `_edge_style`.
_STATE_STYLE: dict[str, str] = {
    "merged": "green",
    "merge_ready": "green",
    "awaiting_review": "yellow",
    "pending_ci": "yellow",
    "blocked": "red",
    "failed": "red",
    "aborted": "red",
    "pending": "dim",
    "": "dim",  # unseeded
}
_IN_FLIGHT_STATES = {
    "provisioning",
    "planning",
    "doing",
    "checking",
    "triaging",
    "doing_subtask",
    "checking_subtask",
    "triaging_subtask",
    "committing",
    "pushing",
    "pr_opening",
    "pending_ci",
    "rebasing_to_main",
    "conflict_resolving",
    "triaging_feedback",
    "fixup_planning",
    "addressing_feedback",
}

# State -> single-character glyph painted at the start of the node cell.
_STATE_GLYPH: dict[str, str] = {
    "merged": "✓",
    "merge_ready": "✓",
    "awaiting_review": "⏸",
    "pending_ci": "⏵",
    "blocked": "✗",
    "failed": "✗",
    "aborted": "✗",
}


def _node_style(state: str | None) -> str:
    if state in _IN_FLIGHT_STATES:
        return "cyan"
    return _STATE_STYLE.get(state or "", "dim")


def _node_glyph(state: str | None) -> str:
    if state in _IN_FLIGHT_STATES:
        return "▶"
    return _STATE_GLYPH.get(state or "", "⋯")


@dataclass
class Cell:
    """One terminal cell. `glyph` is exactly 1 character; `style` is a
    textual/rich markup tag like 'cyan' or '' for default."""

    glyph: str = " "
    style: str = ""


@dataclass
class Filter:
    """View filter selected via /filter."""

    kind: Literal["all", "blocked", "ready", "milestone"]
    milestone: str | None = None


# Per-column character budget. 8 chars per node leaves room for state
# glyph + 6-char id ("R-0001") + 1 trailing space for the next column.
NODE_W = 8
# Spacing between adjacent ranks (vertical). Default = 1 (one edge row).
DEFAULT_RANK_GAP = 1


@dataclass
class _Layout:
    """Internal: pre-computed grid coordinates."""

    n_ranks: int
    n_cols: int
    rank_gap: int

    @property
    def rows(self) -> int:
        if self.n_ranks == 0:
            return 0
        return self.n_ranks + (self.n_ranks - 1) * self.rank_gap

    @property
    def cols(self) -> int:
        return self.n_cols * NODE_W

    def row_of_rank(self, r: int) -> int:
        return r * (1 + self.rank_gap)

    def col_of(self, c: int) -> int:
        return c * NODE_W


def critical_path_from(dag: DAG, anchor: str, states: dict[str, str]) -> set[str]:
    """Return node ids on the deepest unmerged dep chain reachable from
    `anchor` (looking *upward* through deps and *downward* through
    descendants).

    The chain is "longest weighted by 1 per unmerged node". Used by the
    `c` keybinding to highlight what's gating a cursor task.
    """
    if anchor not in dag.nodes:
        return set()

    def is_unmerged(nid: str) -> bool:
        return states.get(nid) != "merged"

    # Upward: longest unmerged dep chain to a root.
    cache_up: dict[str, list[str]] = {}

    def best_up(nid: str) -> list[str]:
        if nid in cache_up:
            return cache_up[nid]
        deps = [d for d in dag.nodes[nid].depends_on if d in dag.nodes and is_unmerged(d)]
        if not deps:
            cache_up[nid] = [nid] if is_unmerged(nid) else []
            return cache_up[nid]
        best: list[str] = []
        for dep in deps:
            candidate = best_up(dep)
            if len(candidate) > len(best):
                best = candidate
        cache_up[nid] = ([nid] if is_unmerged(nid) else []) + best
        return cache_up[nid]

    # Downward: longest unmerged descendant chain.
    children: dict[str, list[str]] = defaultdict(list)
    for nid, n in dag.nodes.items():
        for d in n.depends_on:
            if d in dag.nodes:
                children[d].append(nid)

    cache_down: dict[str, list[str]] = {}

    def best_down(nid: str) -> list[str]:
        if nid in cache_down:
            return cache_down[nid]
        kids = [c for c in children.get(nid, []) if is_unmerged(c)]
        if not kids:
            cache_down[nid] = []
            return cache_down[nid]
        best_kid = max(kids, key=lambda c: len(best_down(c)))
        cache_down[nid] = [best_kid, *best_down(best_kid)]
        return cache_down[nid]

    up = best_up(anchor)
    down = best_down(anchor)
    chain = set(up) | set(down)
    chain.add(anchor)
    return chain


def _filtered_ids(dag: DAG, states: dict[str, str], filt: Filter | None) -> set[str] | None:
    """Return None to mean "no filter", else the set of node ids to keep."""
    if filt is None or filt.kind == "all":
        return None
    if filt.kind == "blocked":
        keep = {nid for nid, st in states.items() if st in {"blocked", "failed"}}
        # Include their descendants — anything that's gated by them.
        with_desc: set[str] = set(keep)
        for nid in keep:
            with_desc |= dag.descendants_of(nid)
        return with_desc
    if filt.kind == "ready":
        merged = {nid for nid, st in states.items() if st == "merged"}
        return {
            nid
            for nid, n in dag.nodes.items()
            if nid not in merged and all(d in merged for d in n.depends_on)
        }
    if filt.kind == "milestone" and filt.milestone:
        return {nid for nid, n in dag.nodes.items() if n.milestone == filt.milestone}
    return None


def _edge_style(source_state: str | None, target_state: str | None) -> str:
    """Edges to merged nodes are dim; edges into in-flight/ready render
    cyan; edges into blocked/failed render red. Default = default."""
    if target_state in {"blocked", "failed"}:
        return "red"
    if target_state in _IN_FLIGHT_STATES:
        return "cyan"
    if source_state == "merged" and target_state == "merged":
        return "dim"
    return ""


def _glyph_for_segment(has_left: bool, has_right: bool, has_up: bool, has_down: bool) -> str:
    """Pick the box-drawing char for an edge cell given which sides connect."""
    bits = (has_up, has_down, has_left, has_right)
    table: dict[tuple[bool, bool, bool, bool], str] = {
        (True, True, True, True): "┼",
        (True, True, True, False): "┤",
        (True, True, False, True): "├",
        (True, True, False, False): "│",
        (True, False, True, True): "┴",
        (False, True, True, True): "┬",
        (True, False, True, False): "┘",
        (True, False, False, True): "└",
        (False, True, True, False): "┐",
        (False, True, False, True): "┌",
        (True, False, False, False): "│",
        (False, True, False, False): "│",
        (False, False, True, True): "─",
        (False, False, True, False): "─",
        (False, False, False, True): "─",
    }
    return table.get(bits, "·")


class _EdgePainter:
    def __init__(self, layout: _Layout, grid: list[list[Cell]]) -> None:
        self.layout = layout
        self.grid = grid
        self.marks: dict[tuple[int, int], dict[str, bool]] = defaultdict(
            lambda: {"left": False, "right": False, "up": False, "down": False, "is_path": False}
        )
        self.styles: dict[tuple[int, int], str] = {}

    def mark(
        self,
        row: int,
        col: int,
        *,
        left: bool = False,
        right: bool = False,
        up: bool = False,
        down: bool = False,
        style: str = "",
        is_path: bool = False,
    ) -> None:
        if row < 0 or row >= self.layout.rows or col < 0 or col >= self.layout.cols:
            return
        mark = self.marks[(row, col)]
        mark["left"] |= left
        mark["right"] |= right
        mark["up"] |= up
        mark["down"] |= down
        mark["is_path"] |= is_path
        self._record_style(row, col, style, is_path)

    def paint_edge(self, edge: Edge, layout_data: _CanvasLayout, states: dict[str, str]) -> None:
        on_path = edge.source in layout_data.critical_path and edge.target in layout_data.critical_path
        style = "cyan" if on_path else _edge_style(states.get(edge.source, ""), states.get(edge.target, ""))
        waypoints = _edge_waypoints(edge, self.layout, layout_data.ranks, layout_data.columns)
        for index in range(len(waypoints) - 1):
            self._connect_waypoints(waypoints[index], waypoints[index + 1], style, on_path)

    def render_edges(self) -> None:
        for (row, col), mark in self.marks.items():
            if row < 0 or row >= self.layout.rows or col < 0 or col >= self.layout.cols:
                continue
            glyph = _glyph_for_segment(mark["left"], mark["right"], mark["up"], mark["down"])
            if self.grid[row][col].glyph == " ":
                self.grid[row][col] = Cell(glyph=glyph, style=self.styles.get((row, col), ""))

    def _record_style(self, row: int, col: int, style: str, is_path: bool) -> None:
        prev = self.styles.get((row, col), "")
        if is_path:
            self.styles[(row, col)] = "cyan"
        elif (style == "red" or prev != "red") and not (prev == "cyan" and style == "dim"):
            self.styles[(row, col)] = style or prev

    def _connect_waypoints(
        self, first: tuple[int, int], second: tuple[int, int], style: str, on_path: bool
    ) -> None:
        row1, col1 = first
        row2, col2 = second
        if self.layout.rank_gap > 0:
            for row in range(row1 + 1, row2):
                self.mark(row, col1, up=True, down=True, style=style, is_path=on_path)
        self.mark(row1, col1, down=True, style=style, is_path=on_path)
        self._connect_horizontal(row2, col1, col2, style, on_path)
        self.mark(row2, col2, up=True, style=style, is_path=on_path)

    def _connect_horizontal(self, target_row: int, col1: int, col2: int, style: str, on_path: bool) -> None:
        if col1 == col2:
            return
        horiz_row = target_row - 1 if self.layout.rank_gap > 0 else target_row
        lo, hi = min(col1, col2), max(col1, col2)
        for col in range(lo, hi + 1):
            self.mark(
                horiz_row,
                col,
                left=col > lo,
                right=col < hi,
                style=style,
                is_path=on_path,
            )
        self.mark(horiz_row, col1, up=True, right=col2 > col1, left=col2 < col1, style=style, is_path=on_path)
        self.mark(
            horiz_row, col2, down=True, right=col2 < col1, left=col2 > col1, style=style, is_path=on_path
        )


@dataclass
class _CanvasLayout:
    keep: set[str] | None
    layout: _Layout
    ranks: dict[str, int]
    columns: dict[str, int]
    critical_path: set[str]


def _edge_waypoints(
    edge: Edge,
    layout: _Layout,
    ranks_map: dict[str, int],
    columns_map: dict[str, int],
) -> list[tuple[int, int]]:
    waypoints = [(layout.row_of_rank(ranks_map[edge.source]), layout.col_of(columns_map[edge.source]) + 1)]
    waypoints.extend((layout.row_of_rank(row), layout.col_of(col) + 1) for row, col in edge.cells)
    waypoints.append(
        (layout.row_of_rank(ranks_map[edge.target]), layout.col_of(columns_map[edge.target]) + 1)
    )
    return waypoints


def _build_canvas_layout(
    dag: DAG,
    ranks_map: dict[str, int],
    columns_map: dict[str, int],
    states: dict[str, str],
    filt: Filter | None,
    rank_gap: int,
    critical_path: set[str] | None,
) -> _CanvasLayout | None:
    keep = _filtered_ids(dag, states, filt)
    if not ranks_map:
        return None
    n_cols_per_rank: dict[int, int] = defaultdict(int)
    for node_id, rank in ranks_map.items():
        if keep is None or node_id in keep:
            n_cols_per_rank[rank] = max(n_cols_per_rank[rank], columns_map[node_id] + 1)
    layout = _Layout(
        n_ranks=max(ranks_map.values()) + 1,
        n_cols=max(n_cols_per_rank.values(), default=0),
        rank_gap=rank_gap,
    )
    if layout.rows == 0 or layout.cols == 0:
        return None
    return _CanvasLayout(keep, layout, ranks_map, columns_map, critical_path or set())


def _paint_nodes(
    dag: DAG,
    grid: list[list[Cell]],
    layout_data: _CanvasLayout,
    *,
    states: dict[str, str],
    anchor: str | None,
) -> None:
    for node_id, node in dag.nodes.items():
        if layout_data.keep is not None and node_id not in layout_data.keep:
            continue
        row = layout_data.layout.row_of_rank(layout_data.ranks[node_id])
        col = layout_data.layout.col_of(layout_data.columns[node_id])
        state = states.get(node_id, "")
        style = "cyan" if node_id in layout_data.critical_path else _node_style(state)
        label = _node_label(node_id, state, anchor)
        for index, char in enumerate(label):
            if col + index < layout_data.layout.cols:
                grid[row][col + index] = Cell(glyph=char, style=style)
        _ = node.milestone


def _node_label(node_id: str, state: str, anchor: str | None) -> str:
    glyph = _node_glyph(state)
    label = f"{glyph}{node_id}"[:NODE_W]
    if anchor == node_id and len(label) + 2 <= NODE_W:
        return f"[{glyph}{node_id}]"[:NODE_W]
    return label


def ascii_canvas(
    dag: DAG,
    ranks_map: dict[str, int],
    columns_map: dict[str, int],
    edges_list: list[Edge],
    *,
    states: dict[str, str],
    filter: Filter | None = None,
    anchor: str | None = None,
    rank_gap: int = DEFAULT_RANK_GAP,
    critical_path: set[str] | None = None,
) -> list[list[Cell]]:
    """Produce a 2D grid of `Cell`s for the whole DAG.

    `states` maps node-id → state string ('' or absent = unseeded).
    `filter` restricts which nodes (and incident edges) are drawn.
    `critical_path` (if given) is a set of node ids to render in cyan
    along with their incident edges.
    """
    layout_data = _build_canvas_layout(dag, ranks_map, columns_map, states, filter, rank_gap, critical_path)
    if layout_data is None:
        return []
    grid: list[list[Cell]] = [
        [Cell() for _ in range(layout_data.layout.cols)] for _ in range(layout_data.layout.rows)
    ]
    painter = _EdgePainter(layout_data.layout, grid)
    for edge in edges_list:
        if layout_data.keep is not None and (
            edge.source not in layout_data.keep or edge.target not in layout_data.keep
        ):
            continue
        painter.paint_edge(edge, layout_data, states)
    painter.render_edges()
    _paint_nodes(dag, grid, layout_data, states=states, anchor=anchor)
    return grid


def grid_to_lines(grid: list[list[Cell]]) -> list[str]:
    """Render a grid to a list of plain strings (no markup). Test helper."""
    return ["".join(c.glyph for c in row) for row in grid]


def grid_to_markup(grid: list[list[Cell]]) -> list[str]:
    """Render a grid to a list of textual-markup lines. Used by widget."""
    out: list[str] = []
    for row in grid:
        chunks: list[str] = []
        cur_style = ""
        cur_text: list[str] = []
        for cell in row:
            if cell.style != cur_style:
                if cur_text:
                    if cur_style:
                        chunks.append(f"[{cur_style}]" + "".join(cur_text) + f"[/{cur_style}]")
                    else:
                        chunks.append("".join(cur_text))
                    cur_text = []
                cur_style = cell.style
            cur_text.append(cell.glyph)
        if cur_text:
            if cur_style:
                chunks.append(f"[{cur_style}]" + "".join(cur_text) + f"[/{cur_style}]")
            else:
                chunks.append("".join(cur_text))
        out.append("".join(chunks))
    return out
