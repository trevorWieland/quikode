"""`/set-model <phase> <cli>:<model>` writes [agents.<phase>] in TOML.

Also covers the other two TUI bugs that landed in the same patch:
- activity feed reverses order so newest is at the bottom (tail -f feel)
- input box CSS no longer collapses Input height (regression: bug #1)
"""

from __future__ import annotations

import tomllib
from importlib import resources
from pathlib import Path

import pytest

from quikode.config import DEFAULT_CONFIG_TOML, load_config
from quikode.state import Store
from quikode.tui.app import QuikodeTUI
from quikode.tui.controllers.command_dispatch import _set_agent_role_in_toml
from quikode.tui.widgets.activity_feed import ActivityEntry, ActivityFeed


def _bootstrap(tmp_path: Path) -> Path:
    qkdir = tmp_path / ".quikode"
    qkdir.mkdir()
    (qkdir / "config.toml").write_text(
        DEFAULT_CONFIG_TOML.format(repo_path=str(tmp_path), dag_path=str(tmp_path / "dag.json"))
    )
    return tmp_path


# ----- /set-model TOML writer -----


def test_set_model_replaces_existing_agent_section(tmp_path):
    p = _bootstrap(tmp_path) / ".quikode" / "config.toml"
    _set_agent_role_in_toml(p, "doer", "claude", "claude-opus-4-7")
    cfg = load_config(tmp_path)
    assert cfg.doer.cli.value == "claude"
    assert cfg.doer.model == "claude-opus-4-7"
    # Other agents preserved
    assert cfg.planner.cli.value == "claude"
    assert cfg.checker.cli.value == "codex"


def test_set_model_appends_section_when_absent(tmp_path):
    qkdir = tmp_path / ".quikode"
    qkdir.mkdir()
    p = qkdir / "config.toml"
    p.write_text(f'repo_path = "{tmp_path}"\ndag_path = "{tmp_path / "dag.json"}"\n')
    _set_agent_role_in_toml(p, "intent_reviewer", "claude", "claude-sonnet-4-6")
    parsed = tomllib.loads(p.read_text())
    assert parsed["agents"]["intent_reviewer"]["cli"] == "claude"
    assert parsed["agents"]["intent_reviewer"]["model"] == "claude-sonnet-4-6"


def test_set_model_idempotent(tmp_path):
    p = _bootstrap(tmp_path) / ".quikode" / "config.toml"
    _set_agent_role_in_toml(p, "triage", "codex", "gpt-5.3-codex")
    first = p.read_text()
    _set_agent_role_in_toml(p, "triage", "codex", "gpt-5.3-codex")
    assert p.read_text() == first


# ----- /set-model dispatcher behavior -----


@pytest.mark.asyncio
async def test_dispatch_set_model_happy_path(tmp_path):
    _bootstrap(tmp_path)
    Store(tmp_path / ".quikode" / "quikode.db")
    app = QuikodeTUI(workspace=tmp_path, poll_interval_s=0.05)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._dispatch_slash("/set-model planner claude:claude-sonnet-4-6")
        await pilot.pause()
        cfg = load_config(tmp_path)
        assert cfg.planner.model == "claude-sonnet-4-6"
        assert cfg.planner.cli.value == "claude"


@pytest.mark.asyncio
async def test_dispatch_set_model_rejects_unknown_phase(tmp_path):
    _bootstrap(tmp_path)
    Store(tmp_path / ".quikode" / "quikode.db")
    app = QuikodeTUI(workspace=tmp_path, poll_interval_s=0.05)
    async with app.run_test() as pilot:
        await pilot.pause()
        # No-op: should not crash, should not modify config
        app._dispatch_slash("/set-model bogus claude:foo")
        await pilot.pause()
        cfg = load_config(tmp_path)
        # planner stays at default
        assert cfg.planner.cli.value == "claude"


@pytest.mark.asyncio
async def test_dispatch_set_model_rejects_unknown_cli(tmp_path):
    _bootstrap(tmp_path)
    Store(tmp_path / ".quikode" / "quikode.db")
    app = QuikodeTUI(workspace=tmp_path, poll_interval_s=0.05)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._dispatch_slash("/set-model planner aider:bogus")
        await pilot.pause()
        cfg = load_config(tmp_path)
        assert cfg.planner.cli.value == "claude"  # unchanged


@pytest.mark.asyncio
async def test_dispatch_set_model_rejects_missing_args(tmp_path):
    _bootstrap(tmp_path)
    Store(tmp_path / ".quikode" / "quikode.db")
    app = QuikodeTUI(workspace=tmp_path, poll_interval_s=0.05)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._dispatch_slash("/set-model planner")  # missing model spec
        await pilot.pause()
        assert app.is_running


# ----- activity feed orientation (newest at bottom) -----


def test_render_entries_writes_oldest_first():
    """Bug fix: SQL returns newest-first (ORDER BY ts DESC). We reverse before
    writing so the bottom of the RichLog is the newest entry (tail -f feel).
    Auto-scroll then naturally lands on the newest line.

    Pure unit test — mock write() and assert order without spinning up the App."""
    feed = ActivityFeed.__new__(ActivityFeed)  # bypass Widget __init__
    written: list[str] = []
    feed.clear = written.clear  # type: ignore[method-assign]
    feed.write = written.append  # type: ignore[method-assign]
    entries = [
        ActivityEntry(timestamp="00:03:00", task_id="T-3", transition="C → D"),
        ActivityEntry(timestamp="00:02:00", task_id="T-2", transition="B → C"),
        ActivityEntry(timestamp="00:01:00", task_id="T-1", transition="A → B"),
    ]
    feed.render_entries(entries)
    # Caller passes newest-first; we write in reverse so:
    #   written[0] = oldest (top of log), written[-1] = newest (bottom of log).
    assert "T-1" in written[0]
    assert "T-3" in written[-1]


# ----- input height (regression for bug: typed text invisible) -----


def test_command_input_css_pins_height_to_three():
    """The CSS must keep `#command-input { height: 3 }` — Textual's Input has
    a 3-row default (tall border + 1-row content area). Inside a constrained
    Container the auto-layout can collapse it to 1 row, which hides the
    cursor and the typed text. Regression for the original visible-typing bug."""
    css_text = resources.files("quikode.tui").joinpath("styles/quikode.tcss").read_text()
    # Find the #command-input block and assert it sets height
    block_start = css_text.index("#command-input")
    block = css_text[block_start : block_start + 200]
    assert "height: 3" in block, f"#command-input missing height:3 — got:\n{block}"
