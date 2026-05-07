"""Detail panel — drill-in for the selected task.

Two tabs: Subtasks (DataTable with active-subtask highlight) and Agent calls
(RichLog with newest-at-bottom). Tab cycles between them.

Phase status line at the top shows what the agent is actually doing right
now — important when the worker drops into monolithic fixup (state=DOING,
no subtask_id on the call). Without it, the subtasks tab is frozen at the
last per-subtask states and the user can't tell anything's running.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from textual.app import ComposeResult
from textual.containers import Container, VerticalScroll
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
    # + how long it's been there. Lets the detail panel surface "monolithic
    # fixup attempt 1 · 32m in" so the user can tell something's running
    # even when no subtask is in flight.
    task_state: str = ""
    last_state_note: str = ""
    in_state_for: str = ""
    last_worktree_edit: str = ""
    # v3 review-loop context — populated when the task is in
    # addressing_feedback so the phase line can show "round N · M threads"
    # without the user having to drill into the DB. Both default to None
    # for older states / pre-v3 tasks.
    review_round: int | None = None
    review_threads_count: int | None = None
    # v3.6 pre-PR audit gauntlet (4 stages: local_ci, rubric, standards,
    # behavior). Populated from `tasks.pre_pr_audit_summary` JSON. The
    # detail panel renders one row per stage with a pass/fail/queued
    # indicator so the operator can see at a glance what passed and what
    # failed in the most recent cycle. None on tasks that have never
    # entered the pipeline.
    pre_pr_audit_cycle: int | None = None
    pre_pr_audit_stages: list[dict] = field(default_factory=list)


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
        # Plan 26: wrap the phase Static in VerticalScroll so multi-line
        # content (gauntlet block + phase description + container stats +
        # state-long-description) doesn't get clipped at the panel
        # boundary. Without this, the operator sees a truncated phase
        # readout once the content exceeds the panel's allocated height.
        with VerticalScroll(id="detail-phase-scroll"):
            yield Static("", id="detail-phase")
        with TabbedContent(initial="subtasks-tab", id="detail-tabs"):
            with TabPane("Subtasks", id="subtasks-tab"):
                yield DataTable(id="subtasks-table", zebra_stripes=True, cursor_type="row")
            with TabPane("Agent calls", id="calls-tab"):
                # Soft buffer cap (RichLog drops oldest when exceeded). Display
                # is bounded by the panel's proportional height; this controls
                # scroll-back depth. 1000 covers a multi-hour task with 40+
                # agent calls per phase without losing early history.
                yield RichLog(id="calls-log", markup=True, wrap=False, max_lines=1000)

    def on_mount(self) -> None:
        # Set up the subtasks DataTable columns once.
        st = self.query_one("#subtasks-table", DataTable)
        st.add_columns("Subtask", "State", "Retries", "Title")

    def render_snapshot(self, snap: DetailSnapshot) -> None:
        if snap.task_id != self._last_task_id:
            self._reset_fingerprints(snap.task_id)
        self._render_phase(snap)
        self._render_subtasks(snap)
        self._render_agent_calls(snap)

    def _reset_fingerprints(self, task_id: str) -> None:
        self._subtasks_fp = ()
        self._calls_fp = ()
        self._phase_fp = ()
        self._user_moved_subtask_cursor = False
        self._last_task_id = task_id

    def _render_phase(self, snap: DetailSnapshot) -> None:
        gauntlet_fp = (
            snap.pre_pr_audit_cycle,
            tuple(
                (s.get("name"), s.get("passed"), (s.get("summary") or "")[:80])
                for s in snap.pre_pr_audit_stages
            ),
        )
        phase_fp = (
            snap.task_state,
            snap.last_state_note,
            snap.in_state_for,
            snap.last_worktree_edit,
            gauntlet_fp,
        )
        if phase_fp != self._phase_fp:
            phase_text = _phase_line(snap)
            gauntlet_text = _gauntlet_block(snap)
            if gauntlet_text:
                phase_text = f"{phase_text}\n\n{gauntlet_text}"
            self.query_one("#detail-phase", Static).update(phase_text)
            self._phase_fp = phase_fp

    def _render_subtasks(self, snap: DetailSnapshot) -> None:
        sub_fp = tuple((s.subtask_id, s.state, s.retries) for s in snap.subtasks)
        if sub_fp == self._subtasks_fp:
            return
        st = self.query_one("#subtasks-table", DataTable)
        prev_cursor_row = st.cursor_coordinate.row
        st.clear()
        add_errors = self._populate_subtask_rows(st, snap)
        self._log_subtask_render(snap, st, add_errors)
        if snap.subtasks:
            target = self._compute_scroll_target(snap, prev_cursor_row)
            self.app.call_after_refresh(self._apply_subtask_scroll, target, len(snap.subtasks))
        self._subtasks_fp = sub_fp

    def _populate_subtask_rows(self, table: DataTable, snap: DetailSnapshot) -> list[str]:
        add_errors: list[str] = []
        for sub in snap.subtasks:
            try:
                table.add_row(
                    sub.subtask_id,
                    _subtask_state_cell(sub.state),
                    str(sub.retries),
                    sub.title[:60],
                    key=sub.subtask_id,
                )
            except Exception as e:
                add_errors.append(f"{sub.subtask_id}:{type(e).__name__}:{e}")
        return add_errors

    def _log_subtask_render(self, snap: DetailSnapshot, table: DataTable, add_errors: list[str]) -> None:
        if not _log.isEnabledFor(logging.DEBUG):
            return
        try:
            rendered = table.row_count
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

    def _render_agent_calls(self, snap: DetailSnapshot) -> None:
        calls_fp = tuple(snap.agent_calls)
        if calls_fp == self._calls_fp:
            return
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

    def _compute_scroll_target(self, snap: DetailSnapshot, prev_cursor_row: int) -> int:
        """Pick the row the subtasks-table viewport should anchor on after a
        re-render. User cursor wins; otherwise follow the active subtask;
        otherwise jump to the last row (newly-appended fixup slices)."""
        if self._user_moved_subtask_cursor:
            return max(0, min(prev_cursor_row, len(snap.subtasks) - 1))
        if snap.active_subtask_idx >= 0:
            return snap.active_subtask_idx
        return max(0, len(snap.subtasks) - 1)

    def _apply_subtask_scroll(self, target: int, total: int) -> None:
        """Move cursor + scroll to `target`, called via call_after_refresh
        so Textual's layout pass has finished and `virtual_size` reflects
        the current row count. `total` is captured at scheduling time so a
        rapid second update doesn't race with this one."""
        st = self.query_one("#subtasks-table", DataTable)
        if total == 0 or target >= total:
            return
        st.move_cursor(row=target, column=0, animate=False)
        if target >= total - 2:
            st.scroll_end(animate=False)
        else:
            st.scroll_to(y=target, animate=False)


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
_WHOLE_TASK_STATES = {
    "fixup_planning",
    "addressing_feedback",
    "rebasing_to_main",
}


