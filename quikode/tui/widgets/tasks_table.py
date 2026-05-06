"""Tasks panel — primary navigation surface."""

from __future__ import annotations

from dataclasses import dataclass

from textual.widgets import DataTable


@dataclass(frozen=True)
class TaskRowSnapshot:
    """One row's data, normalized for the table."""

    task_id: str
    title: str  # human title from the DAG (truncated for display)
    milestone: str  # milestone id like "M-0001"
    state: str
    in_state_for: str  # time since the last state transition
    runtime: str  # wall-clock since the most recent attempt began
    retries: str
    branch_or_pr: str


_STATE_STYLE = {
    "doing_subtask": "state-doing",
    "checking_subtask": "state-doing",
    "triaging_subtask": "state-rebasing",
    "rebasing_to_main": "state-rebasing",
    "conflict_resolving": "state-failed",
    "fixup_planning": "state-doing",
    "blocked": "state-blocked",
    "failed": "state-failed",
    "aborted": "state-failed",
    # v3.5 post-PR split: PENDING_CI shows as "running" (yellow), AWAITING_REVIEW
    # as "awaiting" (subtler), MERGE_READY as "ready" (green). Each one is a
    # distinct color so the operator can tell at a glance whether a PR is
    # waiting on CI, waiting on humans, or fully cleared to land.
    "pending_ci": "state-rebasing",
    "awaiting_review": "state-awaiting",
    "merge_ready": "state-merged",
    "merged": "state-merged",
    "pending": "state-pending",
    # v3.5 post-PR feedback split: TRIAGING_FEEDBACK is the in-process Python
    # triage step (fast); ADDRESSING_FEEDBACK is the worker-driven fixup
    # planner + per-subtask doer. Both color as "doing" so the operator
    # knows actual work is happening.
    "triaging_feedback": "state-responding",
    "addressing_feedback": "state-responding",
}


class TasksTable(DataTable):
    """DataTable populated from per-tick TaskRowSnapshot list.

    Selection is row-based; the App listens to RowHighlighted to update the
    detail panel.
    """

    DEFAULT_CSS = ""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._last_fp: tuple = ()

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_columns(
            "ID", "Milestone · Title", "State", "in-state", "Runtime", "Retries", "Branch / PR / Note"
        )

    def render_rows(self, rows: list[TaskRowSnapshot]) -> None:
        # Fingerprint the snapshot — only re-render when something actually
        # changed. Otherwise the 1s tick visually flashes the table.
        fp = tuple((r.task_id, r.state, r.in_state_for, r.runtime, r.retries, r.branch_or_pr) for r in rows)
        if fp == self._last_fp:
            return
        self._last_fp = fp
        # Snapshot diff would be smoother but full re-render is fine for v1.
        prev_cursor = self.cursor_coordinate
        self.clear()
        for r in rows:
            style = _STATE_STYLE.get(r.state, "")
            state_cell = f"[@click='show_state(\"{r.state}\")']{r.state}[/]" if style else r.state
            mtitle = f"{r.milestone} · {r.title[:50]}" if r.milestone else r.title[:60]
            self.add_row(
                r.task_id,
                mtitle,
                state_cell,
                r.in_state_for,
                r.runtime,
                r.retries,
                r.branch_or_pr,
                key=r.task_id,
            )
        # Try to restore cursor.
        if rows:
            try:
                self.move_cursor(row=min(prev_cursor.row, len(rows) - 1), column=0)
            except Exception:
                pass

    def selected_task_id(self) -> str | None:
        if self.row_count == 0:
            return None
        try:
            row_key = self.coordinate_to_cell_key(self.cursor_coordinate).row_key
            return str(row_key.value) if row_key.value is not None else None
        except Exception:
            return None
