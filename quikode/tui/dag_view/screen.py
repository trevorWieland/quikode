"""DAGScreen — the textual ModalScreen that hosts the DAG viewer.

Composition:
- left:  `DAGGraph` (scrollable ASCII canvas)
- right: `StatsSidebar` (formatted HeadlineStats)
- bottom: a CommandBar instance for slash commands (/filter, /anchor, /clear)

Polling: subscribes via the same SQLite-backed store-poll pattern as the
main dashboard but at 10s cadence. The DAG topology + states change slowly
so we don't need 1s ticks here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, cast

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Static

from quikode.config_loader import load_config
from quikode.dag import DAG
from quikode.state import Store

from ..controllers.slash_catalog import SLASH_CATALOG
from ..widgets.command_bar import CommandBar
from .layout import columns, edges, ranks
from .render import (
    DEFAULT_RANK_GAP,
    Filter,
    ascii_canvas,
    critical_path_from,
    grid_to_markup,
)
from .stats import HeadlineStats, compute_headline_stats

if TYPE_CHECKING:
    pass

DAG_POLL_INTERVAL_S = 10.0


@dataclass
class _ViewState:
    """Mutable view state — pan offset, zoom, anchor, filter, overlays."""

    pan_x: int = 0
    pan_y: int = 0
    zoom: int = 1  # 0 = compact, 1 = default, 2 = loose
    anchor: str | None = None
    filter: Filter | None = None
    show_critical_path: bool = False
    show_milestone_overlay: bool = False

    @property
    def rank_gap(self) -> int:
        return [0, DEFAULT_RANK_GAP, 2][self.zoom]


def _format_seconds(s: float | None) -> str:
    if s is None:
        return "—"
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s // 60)}m"
    if s < 86400:
        return f"{int(s // 3600)}h {int((s % 3600) // 60)}m"
    days = int(s // 86400)
    hours = int((s % 86400) // 3600)
    return f"{days}d {hours}h"


def _format_money(usd: float | None) -> str:
    if usd is None:
        return "—"
    return f"${usd:.2f}"


def _format_stats(stats: HeadlineStats) -> str:
    """Build the right-sidebar text block per design doc lines 145-159."""
    lines: list[str] = []
    lines.append(f"[b]Project depth:[/]   {stats.project_depth} ranks")
    lines.append(f"[b]Remaining:[/]       {stats.remaining_depth} ranks")
    lines.append(f"[b]Nodes:[/]           {stats.merged} / {stats.total_nodes} merged")
    lines.append(f"                  {stats.in_flight} in-flight · {stats.ready} ready")
    if stats.behaviors_total:
        lines.append(f"[b]Behaviors:[/]       {stats.behaviors_completed} / {stats.behaviors_total} proven")
    lines.append("")
    lines.append(f"[b]Cost so far:[/]     {_format_money(stats.cost_so_far_usd)}")
    lines.append(f"[b]Avg per R-*:[/]     {_format_money(stats.avg_cost_per_node_usd)}")
    lines.append(f"[b]Projected:[/]       {_format_money(stats.projected_total_usd)}")
    lines.append("")
    lines.append(f"[b]Time per R-*:[/]    {_format_seconds(stats.avg_runtime_per_node_s)}")
    lines.append(f"[b]ETA serial:[/]      {_format_seconds(stats.eta_serial_s)}")
    for n_par, eta_s in sorted(stats.eta_parallel_s.items()):
        lines.append(f"[b]ETA @ N={n_par}:[/]      {_format_seconds(eta_s)}")
    return "\n".join(lines)


class DAGGraph(Widget):
    """Scrollable ASCII canvas for the DAG."""

    DEFAULT_CSS = """
    DAGGraph {
        height: 1fr;
        width: 1fr;
        overflow: hidden;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._lines: list[str] = []
        self._view = _ViewState()

    def set_lines(self, lines: list[str]) -> None:
        self._lines = lines
        self.refresh()

    def set_view(self, view: _ViewState) -> None:
        self._view = view
        self.refresh()

    def render(self) -> Text:
        # Slice the canvas by current pan offset, then render with markup.
        if not self._lines:
            return Text("(empty graph)", style="dim")
        size = self.size
        v = self._view
        visible_lines = self._lines[v.pan_y : v.pan_y + max(1, size.height)]
        # Sliced markup-aware horizontal pan: we strip from the rendered Text.
        text = Text()
        for ln in visible_lines:
            t = Text.from_markup(ln)
            t.truncate(v.pan_x + size.width, overflow="crop")
            t = t[v.pan_x :]
            text.append(t)
            text.append("\n")
        return text


class StatsSidebar(Static):
    """Right-hand sidebar with HeadlineStats."""

    DEFAULT_CSS = """
    StatsSidebar {
        width: 30;
        height: 1fr;
        border-left: solid $primary-darken-2;
        padding: 0 1;
    }
    """


class DAGScreen(ModalScreen[str | None]):
    """Modal pushed via `g` from the dashboard. Returns the cursor task id
    (or None) on dismiss."""

    DEFAULT_CSS = """
    DAGScreen {
        background: $background;
    }

    #dag-body {
        height: 1fr;
        layout: horizontal;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q,escape", "back", "Back", show=True, priority=True),
        Binding("up,k", "pan(0, -1)", "Pan up", show=False, priority=True),
        Binding("down,j", "pan(0, 1)", "Pan down", show=False, priority=True),
        Binding("left,h", "pan(-2, 0)", "Pan left", show=False, priority=True),
        Binding("right,l", "pan(2, 0)", "Pan right", show=False, priority=True),
        Binding("plus,equals_sign", "zoom(1)", "Zoom in", show=False, priority=True),
        Binding("minus", "zoom(-1)", "Zoom out", show=False, priority=True),
        Binding("c", "toggle_critical_path", "Critical path", show=True, priority=True),
        Binding("m", "toggle_milestone_overlay", "Milestones", show=True, priority=True),
        Binding("enter", "select_anchor", "Select", show=True, priority=True),
        Binding("slash", "focus_command_bar", "Command", show=True, priority=True),
    ]

    def __init__(self, workspace: Path) -> None:
        super().__init__()
        self.workspace = workspace
        self._dag: DAG | None = None
        self._store: Store | None = None
        self._ranks: dict[str, int] = {}
        self._cols: dict[str, int] = {}
        self._edges = []
        self._states: dict[str, str] = {}
        self._stats: HeadlineStats | None = None
        self._view = _ViewState()
        self._error: str | None = None
        # Sorted node ids by (rank, col) for Enter-to-select traversal.
        self._cursor_idx = 0
        self._cursor_order: list[str] = []

    def compose(self) -> ComposeResult:
        with Container(id="dag-body"), Horizontal():
            yield DAGGraph(id="dag-graph")
            yield StatsSidebar(id="dag-stats")
        yield CommandBar(SLASH_CATALOG, on_submit=self._dispatch_slash, id="dag-command-bar")

    def on_mount(self) -> None:
        self._load_workspace()
        self._refresh()
        self.set_interval(DAG_POLL_INTERVAL_S, self._refresh)

    # ----- data plumbing -----

    def _load_workspace(self) -> None:
        try:
            cfg = load_config(self.workspace)
        except FileNotFoundError as e:
            self._error = str(e)
            return
        try:
            self._dag = DAG.load(cfg.dag_path)
        except (OSError, ValueError) as e:
            self._error = f"DAG load failed: {e}"
            return
        db_path = cfg.state_dir / "quikode.db"
        if not db_path.exists():
            self._error = f"no SQLite at {db_path}"
            return
        try:
            self._store = Store(db_path)
        except sqlite3.Error as e:
            self._error = f"sqlite open failed: {e}"
            return
        self._ranks = ranks(self._dag)
        self._cols = columns(self._dag, self._ranks)
        self._edges = edges(self._dag, self._ranks, self._cols)
        # Cursor order: by rank, then col, then id.
        self._cursor_order = sorted(
            self._dag.nodes,
            key=lambda nid: (self._ranks[nid], self._cols[nid], nid),
        )
        if self._cursor_order:
            self._view.anchor = self._cursor_order[0]

    def _refresh(self) -> None:
        graph = self.query_one("#dag-graph", DAGGraph)
        sidebar = self.query_one("#dag-stats", StatsSidebar)
        if self._error or not self._dag:
            graph.set_lines([f"[red]{self._error or 'no DAG loaded'}[/]"])
            sidebar.update("[red]workspace not ready[/]")
            return
        assert self._store is not None
        # Refresh task states + stats.
        self._states = {r["id"]: r["state"] for r in self._store.all_tasks()}
        self._stats = compute_headline_stats(self._dag, self._store)
        # Compute critical path if toggled.
        cp: set[str] | None = None
        if self._view.show_critical_path and self._view.anchor:
            cp = critical_path_from(self._dag, self._view.anchor, self._states)
        grid = ascii_canvas(
            self._dag,
            self._ranks,
            self._cols,
            self._edges,
            states=self._states,
            filter=self._view.filter,
            anchor=self._view.anchor,
            rank_gap=self._view.rank_gap,
            critical_path=cp,
        )
        lines = grid_to_markup(grid)
        graph.set_lines(lines)
        graph.set_view(self._view)
        sidebar.update(_format_stats(self._stats))

    # ----- actions -----

    def action_back(self) -> None:
        self.dismiss(None)

    def action_pan(self, dx: int, dy: int) -> None:
        self._view.pan_x = max(0, self._view.pan_x + dx)
        self._view.pan_y = max(0, self._view.pan_y + dy)
        # Move cursor in lockstep on vertical pan so anchor follows view.
        if dy and self._cursor_order:
            self._cursor_idx = max(0, min(len(self._cursor_order) - 1, self._cursor_idx + dy))
            self._view.anchor = self._cursor_order[self._cursor_idx]
        self._refresh()

    def action_zoom(self, delta: int) -> None:
        self._view.zoom = max(0, min(2, self._view.zoom + delta))
        self._refresh()

    def action_toggle_critical_path(self) -> None:
        self._view.show_critical_path = not self._view.show_critical_path
        self._refresh()

    def action_toggle_milestone_overlay(self) -> None:
        # v1 hook — overlay rendering is a v1.1 deliverable. For v1 we
        # just flip the flag so callers can verify the binding fires.
        self._view.show_milestone_overlay = not self._view.show_milestone_overlay
        self._refresh()

    def action_select_anchor(self) -> None:
        self.dismiss(self._view.anchor)

    def action_focus_command_bar(self) -> None:
        self.query_one("#dag-command-bar", CommandBar).query_one("#command-input").focus()

    # ----- slash dispatch (subset specific to the DAG screen) -----

    def _dispatch_slash(self, raw: str) -> None:
        s = raw.strip()
        if not s.startswith("/"):
            return
        body = s[1:].split()
        if not body:
            return
        verb = body[0]
        args = body[1:]
        if verb == "filter" and args:
            kind = args[0]
            if kind in {"all", "blocked", "ready"}:
                self._view.filter = Filter(kind=cast(Literal["all", "blocked", "ready"], kind))
            elif kind == "milestone" and len(args) >= 2:
                self._view.filter = Filter(kind="milestone", milestone=args[1])
            else:
                return
            self._refresh()
        elif verb == "anchor" and args:
            target = args[0]
            if self._dag and target in self._dag.nodes:
                self._view.anchor = target
                if target in self._cursor_order:
                    self._cursor_idx = self._cursor_order.index(target)
                self._refresh()
        elif verb == "clear":
            self._view.filter = None
            self._refresh()
        # Other slash commands are no-ops on this screen.


def open_dag_view(app, on_dismiss: Callable[[str | None], None] | None = None) -> None:
    """Helper: push a DAGScreen and route the dismissal back to `on_dismiss`."""
    app.push_screen(DAGScreen(workspace=app.workspace), on_dismiss)
