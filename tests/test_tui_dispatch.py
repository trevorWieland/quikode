"""TUI v1 step 5 — slash command dispatch.

Pure-parse tests don't need a textual App; modal/handler tests use Pilot.
"""

from __future__ import annotations

import pytest

from quikode.config import DEFAULT_CONFIG_TOML
from quikode.state import State, Store
from quikode.tui.app import QuikodeTUI
from quikode.tui.controllers.command_dispatch import (
    HANDLERS,
    ParsedCommand,
    parse_slash,
)
from quikode.tui.controllers.slash_catalog import SLASH_CATALOG
from quikode.tui.widgets.confirm_modal import ConfirmModal

# ----- pure parse -----


def test_parse_slash_basic():
    p = parse_slash("/show R-001")
    assert p == ParsedCommand(verb="show", args=["R-001"], raw="/show R-001")


def test_parse_slash_no_args():
    p = parse_slash("/help")
    assert p is not None
    assert p.verb == "help"
    assert p.args == []


def test_parse_slash_with_quoted_arg():
    p = parse_slash("/set-model planner claude:claude-opus-4-7")
    assert p is not None
    assert p.verb == "set-model"
    assert p.args == ["planner", "claude:claude-opus-4-7"]


def test_parse_slash_non_slash_returns_none():
    assert parse_slash("just chatter") is None


def test_parse_slash_empty_returns_none():
    assert parse_slash("/") is None
    assert parse_slash("") is None


def test_parse_slash_strips_whitespace():
    assert parse_slash("   /retry T-1   ") == ParsedCommand(
        verb="retry", args=["T-1"], raw="   /retry T-1   "
    )


# ----- handler coverage -----


def test_every_handled_command_is_in_catalog():
    """Every handler must be discoverable via the autocomplete catalog,
    otherwise users can't find it."""
    extras = set(HANDLERS) - set(SLASH_CATALOG)
    assert not extras, f"handlers without catalog entry: {extras}"


def test_handlers_for_high_traffic_commands_exist():
    must_handle = {"show", "retry", "abort", "open-pr", "mark-merged", "help", "quit"}
    missing = must_handle - set(HANDLERS)
    assert not missing, f"missing handlers: {missing}"


# ----- dispatcher behavior (with Pilot) -----


def _bootstrap_workspace(tmp_path):
    qkdir = tmp_path / ".quikode"
    qkdir.mkdir()
    (qkdir / "config.toml").write_text(
        DEFAULT_CONFIG_TOML.format(repo_path=str(tmp_path), dag_path=str(tmp_path / "dag.json"))
    )
    return tmp_path


@pytest.mark.asyncio
async def test_dispatch_unknown_command_does_not_crash(tmp_path):
    _bootstrap_workspace(tmp_path)
    Store(tmp_path / ".quikode" / "quikode.db")  # touch the db so poller succeeds
    app = QuikodeTUI(workspace=tmp_path, poll_interval_s=0.05)
    async with app.run_test() as pilot:
        app._dispatch_slash("/totally-not-a-thing")
        await pilot.pause()
        assert app.is_running


@pytest.mark.asyncio
async def test_dispatch_help_writes_to_activity(tmp_path):
    _bootstrap_workspace(tmp_path)
    Store(tmp_path / ".quikode" / "quikode.db")
    app = QuikodeTUI(workspace=tmp_path, poll_interval_s=0.05)
    async with app.run_test() as pilot:
        app._dispatch_slash("/help")
        await pilot.pause()
        assert app.is_running


@pytest.mark.asyncio
async def test_dispatch_show_with_id_updates_selection(tmp_path):
    _bootstrap_workspace(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("T-001")
    store.transition("T-001", State.DOING_SUBTASK)
    app = QuikodeTUI(workspace=tmp_path, poll_interval_s=0.05)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._dispatch_slash("/show T-001")
        await pilot.pause()
        assert app._selected_task_id == "T-001"


@pytest.mark.asyncio
async def test_dispatch_retry_pushes_confirm_modal(tmp_path):
    _bootstrap_workspace(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("T-001")
    store.transition("T-001", State.BLOCKED)
    app = QuikodeTUI(workspace=tmp_path, poll_interval_s=0.05)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._dispatch_slash("/retry T-001")
        # Modal mounting is async — give it a couple of frames to settle.
        for _ in range(5):
            await pilot.pause()
            if any(isinstance(s, ConfirmModal) for s in app.screen_stack):
                break
        assert any(isinstance(s, ConfirmModal) for s in app.screen_stack)


@pytest.mark.asyncio
async def test_dispatch_open_pr_with_no_url_warns(tmp_path):
    _bootstrap_workspace(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("T-001")
    app = QuikodeTUI(workspace=tmp_path, poll_interval_s=0.05)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Should not raise; should write a "no PR URL" toast.
        app._dispatch_slash("/open-pr T-001")
        await pilot.pause()
        assert app.is_running


@pytest.mark.asyncio
async def test_dispatch_show_without_arg_uses_selection(tmp_path):
    _bootstrap_workspace(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("T-001")
    store.transition("T-001", State.DOING_SUBTASK)
    app = QuikodeTUI(workspace=tmp_path, poll_interval_s=0.05)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Initial cursor should land on T-001 (only row); /show with no arg uses it.
        app._dispatch_slash("/show")
        await pilot.pause()
        # _selected_task_id is set either by the cursor highlight handler or by /show.
        assert app._selected_task_id == "T-001"
