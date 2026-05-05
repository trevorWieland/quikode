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
        # Reset fingerprints when the selected task changes — we want a fresh
        # render even if the new task happens to have the same row count.
        if snap.task_id != self._last_task_id:
            self._subtasks_fp = ()
            self._calls_fp = ()
            self._phase_fp = ()
            self._user_moved_subtask_cursor = False
            self._last_task_id = snap.task_id

        # ---- Phase status line + (when present) audit-gauntlet block ----
        # in_state_for re-stringifies every tick (incrementing seconds), so
        # the fingerprint includes it and the line refreshes once per second.
        # The cost is one Static.update() — way cheaper than the DataTable
        # rebuild that the fingerprints below skip.
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
            # Auto-follow the active subtask UNLESS the user has moved the
            # cursor manually. Cursor + scroll updates are deferred to the
            # next layout pass via `call_after_refresh` because Textual
            # computes `virtual_size` asynchronously after the message
            # handler returns — synchronous scroll attempts inside
            # `render_snapshot` see the pre-add_row size and anchor wrong.
            # That's the framework's API for "do this after layout
            # settles", not a hack we can purge.
            if snap.subtasks:
                target = self._compute_scroll_target(snap, prev_cursor_row)
                total = len(snap.subtasks)
                self.app.call_after_refresh(self._apply_subtask_scroll, target, total)
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
_WHOLE_SPEC_STATES = {
    "doing",
    "checking",
    "triaging",
    "final_checking",
    "replanning",
    # v3 states where the worker takes the wheel for the whole spec — the
    # subtasks tab is frozen until the task either commits + pushes or
    # transitions back to AWAITING_MERGE.
    "addressing_feedback",
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
    # State display: short label with bracketed long description so the
    # operator never has to remember what "checking" vs "checking_subtask"
    # vs "final_checking" vs "local_ci_checking" actually means.
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
    elif state in {"local_ci_checking", "pre_pr_auditing", "pre_pr_triaging"} and note:
        # Pipeline notes carry per-stage context ("rubric audit (codex)"); show.
        parts.append(f"[dim]{note[:80]}[/]")
    elif note:
        parts.append(f"[dim]{note[:80]}[/]")
    if in_state:
        parts.append(f"in-state [b]{in_state}[/]")
    if edit and state in _WHOLE_SPEC_STATES | {"doing_subtask", "checking_subtask", "triaging_subtask"}:
        parts.append(f"edit [b]{edit}[/]")
    line = "  ·  ".join(parts)
    # Trailing notes — explain "why does the subtasks tab look frozen?" for
    # the operator. The post-PR states get their own copy: the worker has
    # fully released this task, the daemon is the one polling.
    if state in {"pending_ci", "awaiting_review", "merge_ready"}:
        line += "\n[dim italic](no action required — auto-polling for CI + reviews)[/]"
    elif state in _WHOLE_SPEC_STATES:
        line += "\n[dim italic](whole-spec phase — subtask states below are frozen until this returns)[/]"
    return line


_STATE_LONG_DESCRIPTION = {
    "checking": "whole-spec checker (v0.1 legacy)",
    "checking_subtask": "per-subtask checker",
    "triaging_subtask": "per-subtask triage",
    "final_checking": "final whole-spec checker",
    "local_ci_checking": "local CI gate (just ci)",
    "pre_pr_auditing": "pre-PR audit gauntlet",
    "pre_pr_triaging": "merging audit findings → fixup planner",
    "triaging_feedback": "Python triage of review threads",
    "addressing_feedback": "fixup planner + per-subtask doer",
    "conflict_resolving": "spawned conflict-resolver agent",
    "intent_reviewing": "checking spec-compatibility after dep merge",
    "rebasing": "rebasing onto current main",
    "rebasing_to_main": "rebasing onto main (parent merged)",
    "fixup_planning": "planning fixup subtasks",
    "pending_ci": "PR open · CI running",
    "awaiting_review": "CI green · awaiting review",
    "merge_ready": "ready to merge",
    "doing_subtask": "running per-subtask doer",
    "doing": "running whole-spec doer (v0.1 legacy)",
    "triaging": "whole-spec triage (v0.1 legacy)",
    "replanning": "replanning after intent review",
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


def _gauntlet_block(snap: DetailSnapshot) -> str | None:
    """Render the 4-stage pre-PR audit gauntlet as a pass/fail/queued block.

    Shape per stage:
      ✓ local_ci    — passed (CI green; rc=0)
      ✗ rubric      — security=5 < 7 (rationale: missing input validation)
      … standards   — running...
      · behavior    — queued

    Returns None when the task has never entered the pipeline (no summary
    on the row). The cycle number is shown so multi-cycle runs are
    distinguishable: cycle 1 fails → fixup → cycle 2 passes."""
    if not snap.pre_pr_audit_stages or snap.pre_pr_audit_cycle is None:
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
    if state in {"merge_ready", "merged"}:
        return "green"
    if state == "awaiting_review":
        return "blue"
    if state == "pending_ci":
        return "yellow"
    if state in {"blocked", "failed", "aborted"}:
        return "red"
    if state in {"addressing_feedback", "triaging_feedback"}:
        # Cyan to match other "agent actively working" states. Distinct from
        # rebasing (yellow) so a glance differentiates "fixing feedback" from
        # "untangling git" at the phase-line level.
        return "cyan"
    if state == "rebasing_to_main":
        return "yellow"
    if state in _WHOLE_SPEC_STATES | {"doing_subtask", "checking_subtask"}:
        return "cyan"
    if state in {"triaging_subtask", "rebasing", "intent_reviewing", "conflict_resolving"}:
        return "yellow"
    return "white"
