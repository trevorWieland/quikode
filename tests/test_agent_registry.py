"""Plan 38 PR-A: agent registry tests — make_agent dispatches roles to wrappers."""

from __future__ import annotations

from pathlib import Path

import pytest

from quikode.agent_registry import ROLES, make_agent
from quikode.agent_schemas import (
    PlannerOutput,
    ProgressVerdict,
    SubtaskCheckerOutput,
)
from quikode.agents.json_claude import ClaudeJsonAgent
from quikode.agents.json_codex_direct import CodexDirectJsonAgent
from quikode.agents.json_codex_litellm import CodexLitellmJsonAgent
from quikode.agents.json_fallback import QuotaFallbackJsonAgent
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
    assert isinstance(agent.transport, QuotaFallbackJsonAgent)
    assert isinstance(agent.transport.primary, CodexLitellmJsonAgent)
    assert agent.transport.primary.profile == "glm-zai"
    # Plan 59 fix A + E': no `quota_max_total_wait_s` attribute on
    # transports anymore — quota fast-fails inside `_run_with_retry`
    # and the worker layer handles the cross-attempt cadence.
    assert not hasattr(agent.transport.primary, "quota_max_total_wait_s")
    assert len(agent.transport.fallbacks) == 3
    assert isinstance(agent.transport.fallbacks[0], CodexLitellmJsonAgent)
    assert agent.transport.fallbacks[0].profile == "glm-wafer"
    assert not hasattr(agent.transport.fallbacks[0], "quota_max_total_wait_s")
    assert isinstance(agent.transport.fallbacks[1], ClaudeJsonAgent)
    assert agent.transport.fallbacks[1].model_id == "claude-sonnet-4-6"
    assert isinstance(agent.transport.fallbacks[2], CodexDirectJsonAgent)
    assert agent.transport.fallbacks[2].profile == "codex"
    assert agent.output_schema is PlannerOutput


def test_make_agent_subtask_doer_returns_writes_files_without_envelope_schema() -> None:
    """Plan 47: doer role has no envelope schema; the transport runs in
    plain-text mode and the wrapper invokes `invoke_raw`."""
    cfg = _cfg()
    agent = make_agent("subtask_doer", cfg)
    assert isinstance(agent, WritesFilesAgent)
    assert agent.envelope_schema is None
    assert isinstance(agent.transport, QuotaFallbackJsonAgent)
    assert isinstance(agent.transport.primary, CodexLitellmJsonAgent)
    assert agent.transport.primary.profile == "glm-zai"
    # Plan 59 fix A + E': no in-transport quota cap; every transport
    # in the chain fast-fails on quota.
    assert not hasattr(agent.transport.primary, "quota_max_total_wait_s")
    assert len(agent.transport.fallbacks) == 3
    assert isinstance(agent.transport.fallbacks[0], CodexLitellmJsonAgent)
    assert agent.transport.fallbacks[0].profile == "glm-wafer"
    assert not hasattr(agent.transport.fallbacks[0], "quota_max_total_wait_s")
    assert isinstance(agent.transport.fallbacks[1], ClaudeJsonAgent)
    assert agent.transport.fallbacks[1].model_id == "claude-sonnet-4-6"
    assert isinstance(agent.transport.fallbacks[2], CodexDirectJsonAgent)
    assert agent.transport.fallbacks[2].profile == "codex"


def test_make_agent_subtask_doer_override_to_claude_opus() -> None:
    cfg = _cfg(subtask_doer_model="claude-opus-4-7")
    agent = make_agent("subtask_doer", cfg)
    assert isinstance(agent, WritesFilesAgent)
    # Plan 60 fix 2: claude-opus-4-7 declares a quota fallback chain
    # (claude-sonnet-4-6 → gpt-5.5), so the make_agent layer wraps the
    # primary `ClaudeJsonAgent` in a `QuotaFallbackJsonAgent`. Drill in
    # one level to assert the primary transport.
    assert isinstance(agent.transport, QuotaFallbackJsonAgent)
    assert isinstance(agent.transport.primary, ClaudeJsonAgent)
    assert agent.transport.primary.model_id == "claude-opus-4-7[1m]"
    assert isinstance(agent.transport.fallbacks[0], ClaudeJsonAgent)
    assert agent.transport.fallbacks[0].model_id == "claude-sonnet-4-6"
    assert isinstance(agent.transport.fallbacks[1], CodexDirectJsonAgent)
    assert agent.transport.fallbacks[1].profile == "gpt5"
    assert agent.envelope_schema is None


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
