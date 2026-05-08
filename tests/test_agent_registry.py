"""Plan 38 PR-A: agent registry tests — make_agent dispatches roles to wrappers."""

from __future__ import annotations

from pathlib import Path

import pytest

from quikode.agent_registry import ROLES, make_agent
from quikode.agent_schemas import (
    DoerEnvelope,
    PlannerOutput,
    ProgressVerdict,
    SubtaskCheckerOutput,
)
from quikode.agents.json_claude import ClaudeJsonAgent
from quikode.agents.json_codex_direct import CodexDirectJsonAgent
from quikode.agents.json_codex_litellm import CodexLitellmJsonAgent
from quikode.agents.json_protocol import JsonOutputAgent, WritesFilesAgent
from quikode.config import Config
from quikode.model_registry import MODELS


def _cfg(**overrides) -> Config:
    base: dict = {
        "repo_path": Path("/tmp/repo"),
        "dag_path": Path("/tmp/dag"),
    }
    base.update(overrides)
    return Config(**base)


def test_every_role_default_model_resolves() -> None:
    """Every role's default_model must be a real entry in MODELS."""
    for role_name, spec in ROLES.items():
        assert spec.default_model in MODELS, (
            f"role {role_name}: default_model {spec.default_model!r} not in MODELS"
        )


def test_every_role_timeout_field_exists_on_config() -> None:
    """Every role's `timeout_s_field` must resolve on a default Config."""
    cfg = _cfg()
    for role_name, spec in ROLES.items():
        assert hasattr(cfg, spec.timeout_s_field), (
            f"role {role_name}: cfg has no attribute {spec.timeout_s_field!r}"
        )


def test_make_agent_planner_default_returns_codex_direct_json_output() -> None:
    cfg = _cfg()
    agent = make_agent("planner", cfg)
    assert isinstance(agent, JsonOutputAgent)
    assert isinstance(agent.transport, CodexDirectJsonAgent)
    assert agent.output_schema is PlannerOutput
    assert agent.transport.profile == "gpt5"


def test_make_agent_planner_override_to_glm_zai() -> None:
    cfg = _cfg(planner_model="GLM-5.1-zai")
    agent = make_agent("planner", cfg)
    assert isinstance(agent, JsonOutputAgent)
    assert isinstance(agent.transport, CodexLitellmJsonAgent)
    assert agent.transport.profile == "glm-zai"
    assert agent.output_schema is PlannerOutput


def test_make_agent_subtask_doer_returns_writes_files_with_doer_envelope() -> None:
    cfg = _cfg()
    agent = make_agent("subtask_doer", cfg)
    assert isinstance(agent, WritesFilesAgent)
    assert agent.envelope_schema is DoerEnvelope
    assert isinstance(agent.transport, CodexLitellmJsonAgent)
    assert agent.transport.profile == "glm-zai"


def test_make_agent_subtask_doer_override_to_claude_opus() -> None:
    cfg = _cfg(subtask_doer_model="claude-opus-4-7")
    agent = make_agent("subtask_doer", cfg)
    assert isinstance(agent, WritesFilesAgent)
    assert isinstance(agent.transport, ClaudeJsonAgent)
    assert agent.transport.model_id == "claude-opus-4-7[1m]"
    assert agent.envelope_schema is DoerEnvelope


def test_make_agent_subtask_checker_returns_json_output() -> None:
    cfg = _cfg()
    agent = make_agent("subtask_checker", cfg)
    assert isinstance(agent, JsonOutputAgent)
    assert agent.output_schema is SubtaskCheckerOutput


def test_make_agent_progress_returns_json_output() -> None:
    cfg = _cfg()
    agent = make_agent("progress", cfg)
    assert isinstance(agent, JsonOutputAgent)
    assert agent.output_schema is ProgressVerdict


def test_make_agent_unknown_role_raises_keyerror() -> None:
    cfg = _cfg()
    with pytest.raises(KeyError, match="unknown role"):
        make_agent("not-a-role", cfg)


def test_make_agent_unknown_model_raises_keyerror() -> None:
    cfg = _cfg(planner_model="totally-bogus-model")
    with pytest.raises(KeyError, match="not in model registry"):
        make_agent("planner", cfg)


def test_make_agent_every_role_constructs_with_defaults() -> None:
    """Smoke: every role's default_model resolves into a working wrapper."""
    cfg = _cfg()
    for role_name in ROLES:
        agent = make_agent(role_name, cfg)
        assert isinstance(agent, (JsonOutputAgent, WritesFilesAgent))
