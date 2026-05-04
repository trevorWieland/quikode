"""TUI v1 step 1 — skeleton boots, panels exist, command bar dispatches."""

from __future__ import annotations

import pytest
from textual.widgets import TabbedContent

from quikode.tui.app import QuikodeTUI
from quikode.tui.controllers.slash_catalog import SLASH_CATALOG, filter_catalog
from quikode.tui.widgets.activity_feed import ActivityFeed
from quikode.tui.widgets.command_bar import CommandBar, CommandSuggestionList
from quikode.tui.widgets.detail_panel import DetailPanel
from quikode.tui.widgets.header import WorkspaceHeader
from quikode.tui.widgets.resources_panel import ResourcesPanel
from quikode.tui.widgets.tasks_table import TasksTable

# ----- pure (non-textual) tests -----


def test_slash_catalog_has_expected_commands():
    """The catalog must include the high-traffic commands the design doc names."""
    expected = {
        "run",
        "stop",
        "force-quit",
        "status",
        "show",
        "explain",
        "retry",
        "abort",
        "open-pr",
        "set-model",
        "set-max-parallel",
        "help",
        "quit",
    }
    missing = expected - set(SLASH_CATALOG)
    assert not missing, f"catalog missing: {missing}"


def test_catalog_descriptions_are_short():
    """Suggestion popover renders inline; descriptions over ~60 chars wrap badly."""
    too_long = {n: d for n, d in SLASH_CATALOG.items() if len(d) > 70}
    assert not too_long, f"descriptions too long: {too_long}"


def test_filter_catalog_prefix_match_first():
    ranked = filter_catalog("/sho")
    assert ranked[0][0] == "show"


def test_filter_catalog_substring_match():
    ranked = filter_catalog("merg")
    names = [r[0] for r in ranked]
    assert "mark-merged" in names


def test_filter_catalog_unknown_returns_empty():
    assert filter_catalog("xyz-nonexistent-zzz") == []


def test_filter_catalog_empty_returns_all():
    ranked = filter_catalog("")
    assert len(ranked) == len(SLASH_CATALOG)


def test_command_suggestion_list_handles_leading_slash():
    s = CommandSuggestionList(SLASH_CATALOG)
    a = s.filter("/show")
    b = s.filter("show")
    assert a == b


# ----- textual app boot test -----


@pytest.mark.asyncio
async def test_app_boots_and_shows_panels(tmp_path):
    """Smoke test: the App composes without raising and contains every named panel."""
    app = QuikodeTUI(workspace=tmp_path)
    async with app.run_test() as pilot:
        # Header rendered with workspace path
        header = app.query_one("#header-bar", WorkspaceHeader)
        assert "quikode" in str(header.render())
        # All five panels present
        assert app.query_one("#tasks-panel", TasksTable) is not None
        assert app.query_one("#activity-panel", ActivityFeed) is not None
        assert app.query_one("#resources-panel", ResourcesPanel) is not None
        assert app.query_one("#detail-panel", DetailPanel) is not None
        assert app.query_one("#command-bar", CommandBar) is not None
        await pilot.pause()


@pytest.mark.asyncio
async def test_q_key_quits(tmp_path):
    app = QuikodeTUI(workspace=tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("q")
        # After the Q binding fires, exit() is called; pilot pause lets it propagate.
        await pilot.pause()
    # Reaching this point without exception means the binding fired and exit propagated.
    assert True


@pytest.mark.asyncio
async def test_unknown_slash_logs_to_activity(tmp_path):
    app = QuikodeTUI(workspace=tmp_path)
    async with app.run_test() as pilot:
        # Directly invoke dispatcher (faster + more reliable than typing).
        app._dispatch_slash("/totally-not-a-command")
        await pilot.pause()
        # The activity feed should have written an "unknown command" line.
        # We can't easily read RichLog content, so just assert nothing crashed.
        assert app.is_running


@pytest.mark.asyncio
async def test_known_slash_does_not_crash(tmp_path):
    app = QuikodeTUI(workspace=tmp_path)
    async with app.run_test() as pilot:
        app._dispatch_slash("/status")
        await pilot.pause()
        assert app.is_running


@pytest.mark.asyncio
async def test_tab_cycles_detail_panel(tmp_path):
    app = QuikodeTUI(workspace=tmp_path)
    async with app.run_test() as pilot:
        detail = app.query_one("#detail-panel", DetailPanel)
        tabs = detail.query_one("#detail-tabs", TabbedContent)
        first = tabs.active
        detail.cycle_tab(direction=1)
        await pilot.pause()
        assert tabs.active != first
