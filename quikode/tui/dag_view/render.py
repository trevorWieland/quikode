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
    "awaiting_merge": "yellow",
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
    "final_checking",
    "committing",
    "pushing",
    "pr_opening",
    "polling_ci",
    "rebasing",
    "conflict_resolving",
    "intent_reviewing",
    "replanning",
    "responding_to_review",
    "rebasing_to_main",
}

# State -> single-character glyph painted at the start of the node cell.
_STATE_GLYPH: dict[str, str] = {
    "merged": "✓",
    "awaiting_merge": "⏸",
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
        best = max((best_up(d) for d in deps), key=len)
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
    keep = _filtered_ids(dag, states, filter)
    if not ranks_map:
        return []
    n_ranks = max(ranks_map.values()) + 1
    n_cols_per_rank: dict[int, int] = defaultdict(int)
    for nid, r in ranks_map.items():
        if keep is not None and nid not in keep:
            continue
        n_cols_per_rank[r] = max(n_cols_per_rank[r], columns_map[nid] + 1)
    n_cols = max(n_cols_per_rank.values(), default=0)
    layout = _Layout(n_ranks=n_ranks, n_cols=n_cols, rank_gap=rank_gap)
    if layout.rows == 0 or layout.cols == 0:
        return []
    grid: list[list[Cell]] = [[Cell() for _ in range(layout.cols)] for _ in range(layout.rows)]

    # Track which cells get marked by edges; we coalesce glyphs per cell.
    edge_marks: dict[tuple[int, int], dict[str, bool]] = defaultdict(
        lambda: {"left": False, "right": False, "up": False, "down": False, "is_path": False}
    )
    edge_styles: dict[tuple[int, int], str] = {}

    def _mark(rr: int, cc: int, *, left=False, right=False, up=False, down=False, style="", is_path=False):
        if rr < 0 or rr >= layout.rows or cc < 0 or cc >= layout.cols:
            return
        m = edge_marks[(rr, cc)]
        m["left"] |= left
        m["right"] |= right
        m["up"] |= up
        m["down"] |= down
        m["is_path"] |= is_path
        # Highest-priority style wins: critical path > red > cyan > dim > default.
        prev = edge_styles.get((rr, cc), "")
        if is_path:
            edge_styles[(rr, cc)] = "cyan"
        elif (style == "red" or prev != "red") and not (prev == "cyan" and style == "dim"):
            edge_styles[(rr, cc)] = style or prev

    # 1) Paint edges first (so node cells overwrite any glyph collisions).
    for e in edges_list:
        if keep is not None and (e.source not in keep or e.target not in keep):
            continue
        ss = states.get(e.source, "")
        ts = states.get(e.target, "")
        on_path = bool(critical_path) and e.source in critical_path and e.target in critical_path
        style = "cyan" if on_path else _edge_style(ss, ts)

        sr, sc = ranks_map[e.source], columns_map[e.source]
        tr, tc = ranks_map[e.target], columns_map[e.target]
        # Build the chain of (row, col) waypoints.
        waypoints: list[tuple[int, int]] = []
        # Start: just below the source node cell
        waypoints.append((layout.row_of_rank(sr), layout.col_of(sc) + 1))
        # Intermediate dummy cells (long-edge midpoints)
        for rr_intermed, cc_intermed in e.cells:
            waypoints.append((layout.row_of_rank(rr_intermed), layout.col_of(cc_intermed) + 1))
        waypoints.append((layout.row_of_rank(tr), layout.col_of(tc) + 1))

        # Connect each waypoint pair: drop down rank_gap rows between, then
        # slide horizontally to the target column. We mark "down" exits and
        # "up" entrances on node rows, and horizontal sliding on the gap row.
        for i in range(len(waypoints) - 1):
            r1, c1 = waypoints[i]
            r2, c2 = waypoints[i + 1]
            # Drop from r1 to r1+rank_gap (vertical column of '│')
            if rank_gap > 0:
                for rr in range(r1 + 1, r2):
                    _mark(rr, c1, up=True, down=True, style=style, is_path=on_path)
            # Mark exit from source node row (down)
            _mark(r1, c1, down=True, style=style, is_path=on_path)
            # Horizontal slide on row r2 - 1 if columns differ
            if c1 != c2:
                horiz_row = r2 - 1 if rank_gap > 0 else r2
                lo, hi = min(c1, c2), max(c1, c2)
                for cc in range(lo, hi + 1):
                    is_left = cc > lo
                    is_right = cc < hi
                    _mark(horiz_row, cc, left=is_left, right=is_right, style=style, is_path=on_path)
                # corners: at lo end and hi end the vertical drop joins horizontal
                # (already marked via the vertical loop's marks)
                # Mark the corner where vertical from c1 meets horizontal:
                #   above: c1 going down; below: horizontal extending toward c2
                _mark(
                    horiz_row,
                    c1,
                    up=True,
                    right=(c2 > c1),
                    left=(c2 < c1),
                    style=style,
                    is_path=on_path,
                )
                _mark(
                    horiz_row,
                    c2,
                    down=True,
                    right=(c2 < c1),
                    left=(c2 > c1),
                    style=style,
                    is_path=on_path,
                )
            # Mark entry to target node (up)
            _mark(r2, c2, up=True, style=style, is_path=on_path)

    # Render edge cells from marks.
    for (rr, cc), m in edge_marks.items():
        if rr < 0 or rr >= layout.rows or cc < 0 or cc >= layout.cols:
            continue
        glyph = _glyph_for_segment(m["left"], m["right"], m["up"], m["down"])
        if grid[rr][cc].glyph == " ":  # only paint where empty
            grid[rr][cc] = Cell(glyph=glyph, style=edge_styles.get((rr, cc), ""))

    # 2) Paint nodes (overwrites any conflicting edge glyphs at their cell).
    for nid, n in dag.nodes.items():
        if keep is not None and nid not in keep:
            continue
        rr = layout.row_of_rank(ranks_map[nid])
        cc = layout.col_of(columns_map[nid])
        st = states.get(nid, "")
        on_path = bool(critical_path) and nid in critical_path
        style = "cyan" if on_path else _node_style(st)
        glyph = _node_glyph(st)
        # Compose label: glyph + space + id, truncated to NODE_W.
        label = f"{glyph}{nid}"[:NODE_W]
        # Highlight anchor with brackets if it fits; else accept the truncation.
        if anchor == nid and len(label) + 2 <= NODE_W:
            label = f"[{glyph}{nid}]"[:NODE_W]
        for i, ch in enumerate(label):
            if cc + i < layout.cols:
                grid[rr][cc + i] = Cell(glyph=ch, style=style)
        # Mark milestone (used optionally by the `m` overlay) — n.milestone
        # is read here but we don't draw boxes in v1. The screen widget
        # can read this back from the layout + columns_map if it wants to.
        _ = n.milestone

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
