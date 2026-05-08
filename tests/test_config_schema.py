"""Pydantic schema for Config + types models.

These tests pin the contract that the TUI settings modal relies on:
- All numeric Config fields carry bounds (`ge`/`le`).
- All fields carry a description (so the modal can render labels).
- StrEnum-valued fields parse old plain-string values from older configs.

Plan 38 PR-B.7: the legacy `AgentRole` / `AgentCli` / `AgentResult` /
`IntentReviewOutcome` shapes were retired. Roles bind to MODELS via
`cfg.<role>_model`; the JsonAgent layer's `JsonAgentResult` carries
the per-call shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from quikode.config import Config, StackingStrategy


def _cfg(**kw: Any) -> Config:
    return Config(repo_path=Path("/tmp"), dag_path=Path("/tmp"), **kw)


# ----- bounds -----


def test_max_parallel_lower_bound():
    with pytest.raises(ValidationError):
        _cfg(max_parallel=0)


def test_max_parallel_upper_bound():
    with pytest.raises(ValidationError):
        _cfg(max_parallel=999)


def test_subtask_doer_timeout_must_be_at_least_60s():
    with pytest.raises(ValidationError):
        _cfg(subtask_doer_timeout_s=10)


def test_stall_warn_seconds_lower_bound():
    with pytest.raises(ValidationError):
        _cfg(stall_warn_seconds=10)


def test_stacking_max_depth_lower_bound():
    with pytest.raises(ValidationError):
        _cfg(stacking_max_depth=0)


# ----- enum coercion -----


def test_stacking_strategy_accepts_old_string():
    cfg = _cfg(stacking_strategy="within-milestone")
    assert cfg.stacking_strategy is StackingStrategy.WITHIN_MILESTONE
    # equality with bare string still works (StrEnum)
    assert cfg.stacking_strategy == "within-milestone"


def test_stacking_strategy_rejects_unknown():
    with pytest.raises(ValidationError):
        _cfg(stacking_strategy="bogus")


# ----- descriptions present (modal-renderable) -----


def test_all_config_fields_have_descriptions():
    """Every Config field needs a Field(description=...) so the settings modal
    has labels. Computed-default-only fields (auth dirs) are exempt."""
    schema = Config.model_json_schema()
    props = schema["properties"]
    # Auth-mount paths get default labels — modal will show field name. Document the exemption.
    exempt = {
        "claude_auth_dir",
        "claude_json_path",
        "codex_auth_dir",
        "opencode_auth_dir",
        "opencode_config_dir",
    }
    missing = [name for name, p in props.items() if name not in exempt and not p.get("description")]
    assert not missing, f"Config fields without description: {missing}"


def test_config_extra_fields_forbidden():
    """Catches typos in TOML — `max_paralel = 5` would silently no-op without this."""
    with pytest.raises(ValidationError):
        _cfg(unknown_field=1)


def test_config_validate_assignment_enforces_bounds():
    """Settings modal mutates config.max_parallel = N; we want pydantic to
    reject out-of-bounds at assignment, not just construction."""
    cfg = _cfg()
    with pytest.raises(ValidationError):
        cfg.max_parallel = 0
