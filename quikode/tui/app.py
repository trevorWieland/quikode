"""Root Textual app — wires panels, polls store, dispatches slash commands.

All eight v1 build steps land here: skeleton (1), live SQLite reader (2),
activity + resources (3), detail panel (4), slash dispatch + autocomplete
(5), orchestrator subprocess control (6), settings modal (7), polish (8).
"""

from __future__ import annotations

import sys
from dataclasses import replace
from importlib import resources
from pathlib import Path
from typing import ClassVar

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable

from quikode.config_loader import load_config
from quikode.state import Store

from .controllers import orchestrator_control
from .controllers.command_dispatch import dispatch as dispatch_slash
from .controllers.slash_catalog import SLASH_CATALOG
from .controllers.store_polls import PollSnapshot, StorePoller
from .dag_view.screen import DAGScreen
from .widgets.activity_feed import ActivityFeed
from .widgets.command_bar import CommandBar
from .widgets.detail_panel import DetailPanel
from .widgets.header import WorkspaceHeader
from .widgets.resources_panel import ResourcesPanel
from .widgets.tasks_table import TasksTable

POLL_INTERVAL_S = 1.0


class QuikodeTUI(App):
    """Mission control. Polls SQLite each tick and re-renders panels."""

    CSS_PATH = "styles/quikode.tcss"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit_with_confirm", "Quit", show=True),
        Binding("ctrl+c", "quit_with_confirm", "Quit", show=False),
        Binding("/", "focus_command_bar", "Command", show=True),
        Binding(":", "focus_command_bar", "Command", show=False),
        Binding("?", "show_help", "Help", show=True),
        Binding("tab", "cycle_detail_tab", "Cycle detail tab", show=False),
        Binding("r", "noop_action('retry')", "Retry", show=True),
        Binding("a", "noop_action('abort')", "Abort", show=True),
        Binding("o", "noop_action('open-pr')", "Open PR", show=True),
        Binding("d", "noop_action('export')", "Dump", show=False),
        Binding("t", "noop_action('tail')", "Tail", show=False),
        Binding("v", "noop_action('view-plan')", "View plan", show=False),
        Binding("e", "noop_action('explain')", "Explain", show=False),
        Binding("m", "noop_action('mark-merged')", "Mark merged", show=False),
        Binding("period", "force_refresh", "Refresh", show=False),
        Binding("comma", "open_settings", "Settings", show=True),
        Binding("g", "open_dag_view", "DAG view", show=True),
    ]

    def __init__(self, workspace: Path | None = None, *, poll_interval_s: float = POLL_INTERVAL_S) -> None:
        super().__init__()
        self.workspace = workspace or Path.cwd()
        self.poller = StorePoller(self.workspace)
        self._selected_task_id: str | None = None
        self._poll_interval_s = poll_interval_s

    def compose(self) -> ComposeResult:
        yield WorkspaceHeader(id="header-bar")
        # Proportional split — left column (3fr) gets tasks + detail, right column
        # (2fr) gets activity + resources. Each child uses `1fr` / `2fr` weights
        # in CSS so the layout adapts to any terminal size. No fixed heights;
        # no composition-order coupling to a grid template (the previous layout
        # required compose() order to match `grid-rows: auto 2fr 12` exactly).
        with Horizontal(id="main-split"):
            with Vertical(id="main-left"):
                yield TasksTable(id="tasks-panel")
                yield DetailPanel(id="detail-panel")
            with Vertical(id="main-right"):
                yield ActivityFeed(id="activity-panel")
                yield ResourcesPanel(id="resources-panel")
        yield CommandBar(SLASH_CATALOG, on_submit=self._dispatch_slash, id="command-bar")

    def on_mount(self) -> None:
        self.title = "quikode · mission control"
        self.theme = "textual-dark"
        self.refresh_now()
        self.set_interval(self._poll_interval_s, self.refresh_now)

    def on_unmount(self) -> None:
        self.poller.close()

    # ----- polling + rendering -----

    def refresh_now(self) -> None:
        # The widgets live on the App's main screen. If a modal is on top,
        # or the App is mid-teardown, the panels may not be reachable via
        # `self.query_one(...)` (which searches the active screen). Skip
        # rendering rather than crash.
        try:
            self.query_one("#header-bar", WorkspaceHeader)
        except Exception:  # widgets not mounted yet, or modal screen up
            return
        snap = self.poller.poll(selected_task_id=self._selected_task_id)
        try:
            oc_status = orchestrator_control.status(self.workspace)
            running = oc_status.running
            heartbeat_age = oc_status.heartbeat_age_s
            heartbeat_stale = oc_status.heartbeat_stale
        except OSError:
            running = False
            heartbeat_age = None
            heartbeat_stale = False
        snap = replace(
            snap,
            header=replace(
                snap.header,
                orchestrator_running=running,
                heartbeat_age_s=heartbeat_age,
                heartbeat_stale=heartbeat_stale,
            ),
        )
        self._render(snap)

    def _render(self, snap: PollSnapshot) -> None:
        try:
            self.query_one("#header-bar", WorkspaceHeader).render_snapshot(snap.header)
            self.query_one("#tasks-panel", TasksTable).render_rows(snap.tasks)
            self.query_one("#activity-panel", ActivityFeed).render_entries(snap.activity)
            self.query_one("#resources-panel", ResourcesPanel).render_snapshot(snap.resources)
            self.query_one("#detail-panel", DetailPanel).render_snapshot(snap.detail)
        except Exception:
            # During modal-active or teardown, query_one may fail to find a
            # panel. Ignore — the next tick will catch up once the main
            # screen is current again.
            return

    # ----- selection wiring (step 4) -----

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Update the cached selection. The next poll tick re-renders the detail
        # panel — calling refresh_now() here would re-enter rendering and risk
        # a recursive cycle when render_rows() auto-selects the first row.
        #
        # Filter by source widget id: the subtasks DataTable inside the detail
        # panel also emits highlights as its cursor moves during re-renders.
        # Without this guard, those events overwrite _selected_task_id with a
        # subtask id, making the next poll find no matching task row and the
        # phase line flicker to "S-… · ?".
        if event.control.id != "tasks-panel":
            return
        if event.row_key and event.row_key.value:
            self._selected_task_id = str(event.row_key.value)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Enter on a row → repopulate the detail panel right now (don't wait
        # for the next 1s poll tick). Same filter as RowHighlighted: we only
        # care about selections in the tasks panel.
        if event.control.id != "tasks-panel":
            return
        if event.row_key and event.row_key.value:
            self._selected_task_id = str(event.row_key.value)
            self.refresh_now()

    # ----- actions -----

    def action_focus_command_bar(self) -> None:
        self.query_one("#command-bar", CommandBar).query_one("#command-input").focus()

    def action_show_help(self) -> None:
        feed = self.query_one("#activity-panel", ActivityFeed)
        feed.write("[b]help[/] · slash commands → see /help; full keymap → /keybindings")

    def action_cycle_detail_tab(self) -> None:
        self.query_one("#detail-panel", DetailPanel).cycle_tab(direction=1)

    def action_quit_with_confirm(self) -> None:
        msg = orchestrator_control.parting_status_message(self.workspace)
        self.exit(message=msg)

    def action_noop_action(self, slug: str) -> None:
        feed = self.query_one("#activity-panel", ActivityFeed)
        feed.write(f"[dim]({slug} key-binding not yet wired)[/]")

    def action_force_refresh(self) -> None:
        self.refresh_now()

    def action_open_settings(self) -> None:
        # Use the dispatcher so behavior matches `/settings` slash command.
        self._dispatch_slash("/settings")

    def action_open_dag_view(self) -> None:
        def _on_close(selected: str | None) -> None:
            if selected:
                self._selected_task_id = selected
                self.refresh_now()

        self.push_screen(DAGScreen(workspace=self.workspace), _on_close)

    # ----- slash dispatch -----

    def _dispatch_slash(self, raw: str) -> None:
        dispatch_slash(self, raw)

    def on_key(self, event: events.Key) -> None:
        _ = event


def run_tui(workspace: Path | None = None) -> None:
    """Entrypoint used by the `quikode tui` CLI command."""
    ws = workspace or Path.cwd()
    # The poller opens SQLite read-only — older workspaces predate v2 columns
    # and the poller's column-explicit SELECTs would crash. Open the writer
    # store once on entry to trigger _migrate(), then close. Quiet on no-config
    # workspaces so the TUI still launches and shows its "no workspace" state.
    try:
        cfg = load_config(ws)
        db_path = cfg.state_dir / "quikode.db"
        if db_path.exists():
            Store(db_path).conn.close()
    except FileNotFoundError:
        pass

    css = resources.files(__package__).joinpath("styles/quikode.tcss")
    QuikodeTUI.CSS_PATH = str(css)
    app = QuikodeTUI(workspace=ws)
    result = app.run()
    msg = orchestrator_control.parting_status_message(ws)
    if msg and result is None:
        sys.stderr.write(msg + "\n")