def _phase_line(snap: DetailSnapshot) -> str:
    """One-line summary of what the agent is doing right now.

    Examples:
      R-0001 · doing_subtask · S-07-mcp-tools attempt 2 · in-state 47s · edit 12s ago
      R-0001 · doing · monolithic fixup attempt 1 · in-state 32m · edit 42s ago
      R-0001 · pending_ci · #57 green · in-state 14m
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
    # State display: short label with bracketed long description so the
    # operator never has to remember what the compact state label means.
    long_desc = _state_long_description(state)
    if long_desc:
        parts.append(f"[{color}]{state}[/] [dim]({long_desc})[/]")
    else:
        parts.append(f"[{color}]{state}[/]")
    # State-specific extras: review-loop context (round / threads) takes
    # priority over the generic state_log note when addressing_feedback,
    # since "round 3 · 2 threads" is more useful at-a-glance than a
    # truncated transition note.
    if state == "addressing_feedback":
        if snap.review_round is not None and snap.review_threads_count is not None:
            parts.append(f"round [b]{snap.review_round}[/] · [b]{snap.review_threads_count}[/] threads")
    elif state in {"local_ci_checking", "pre_pr_auditing", "fixup_planning"} and note:
        # Pipeline notes carry per-stage context ("rubric audit (codex)"); show.
        parts.append(f"[dim]{note[:80]}[/]")
    elif note:
        parts.append(f"[dim]{note[:80]}[/]")
    if in_state:
        parts.append(f"in-state [b]{in_state}[/]")
    if edit and state in _WHOLE_TASK_STATES | {"doing_subtask", "checking_subtask", "triaging_subtask"}:
        parts.append(f"edit [b]{edit}[/]")
    line = "  ·  ".join(parts)
    # Trailing notes — explain "why does the subtasks tab look frozen?" for
    # the operator. The post-PR states get their own copy: the worker has
    # fully released this task, the daemon is the one polling.
    if state in {"pending_ci", "awaiting_review", "merge_ready"}:
        line += "\n[dim italic](no action required — auto-polling for CI + reviews)[/]"
    elif state in _WHOLE_TASK_STATES:
        line += "\n[dim italic](task-level phase — subtask states below are frozen until this returns)[/]"
    return line


_STATE_LONG_DESCRIPTION = {
    "checking_subtask": "per-subtask checker",
    "triaging_subtask": "per-subtask triage",
    "local_ci_checking": "local CI gate (just ci)",
    "pre_pr_auditing": "pre-PR audit gauntlet",
    "fixup_planning": "planning fixup subtasks",
    "triaging_feedback": "Python triage of review threads",
    "addressing_feedback": "fixup planner + per-subtask doer",
    "conflict_resolving": "spawned conflict-resolver agent",
    "rebasing_to_main": "rebasing onto main (parent merged)",
    "pending_ci": "PR open · CI running",
    "awaiting_review": "CI green · awaiting review",
    "merge_ready": "ready to merge",
    "doing_subtask": "running per-subtask doer",
}


def _state_long_description(state: str) -> str | None:
    """Long-form description for an ambiguous state name. None when no
    description is needed (already self-explanatory: pending, merged, etc.)."""
    return _STATE_LONG_DESCRIPTION.get(state)


# Stage display order + human labels. The same four stages always render —
# ones that haven't run yet in the current cycle show as "queued" so the
# operator sees the full pipeline shape rather than just "what's done so far."
_GAUNTLET_STAGES = [
    ("local_ci", "local CI gate (just ci)"),
    ("rubric", "rubric audit (codex, 6 categories)"),
    ("standards", "standards audit (claude-opus + repo profile)"),
    ("behavior", "behavior audit (codex verifies expected_evidence)"),
]


# Plan 26: states where the persisted `pre_pr_audit_summary` represents
# either the in-flight cycle, the cycle just queued, or the post-pipeline
# disposition that the operator wants to see. In every other state the
# summary is stale (last completed cycle that's no longer the current
# concern) and rendering it alongside e.g. `doing_subtask Z-99-stabilize…`
# misleads the operator into thinking the audit is the active phase.
_GAUNTLET_RELEVANT_STATES = frozenset(
    {
        "pre_pr_auditing",
        "local_ci_checking",
        "fixup_planning",
        "committing",
        "pushing",
        "pending_ci",
        "awaiting_review",
        "merge_ready",
        "merged",
        "blocked",
        "failed",
    }
)


def _gauntlet_block(snap: DetailSnapshot) -> str | None:
    """Render the 4-stage pre-PR audit gauntlet as a pass/fail/queued block.

    Shape per stage:
      ✓ local_ci    — passed (CI green; rc=0)
      ✗ rubric      — security=5 < 7 (rationale: missing input validation)
      … standards   — running...
      · behavior    — queued

    Returns None when the task has never entered the pipeline (no summary
    on the row), OR when the task's current state is not pipeline-related
    (plan 26: avoids showing a stale prior-cycle summary while the task
    is back in spec/fixup subtask work)."""
    if not snap.pre_pr_audit_stages or snap.pre_pr_audit_cycle is None:
        return None
    if snap.task_state not in _GAUNTLET_RELEVANT_STATES:
        return None
    by_name = {s.get("name"): s for s in snap.pre_pr_audit_stages}
    lines = [f"[bold]Pre-PR audit gauntlet[/] — cycle {snap.pre_pr_audit_cycle}"]
    for name, label in _GAUNTLET_STAGES:
        s = by_name.get(name)
        if not s:
            # Stage not in the summary at all (older row layout) — skip rather
            # than render a misleading "queued."
            continue
        passed = s.get("passed")
        summary = (s.get("summary") or "")[:80]
        if passed is True:
            icon = "[green]✓[/]"
            status = "[green]passed[/]"
        elif passed is False:
            icon = "[red]✗[/]"
            status = "[red]failed[/]"
        elif summary == "queued":
            icon = "[dim]·[/]"
            status = "[dim]queued[/]"
        else:
            icon = "[yellow]…[/]"
            status = "[yellow]running[/]"
        lines.append(f"  {icon} [cyan]{label}[/] — {status} {f'[dim]({summary})[/]' if summary else ''}")
    return "\n".join(lines)


def _phase_color(state: str) -> str:
    color_sets = (
        ("green", {"merge_ready", "merged"}),
        ("blue", {"awaiting_review"}),
        ("red", {"blocked", "failed", "aborted"}),
        ("cyan", {"addressing_feedback"} | _WHOLE_TASK_STATES | {"doing_subtask", "checking_subtask"}),
        (
            "yellow",
            {"pending_ci", "rebasing_to_main", "triaging_subtask", "triaging_feedback", "conflict_resolving"},
        ),
    )
    return next((color for color, states in color_sets if state in states), "white")
