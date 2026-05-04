"""Regression: subtasks-table cursor moves must not pollute _selected_task_id.

Bug discovered during R-0001's run: the detail panel's `subtasks-table`
DataTable is inside the same App as the main `tasks-panel` DataTable, and
both emit `DataTable.RowHighlighted` events when their cursor moves. The
default `on_data_table_row_highlighted` handler took every event regardless
of source widget id, so when the subtasks table's cursor advanced (during a
re-render's `move_cursor` call), `_selected_task_id` was overwritten with a
subtask id like "S-01-domain-types". The next 1s poll tick found no task
row matching that id → empty DetailSnapshot → phase line flickered to
"S-01-domain-types · ?".

Fix lives in `quikode/tui/app.py`: filter both row-highlighted and
row-selected handlers by `event.control.id != "tasks-panel"` so subtasks
table events are ignored.
"""

from __future__ import annotations

import pytest
from textual.widgets import DataTable

from quikode.tui.app import QuikodeTUI
from quikode.tui.widgets.detail_panel import DetailPanel
from quikode.tui.widgets.tasks_table import TasksTable


@pytest.mark.asyncio
async def test_subtasks_table_highlight_does_not_change_selected_task(tmp_path):
    """A RowHighlighted from the subtasks-table DataTable must NOT update
    the App's _selected_task_id (the previous tasks-panel selection wins)."""
    app = QuikodeTUI(workspace=tmp_path)
    async with app.run_test() as pilot:
        # Pre-seed the App with a "selected" task id (as if the user had
        # already arrowed onto a row in the tasks panel).
        app._selected_task_id = "R-001"

        # The subtasks DataTable lives inside the detail panel and has
        # id="subtasks-table". Add a row so we can move the cursor.
        detail = app.query_one("#detail-panel", DetailPanel)
        st = detail.query_one("#subtasks-table", DataTable)
        st.add_row("S-99-fake", "pending", "0", "fake subtask", key="S-99-fake")
        await pilot.pause()

        # Move the cursor onto the row. This emits RowHighlighted with
        # event.control.id == "subtasks-table"; with the filter in place,
        # the handler is a no-op.
        st.move_cursor(row=0, column=0)
        await pilot.pause()

        assert app._selected_task_id == "R-001"


@pytest.mark.asyncio
async def test_tasks_panel_highlight_still_updates_selected(tmp_path):
    """The filter must NOT block legitimate tasks-panel events — a
    RowHighlighted from id='tasks-panel' should still update selection.

    Regression guard for over-eager filtering."""
    app = QuikodeTUI(workspace=tmp_path)
    async with app.run_test() as pilot:
        app._selected_task_id = None
        tasks = app.query_one("#tasks-panel", TasksTable)
        # Seed a row with a known key.
        tasks.add_row("R-042", "M-1 · t", "pending", "—", "—", "0/0/0", "—", key="R-042")
        await pilot.pause()
        tasks.move_cursor(row=0, column=0)
        await pilot.pause()
        assert app._selected_task_id == "R-042"
