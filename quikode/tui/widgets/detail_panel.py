"""Detail panel — drill-in for the selected task.

Two tabs: Subtasks (DataTable with active-subtask highlight) and Agent calls
(RichLog with newest-at-bottom). Tab cycles between them.

Phase status line at the top shows what the agent is actually doing right
now — important when the worker drops into whole-spec fixup (state=DOING,
no subtask_id on the call). Without it, the subtasks tab is frozen at the
last per-subtask states and the user can't tell anything's running.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from textual.app import ComposeResult
from textual.containers import Container
from textual.widgets import DataTable, RichLog, Static, TabbedContent, TabPane

# Diagnostic logger for the 2026-05-04 missing-rows bug. Enabled by setting
# `QUIKODE_TUI_DEBUG=1` in the env. Logs per-render snapshot/row counts to
# /tmp/quikode-tui.log so we can compare what the snapshot carries vs. what
# the DataTable actually ends up with after the bulk add.
_log = logging.getLogger("quikode.tui.detail_panel")
if os.environ.get("QUIKODE_TUI_DEBUG"):
    _h = logging.FileHandler("/tmp/quikode-tui.log", mode="a")
    _h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _log.addHandler(_h)
    _log.setLevel(logging.DEBUG)


@dataclass(frozen=True)
class SubtaskRowSnapshot:
    subtask_id: str
    title: str
    state: str  # SubtaskState value
    retries: int


@dataclass(frozen=True)
class DetailSnapshot:
    task_id: str
    title: str = ""  # the parent task's title
    plan_summary: str = ""
    subtasks: list[SubtaskRowSnapshot] = field(default_factory=list)
    agent_calls: list[str] = field(default_factory=list)
    # Index into `subtasks` of the currently-active subtask (the one whose
    # state is doing/checking/triaging). -1 if no subtask is active.
    active_subtask_idx: int = -1
    # Phase context — task's current state + the most recent state_log note
    # + how long it's been there. Lets the detail panel surface "whole-spec
    # fixup attempt 1 · 32m in" so the user can tell something's running
    # even when no subtask is in flight.
    task_state: str = ""
    last_state_note: str = ""
    in_state_for: str = ""
    last_worktree_edit: str = ""
    # v3 review-loop context — populated when the task is in
    # responding_to_review so the phase line can show "round N · M threads"
    # without the user having to drill into the DB. Both default to None
    # for older states / pre-v3 tasks.
    review_round: int | None = None
    review_threads_count: int | None = None


class DetailPanel(Container):
    """Tabbed pane: subtasks / agent calls.

    Re-rendering each poll tick (1s) caused visible flicker because
    DataTable.clear() + re-add_row() forces a full repaint each time.
    Same for RichLog.clear() + re-write(). To avoid the flash we fingerprint
    the snapshot's data and only re-render when the fingerprint changes.
    """

    DEFAULT_CSS = ""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Fingerprint of the last rendered snapshot; tuple of immutable values
        # so equality is fast.
        self._subtasks_fp: tuple = ()
        self._calls_fp: tuple = ()
        self._phase_fp: tuple = ()
        self._last_task_id: str | None = None
        # Tracks whether the user has manually moved the subtasks cursor.
        # While False, render_snapshot auto-follows the active subtask so
        # newly-appended fixup rows (which can fall below the visible
        # viewport) stay in view. Set True by the App when the user
        # arrow-keys the cursor; reset on task selection change.
        self._user_moved_subtask_cursor = False

    def compose(self) -> ComposeResult:
        yield Static("", id="detail-phase")
        with TabbedContent(initial="subtasks-tab", id="detail-tabs"):
            with TabPane("Subtasks", id="subtasks-tab"):
                yield DataTable(id="subtasks-table", zebra_stripes=True, cursor_type="row")
            with TabPane("Agent calls", id="calls-tab"):
                yield RichLog(id="calls-log", markup=True, wrap=False, max_lines=200)

    def on_mount(self) -> None:
        # Set up the subtasks DataTable columns once.
        st = self.query_one("#subtasks-table", DataTable)
        st.add_columns("Subtask", "State", "Retries", "Title")

    def render_snapshot(self, snap: DetailSnapshot) -> None:
        # Reset fingerprints when the selected task changes — we want a fresh
        # render even if the new task happens to have the same row count.
        if snap.task_id != self._last_task_id:
            self._subtasks_fp = ()
            self._calls_fp = ()
            self._phase_fp = ()
            self._user_moved_subtask_cursor = False
            self._last_task_id = snap.task_id

        # ---- Phase status line ----
        # in_state_for re-stringifies every tick (incrementing seconds), so
        # the fingerprint includes it and the line refreshes once per second.
        # The cost is one Static.update() — way cheaper than the DataTable
        # rebuild that the fingerprints below skip.
        phase_fp = (snap.task_state, snap.last_state_note, snap.in_state_for, snap.last_worktree_edit)
        if phase_fp != self._phase_fp:
            self.query_one("#detail-phase", Static).update(_phase_line(snap))
            self._phase_fp = phase_fp

        # ---- Subtasks tab ----
        sub_fp = tuple((s.subtask_id, s.state, s.retries) for s in snap.subtasks)
        if sub_fp != self._subtasks_fp:
            st = self.query_one("#subtasks-table", DataTable)
            # Preserve cursor across re-render so it doesn't flick to row 0
            # every time a subtask state changes.
            prev_cursor_row = st.cursor_coordinate.row
            st.clear()
            # Add rows one at a time WITH explicit row keys so textual's
            # internal state survives rapid update cycles. Live-observed
            # bug: bulk `add_row()` calls in a tight loop occasionally lost
            # the last 1-2 rows (R-0002 18 subtasks → only 16 rendered)
            # — root cause was textual DataTable's internal row registry
            # racing with the next render tick. The `key=` argument forces
            # the row to register synchronously by subtask_id, ensuring
            # every add_row commits before the loop continues.
            add_errors: list[str] = []
            for sub in snap.subtasks:
                state_cell = _subtask_state_cell(sub.state)
                try:
                    st.add_row(
                        sub.subtask_id,
                        state_cell,
                        str(sub.retries),
                        sub.title[:60],
                        key=sub.subtask_id,
                    )
                except Exception as e:
                    # Defensive: if a duplicate-key error somehow surfaces
                    # (key collision after clear), skip that row rather than
                    # let one bad add abort the loop and leave a partial
                    # table on screen. Log so the underlying issue surfaces
                    # in /tmp/quikode-tui.log when QUIKODE_TUI_DEBUG=1.
                    add_errors.append(f"{sub.subtask_id}:{type(e).__name__}:{e}")
            if _log.isEnabledFor(logging.DEBUG):
                try:
                    rendered = st.row_count
                except Exception:
                    rendered = -1
                _log.debug(
                    "render_snapshot %s: snapshot=%d rendered=%d add_errors=%d %s",
                    snap.task_id,
                    len(snap.subtasks),
                    rendered,
                    len(add_errors),
                    ("; ".join(add_errors)[:500]) if add_errors else "",
                )
            # Auto-follow the active subtask UNLESS the user has manually
            # moved the cursor (in which case respect their position). When
            # fixup decomposition appends new subtasks past the visible
            # viewport (e.g. F-7-* on R-0002 fell below screen), the user
            # would otherwise have no idea they exist.
            #
            # NOTE: do NOT call `scroll_to(y=...)` after `add_row()` in a
            # tight loop — Textual's DataTable processes row additions
            # asynchronously, and the explicit scroll fires before all
            # rows are registered, leaving the viewport anchored at a
            # stale row index (observed live: F-7-1/F-7-2 invisible on
            # R-0002 even though all 18 rows were in the snapshot).
            # `move_cursor` triggers a natural scroll-into-view via
            # Textual's cursor-follow logic on the next render pass,
            # which is sequenced correctly with the row additions.
            if snap.subtasks:
                if self._user_moved_subtask_cursor:
                    target = min(prev_cursor_row, len(snap.subtasks) - 1)
                elif snap.active_subtask_idx >= 0:
                    target = snap.active_subtask_idx
                else:
                    # Default to last row when no active — usually the most
                    # recently-appended fixup subtask.
                    target = len(snap.subtasks) - 1
                try:
                    st.move_cursor(row=max(target, 0), column=0, animate=False)
                except Exception:
                    pass
            # Belt-and-suspenders: explicitly mark the table dirty so
            # Textual's next render pass sees the updated row set.
            try:
                st.refresh(layout=True)
            except Exception:
                pass
            self._subtasks_fp = sub_fp

        # ---- Agent calls tab ----
        calls_fp = tuple(snap.agent_calls)
        if calls_fp != self._calls_fp:
            calls = self.query_one("#calls-log", RichLog)
            calls.clear()
            if snap.agent_calls:
                for line in reversed(snap.agent_calls):
                    calls.write(line)
            else:
                calls.write("[dim]no agent calls yet[/]")
            self._calls_fp = calls_fp

    def cycle_tab(self, direction: int = 1) -> None:
        tabs = self.query_one("#detail-tabs", TabbedContent)
        order = ["subtasks-tab", "calls-tab"]
        cur = tabs.active or order[0]
        idx = order.index(cur) if cur in order else 0
        tabs.active = order[(idx + direction) % len(order)]


def _subtask_state_cell(state: str) -> str:
    """Color-code subtask states for at-a-glance scanning."""
    palette = {
        "done": "green",
        "doing": "cyan",
        "checking": "cyan",
        "triaging": "yellow",
        "blocked": "red",
        "skipped": "dim",
        "pending": "dim",
    }
    color = palette.get(state, "")
    return f"[{color}]{state}[/]" if color else state


# State values where work is happening at the workspace level (no per-subtask
# row to highlight). For these the subtasks tab can be misleading — surface
# the phase explicitly.
_WHOLE_SPEC_STATES = {
    "doing",
    "checking",
    "triaging",
    "final_checking",
    "replanning",
    # v3 states where the worker takes the wheel for the whole spec — the
    # subtasks tab is frozen until the task either commits + pushes or
    # transitions back to AWAITING_MERGE.
    "responding_to_review",
    "rebasing_to_main",
}


def _phase_line(snap: DetailSnapshot) -> str:
    """One-line summary of what the agent is doing right now.

    Examples:
      R-0001 · doing_subtask · S-07-mcp-tools attempt 2 · in-state 47s · edit 12s ago
      R-0001 · doing · whole-spec fixup attempt 1 · in-state 32m · edit 42s ago
      R-0001 · awaiting_merge · #57 green · in-state 14m
    """
    if not snap.task_id or snap.task_id == "(none)":
        return "[dim]No task selected — highlight a row above and press Enter.[/]"
    state = snap.task_state or "?"
    note = snap.last_state_note
    in_state = snap.in_state_for
    edit = snap.last_worktree_edit

    color = _phase_color(state)
    parts = [f"[b]{snap.task_id}[/]"]
    if snap.title:
        parts.append(f"[dim]{snap.title[:60]}[/]")
    parts.append(f"[{color}]{state}[/]")
    # State-specific extras: review-loop context (round / threads) takes
    # priority over the generic state_log note when responding_to_review,
    # since "round 3 · 2 threads" is more useful at-a-glance than a
    # truncated transition note.
    if state == "responding_to_review":
        if snap.review_round is not None and snap.review_threads_count is not None:
            parts.append(f"round [b]{snap.review_round}[/] · [b]{snap.review_threads_count}[/] threads")
        else:
            parts.append("[dim]responding to review feedback[/]")
    elif note:
        parts.append(f"[dim]{note[:80]}[/]")
    if in_state:
        parts.append(f"in-state [b]{in_state}[/]")
    if edit and state in _WHOLE_SPEC_STATES | {"doing_subtask", "checking_subtask", "triaging_subtask"}:
        parts.append(f"edit [b]{edit}[/]")
    line = "  ·  ".join(parts)
    # Trailing notes — explain "why does the subtasks tab look frozen?" for
    # the operator. AWAITING_MERGE gets its own copy: the worker has fully
    # released this task, the daemon is the one polling.
    if state == "awaiting_merge":
        line += "\n[dim italic](no action required — auto-polling for reviews + merge)[/]"
    elif state in _WHOLE_SPEC_STATES:
        line += "\n[dim italic](whole-spec phase — subtask states below are frozen until this returns)[/]"
    return line


def _phase_color(state: str) -> str:
    if state in {"awaiting_merge"}:
        return "green"
    if state in {"merged"}:
        return "green"
    if state in {"blocked", "failed", "aborted"}:
        return "red"
    if state == "responding_to_review":
        # Cyan to match other "agent actively working" states. Distinct from
        # rebasing (yellow) so a glance differentiates "fixing review" from
        # "untangling git" at the phase-line level.
        return "cyan"
    if state == "rebasing_to_main":
        return "yellow"
    if state in _WHOLE_SPEC_STATES | {"doing_subtask", "checking_subtask"}:
        return "cyan"
    if state in {"triaging_subtask", "rebasing", "intent_reviewing", "conflict_resolving"}:
        return "yellow"
    return "white"
