"""TUI v1 step 2-4 — live poller against a real SQLite store.

Builds a workspace with `.quikode/config.toml` + `.quikode/quikode.db`,
populates a few tasks, and asserts the poller derives correct snapshots.
Avoids needing the orchestrator to be running.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quikode.config import DEFAULT_CONFIG_TOML
from quikode.state import State, Store
from quikode.tui.app import QuikodeTUI
from quikode.tui.controllers.store_polls import StorePoller, _humanize_seconds
from quikode.tui.widgets.tasks_table import TasksTable


def _bootstrap_workspace(tmp_path: Path) -> Path:
    qkdir = tmp_path / ".quikode"
    qkdir.mkdir()
    (qkdir / "config.toml").write_text(
        DEFAULT_CONFIG_TOML.format(repo_path=str(tmp_path), dag_path=str(tmp_path / "dag.json"))
    )
    return tmp_path


def _make_store(tmp_path: Path) -> Store:
    return Store(tmp_path / ".quikode" / "quikode.db")


def test_poller_unconfigured_workspace_returns_error_snapshot(tmp_path):
    """No .quikode/config.toml present → poller returns error snapshot, doesn't crash."""
    p = StorePoller(workspace=tmp_path)
    snap = p.poll()
    assert snap.error is not None
    assert "config" in snap.error.lower() or "no quikode" in snap.error.lower()
    assert snap.tasks == []


def test_poller_configured_no_db_returns_error(tmp_path):
    _bootstrap_workspace(tmp_path)
    p = StorePoller(workspace=tmp_path)
    snap = p.poll()
    assert snap.error is not None
    assert "sqlite" in snap.error.lower() or "no SQLite" in snap.error


def test_poller_with_tasks_renders_counts_and_rows(tmp_path):
    _bootstrap_workspace(tmp_path)
    store = _make_store(tmp_path)
    # T-001 in flight (doing_subtask), T-002 awaiting merge, T-003 blocked, T-004 merged
    store.upsert_pending("T-001")
    store.upsert_pending("T-002")
    store.upsert_pending("T-003")
    store.upsert_pending("T-004")
    store.transition("T-001", State.DOING_SUBTASK)
    store.transition("T-002", State.PENDING_CI, pr_number=42)
    store.transition("T-003", State.BLOCKED, last_error="exhausted retry budget on S-04")
    store.transition("T-004", State.MERGED)
    store.close() if hasattr(store, "close") else None

    p = StorePoller(workspace=tmp_path)
    snap = p.poll()
    assert snap.error is None

    # Header counts (all tasks, including merged)
    assert snap.header.in_flight == 1
    assert snap.header.awaiting == 1
    assert snap.header.blocked == 1
    assert snap.header.merged == 1

    # Tasks panel hides MERGED + ABORTED + PENDING — they're static / bulk,
    # the header counts are the live view. Only the 3 non-terminal active
    # tasks should appear.
    panel_ids = [r.task_id for r in snap.tasks]
    assert "T-004" not in panel_ids  # merged → filtered out
    assert set(panel_ids) == {"T-001", "T-002", "T-003"}
    # Order: blocked first, then awaiting, then in-flight
    assert panel_ids.index("T-003") < panel_ids.index("T-002") < panel_ids.index("T-001")

    # PR number formatting
    awaiting_row = next(r for r in snap.tasks if r.task_id == "T-002")
    assert awaiting_row.branch_or_pr == "#42"

    # Activity feed has at least one entry per transition we made
    assert len(snap.activity) >= 4


def test_poller_hides_pending_tasks_from_panel(tmp_path):
    """A fresh tanren workspace seeds 230+ pending rows; without filtering
    they crowd the live work out of the primary table. PENDING is included
    in `_PANEL_HIDDEN` alongside MERGED/ABORTED — the header still surfaces
    pending count via `total_in_scope - active - merged - awaiting - blocked`."""
    _bootstrap_workspace(tmp_path)
    store = _make_store(tmp_path)
    # Seed 5 pending + 1 in-flight. Only the in-flight should appear.
    for tid in ("T-001", "T-002", "T-003", "T-004", "T-005", "T-LIVE"):
        store.upsert_pending(tid)
    store.transition("T-LIVE", State.DOING_SUBTASK)

    snap = StorePoller(workspace=tmp_path).poll()
    assert snap.error is None
    panel_ids = [r.task_id for r in snap.tasks]
    assert panel_ids == ["T-LIVE"]  # all PENDING filtered out
    # Pending still counts toward total_in_scope so the header math works.
    assert snap.header.total_in_scope >= 6


def test_poller_resources_filters_to_in_flight(tmp_path):
    _bootstrap_workspace(tmp_path)
    store = _make_store(tmp_path)
    store.upsert_pending("T-001")
    store.upsert_pending("T-002")
    store.transition("T-001", State.DOING_SUBTASK)
    store.transition("T-002", State.MERGED)
    # Stats for both tasks
    store.record_container_stats("T-001", "qk-1", cpu_pct=42.0, mem_bytes=2 * 1024**3, mem_pct=10.0)
    store.record_container_stats("T-002", "qk-2", cpu_pct=10.0, mem_bytes=1 * 1024**3, mem_pct=5.0)

    p = StorePoller(workspace=tmp_path)
    snap = p.poll()
    # Only the in-flight task should appear in resources.containers
    assert [c.task_id for c in snap.resources.containers] == ["T-001"]
    assert snap.resources.containers[0].cpu_pct == pytest.approx(42.0)
    assert snap.resources.containers[0].rss_gb == pytest.approx(2.0, abs=0.01)


