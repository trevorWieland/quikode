"""DAG screen smoke tests via Textual Pilot."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quikode.config import DEFAULT_CONFIG_TOML
from quikode.state import State, Store
from quikode.tui.app import QuikodeTUI
from quikode.tui.dag_view.screen import DAGScreen


def _bootstrap(tmp_path: Path) -> Path:
    qkdir = tmp_path / ".quikode"
    qkdir.mkdir()
    dag_path = tmp_path / "dag.json"
    nodes = []
    for nid, deps in [("A", []), ("B", ["A"]), ("C", ["B"]), ("D", ["B", "C"])]:
        nodes.append(
            {
                "id": nid,
                "kind": "behavior",
                "milestone": "M-1",
                "title": f"Node {nid}",
                "scope": "x",
                "depends_on": deps,
                "completes_behaviors": [],
                "supports_behaviors": [],
                "boundary_with_neighbors": "",
                "expected_evidence": [],
                "playbook": [],
                "rationale": "",
                "risks": [],
            }
        )
    dag_path.write_text(
        json.dumps(
            {
                "schema": "test",
                "milestones": [{"id": "M-1", "title": "x", "goal": "x", "status": "planned"}],
                "nodes": nodes,
            }
        )
    )
    (qkdir / "config.toml").write_text(
        DEFAULT_CONFIG_TOML.format(repo_path=str(tmp_path), dag_path=str(dag_path))
    )
    return tmp_path


@pytest.mark.asyncio
async def test_dag_screen_opens_and_renders(tmp_path):
    _bootstrap(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("A")
    store.transition("A", State.MERGED)

    app = QuikodeTUI(workspace=tmp_path, poll_interval_s=10.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Pressing 'g' opens the DAG screen.
        await pilot.press("g")
        await pilot.pause()
        # Active screen should now be DAGScreen.
        assert isinstance(app.screen, DAGScreen)


@pytest.mark.asyncio
async def test_critical_path_toggle(tmp_path):
    _bootstrap(tmp_path)
    Store(tmp_path / ".quikode" / "quikode.db")
    app = QuikodeTUI(workspace=tmp_path, poll_interval_s=10.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DAGScreen)
        before = screen._view.show_critical_path
        # Invoke the action directly — pilot key delivery races with the
        # CommandBar's Input grabbing focus and consuming chars in tests.
        # The keybinding is wired (priority=True) and lands when the
        # graph widget has focus interactively; this test asserts the
        # action toggles state.
        screen.action_toggle_critical_path()
        await pilot.pause()
        assert screen._view.show_critical_path is not before


@pytest.mark.asyncio
async def test_enter_pops_with_selected_anchor(tmp_path):
    _bootstrap(tmp_path)
    Store(tmp_path / ".quikode" / "quikode.db")
    app = QuikodeTUI(workspace=tmp_path, poll_interval_s=10.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DAGScreen)
        anchor = screen._view.anchor
        assert anchor is not None
        screen.action_select_anchor()
        await pilot.pause()
        # After dismiss the app's _selected_task_id should match the anchor.
        assert app._selected_task_id == anchor


@pytest.mark.asyncio
async def test_q_returns_without_setting_selection(tmp_path):
    _bootstrap(tmp_path)
    Store(tmp_path / ".quikode" / "quikode.db")
    app = QuikodeTUI(workspace=tmp_path, poll_interval_s=10.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        prior_selection = app._selected_task_id
        await pilot.press("g")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DAGScreen)
        screen.action_back()
        await pilot.pause()
        # selection unchanged (back dismisses with None)
        assert app._selected_task_id == prior_selection


@pytest.mark.asyncio
async def test_milestone_toggle_flag_flips(tmp_path):
    _bootstrap(tmp_path)
    Store(tmp_path / ".quikode" / "quikode.db")
    app = QuikodeTUI(workspace=tmp_path, poll_interval_s=10.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("g")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DAGScreen)
        before = screen._view.show_milestone_overlay
        screen.action_toggle_milestone_overlay()
        await pilot.pause()
        assert screen._view.show_milestone_overlay is not before
