"""TUI v1 step 7 — pydantic-driven settings modal."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from textual.widgets import Input, Static

from quikode.config import DEFAULT_CONFIG_TOML, Config, load_config
from quikode.tui.app import QuikodeTUI
from quikode.tui.widgets.settings_modal import (
    _MODAL_FIELDS,
    SettingsModal,
    _field_meta,
    _format_toml_value,
    _persist_overrides,
)


def _bootstrap_workspace(tmp_path: Path) -> Path:
    qkdir = tmp_path / ".quikode"
    qkdir.mkdir()
    (qkdir / "config.toml").write_text(
        DEFAULT_CONFIG_TOML.format(repo_path=str(tmp_path), dag_path=str(tmp_path / "dag.json"))
    )
    return tmp_path


# ----- TOML round-trip helpers -----


def test_format_toml_bool():
    assert _format_toml_value(True) == "true"
    assert _format_toml_value(False) == "false"


def test_format_toml_int():
    assert _format_toml_value(7) == "7"


def test_format_toml_string_quotes():
    assert _format_toml_value("within-milestone") == '"within-milestone"'


def test_persist_overrides_replaces_existing_key(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('# comment\nrepo_path = "/x"\nmax_parallel = 3\n\n[agents.planner]\ncli = "claude"\n')
    _persist_overrides(p, {"max_parallel": 7})
    parsed = tomllib.loads(p.read_text())
    assert parsed["max_parallel"] == 7
    # Section preserved
    assert parsed["agents"]["planner"]["cli"] == "claude"


def test_persist_overrides_appends_missing_key(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('# header\nrepo_path = "/x"\n\n[agents.planner]\ncli = "claude"\n')
    _persist_overrides(p, {"max_parallel": 5})
    parsed = tomllib.loads(p.read_text())
    assert parsed["max_parallel"] == 5


def test_persist_overrides_routes_stacking_to_section(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("# header\n")
    _persist_overrides(p, {"stacking_strategy": "within-milestone"})
    parsed = tomllib.loads(p.read_text())
    # stacking_strategy is mapped to [stacking].strategy per _TOML_SCHEMA
    assert parsed["stacking"]["strategy"] == "within-milestone"


def test_persist_overrides_replaces_existing_sectioned_key(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('# header\nrepo_path = "/x"\n\n[stacking]\nstrategy = "off"\nmax_depth = 4\n')
    _persist_overrides(p, {"stacking_strategy": "aggressive"})
    parsed = tomllib.loads(p.read_text())
    assert parsed["stacking"]["strategy"] == "aggressive"
    assert parsed["stacking"]["max_depth"] == 4  # other keys preserved


# ----- modal behavior with Pilot -----


@pytest.mark.asyncio
async def test_settings_modal_renders_current_config(tmp_path):
    _bootstrap_workspace(tmp_path)
    cfg = load_config(tmp_path)
    app = QuikodeTUI(workspace=tmp_path, poll_interval_s=0.05)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = SettingsModal(cfg, tmp_path / ".quikode" / "config.toml")
        app.push_screen(modal)
        for _ in range(5):
            await pilot.pause()
            if modal.is_running:
                break
        # The max_parallel input should reflect cfg.max_parallel
        max_par_input = modal.query_one("#field-max_parallel", Input)
        assert max_par_input.value == str(cfg.max_parallel)


@pytest.mark.asyncio
async def test_settings_apply_persists_to_toml(tmp_path):
    _bootstrap_workspace(tmp_path)
    cfg = load_config(tmp_path)
    toml_path = tmp_path / ".quikode" / "config.toml"
    app = QuikodeTUI(workspace=tmp_path, poll_interval_s=0.05)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = SettingsModal(cfg, toml_path)
        app.push_screen(modal)
        for _ in range(5):
            await pilot.pause()
            if modal.is_running:
                break
        # Change max_parallel
        max_par_input = modal.query_one("#field-max_parallel", Input)
        max_par_input.value = "7"
        # Click Apply
        modal._apply(restart=False)
        await pilot.pause()
    # After dismissal, the file should reflect the change
    parsed = tomllib.loads(toml_path.read_text())
    assert parsed["max_parallel"] == 7


@pytest.mark.asyncio
async def test_settings_apply_invalid_value_shows_error(tmp_path):
    _bootstrap_workspace(tmp_path)
    cfg = load_config(tmp_path)
    toml_path = tmp_path / ".quikode" / "config.toml"
    app = QuikodeTUI(workspace=tmp_path, poll_interval_s=0.05)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = SettingsModal(cfg, toml_path)
        app.push_screen(modal)
        for _ in range(5):
            await pilot.pause()
            if modal.is_running:
                break
        # max_parallel has le=32; set to a way-out-of-bounds value
        modal.query_one("#field-max_parallel", Input).value = "999999"
        modal._apply(restart=False)
        await pilot.pause()
        # Error widget should show something
        err = modal.query_one("#settings-error", Static)
        # Pydantic error message contains "max_parallel" or "less than or equal"
        assert "max_parallel" in str(err.render()) or "less than or equal" in str(err.render())


def test_modal_field_meta_is_populated_for_all_modal_fields():
    """Every field exposed in the modal must carry a description (so the
    label shows something meaningful)."""
    for name, _kind in _MODAL_FIELDS:
        meta = _field_meta(name)
        assert meta.get("description"), f"{name} has no description in Config schema"


def test_load_config_after_settings_modal_persist(tmp_path):
    """Round-trip: persist override → load_config → see new value."""
    _bootstrap_workspace(tmp_path)
    toml_path = tmp_path / ".quikode" / "config.toml"
    _persist_overrides(toml_path, {"max_parallel": 11, "stacking_strategy": "within-milestone"})
    cfg = load_config(tmp_path)
    assert cfg.max_parallel == 11
    assert cfg.stacking_strategy == "within-milestone"


def test_modal_rejects_extra_fields_via_pydantic(tmp_path):
    """Settings modal applies via cfg.model_copy(update=...). Any extras
    (typos, removed fields) are caught by pydantic's extra='forbid'."""
    _bootstrap_workspace(tmp_path)
    cfg = load_config(tmp_path)
    with pytest.raises(Exception):  # noqa: B017
        Config.model_validate({**cfg.model_dump(), "totally_made_up_field": 1})