def test_poller_detail_for_selected_task(tmp_path):
    _bootstrap_workspace(tmp_path)
    store = _make_store(tmp_path)
    store.upsert_pending("T-001")
    store.transition("T-001", State.DOING_SUBTASK)
    store.upsert_subtasks(
        "T-001",
        [
            {"subtask_id": "S-01", "title": "domain", "acceptance": ["a"]},
            {"subtask_id": "S-02", "title": "events", "acceptance": ["b"]},
        ],
    )
    store.update_subtask("T-001", "S-01", state="done")
    store.update_subtask("T-001", "S-02", state="doing")
    store.record_agent_call(
        "T-001",
        phase="planner",
        cli="claude",
        model="claude-opus-4-7",
        rc=0,
        duration_s=12.5,
        tokens_used=4321,
    )
    store.add_artifact("T-001", "planner_output", "Add accounts across 5 interfaces.")

    p = StorePoller(workspace=tmp_path)
    snap = p.poll(selected_task_id="T-001")
    assert snap.detail.task_id == "T-001"
    # Subtasks are now structured rows, not formatted strings
    assert any(s.subtask_id == "S-01" and s.state == "done" for s in snap.detail.subtasks)
    assert any(s.subtask_id == "S-02" and s.state == "doing" for s in snap.detail.subtasks)
    assert any("planner" in c and "claude" in c for c in snap.detail.agent_calls)


def test_poller_detail_no_selection(tmp_path):
    _bootstrap_workspace(tmp_path)
    _make_store(tmp_path)
    p = StorePoller(workspace=tmp_path)
    snap = p.poll(selected_task_id=None)
    assert snap.detail.task_id == "(none)"


def test_poller_invalid_selection_handled(tmp_path):
    _bootstrap_workspace(tmp_path)
    _make_store(tmp_path)
    p = StorePoller(workspace=tmp_path)
    snap = p.poll(selected_task_id="DOES-NOT-EXIST")
    # Unknown task id → empty detail snapshot (no crash, no plan/log content).
    assert snap.detail.task_id == "DOES-NOT-EXIST"
    assert snap.detail.subtasks == []
    assert snap.detail.agent_calls == []


def test_poller_subtasks_active_idx(tmp_path):
    """The active subtask (in doing/checking/triaging state) is surfaced via
    DetailSnapshot.active_subtask_idx so the detail panel can highlight it."""
    _bootstrap_workspace(tmp_path)
    store = _make_store(tmp_path)
    store.upsert_pending("T-001")
    store.transition("T-001", State.DOING_SUBTASK)
    store.upsert_subtasks(
        "T-001",
        [
            {"subtask_id": "S-01", "title": "domain", "acceptance": ["a"]},
            {"subtask_id": "S-02", "title": "events", "acceptance": ["b"]},
            {"subtask_id": "S-03", "title": "store", "acceptance": ["c"]},
        ],
    )
    store.update_subtask("T-001", "S-01", state="done")
    store.update_subtask("T-001", "S-02", state="done")
    store.update_subtask("T-001", "S-03", state="doing")
    p = StorePoller(workspace=tmp_path)
    snap = p.poll(selected_task_id="T-001")
    assert snap.detail.active_subtask_idx == 2  # S-03 is at index 2
    assert len(snap.detail.subtasks) == 3
    assert snap.detail.subtasks[2].subtask_id == "S-03"
    assert snap.detail.subtasks[2].state == "doing"


@pytest.mark.asyncio
async def test_app_polls_and_renders_real_data(tmp_path):
    _bootstrap_workspace(tmp_path)
    store = _make_store(tmp_path)
    store.upsert_pending("T-LIVE")
    store.transition("T-LIVE", State.DOING_SUBTASK)
    # Use a tight poll interval for the test so we don't have to wait 1s.
    app = QuikodeTUI(workspace=tmp_path, poll_interval_s=0.05)
    async with app.run_test() as pilot:
        # Wait one tick so the timer fires. on_mount calls refresh_now() first
        # so even without waiting we should already see the task.
        await pilot.pause()
        table = app.query_one("#tasks-panel", TasksTable)
        assert table.row_count >= 1
        # Row keys carry the task id we set
        keys = [str(k.value) for k in table.rows]
        assert "T-LIVE" in keys


def test_humanize_seconds():
    assert _humanize_seconds(5) == "5s"
    assert _humanize_seconds(75).startswith("1m")
    assert _humanize_seconds(3700).startswith("1h")
    assert _humanize_seconds(-1) == "—"


def test_branch_or_pr_priority(tmp_path):
    _bootstrap_workspace(tmp_path)
    store = _make_store(tmp_path)
    store.upsert_pending("T-001")
    store.transition("T-001", State.PENDING_CI, branch="quikode/T-001-abc", pr_number=99)
    p = StorePoller(workspace=tmp_path)
    snap = p.poll()
    row = next(r for r in snap.tasks if r.task_id == "T-001")
    assert row.branch_or_pr == "#99"  # PR number wins over branch
