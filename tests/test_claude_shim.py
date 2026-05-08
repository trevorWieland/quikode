"""Plan 38 PR-A: claude-shim integration tests with mocked exec_in."""

from __future__ import annotations

import json
from unittest.mock import patch

from quikode.agent_schemas import ProgressVerdict
from quikode.agents.json_claude import ClaudeJsonAgent


def test_claude_invocation_includes_model_and_schema() -> None:
    captured: dict = {}

    def fake_exec_in(handle, cmd, log_path=None, stdin=None, timeout=None):
        captured["cmd"] = cmd
        captured["stdin"] = stdin
        envelope = {
            "type": "result",
            "result": "the assistant text",
            "structured_output": {"verdict": "progressing", "rationale": "ok"},
            "total_cost_usd": 0.005,
            "usage": {
                "input_tokens": 200,
                "output_tokens": 75,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 1000,
            },
        }
        return (0, json.dumps(envelope), "")

    agent = ClaudeJsonAgent(model_id="claude-opus-4-7[1m]")
    with (
        patch("quikode.agents.json_protocol.exec_in", side_effect=fake_exec_in),
        patch("quikode.agents.ccusage.fetch_session_stats", return_value=None),
    ):
        raw = agent.invoke(
            "the prompt",
            output_schema=ProgressVerdict,
            handle=object(),
            log_path=None,
            timeout=60,
        )
    assert raw.rc == 0
    assert raw.structured == {"verdict": "progressing", "rationale": "ok"}
    assert raw.tokens_input == 200
    assert raw.tokens_output == 75
    assert raw.cost_usd == 0.005
    cmd = captured["cmd"]
    assert cmd[0] == "bash" and cmd[1] == "-lc"
    shell_cmd = cmd[2]
    assert "claude -p" in shell_cmd
    assert "--permission-mode acceptEdits" in shell_cmd
    assert "--add-dir /workspace" in shell_cmd
    assert "--output-format json" in shell_cmd
    assert "--model claude-opus-4-7[1m]" in shell_cmd
    assert "--json-schema" in shell_cmd
    # Schema embedded in heredoc.
    assert "ProgressVerdict" in shell_cmd or "verdict" in shell_cmd
    assert captured["stdin"] == "the prompt"


def test_claude_handles_envelope_without_structured_output() -> None:
    """Older claude builds may emit only `result` text. Surface as raw_text
    + parse failure so the wrapper can record a parse error."""

    def fake_exec_in(handle, cmd, log_path=None, stdin=None, timeout=None):
        envelope = {
            "type": "result",
            "result": "just prose, no structured_output",
            "usage": {"input_tokens": 50, "output_tokens": 25},
            "total_cost_usd": 0.001,
        }
        return (0, json.dumps(envelope), "")

    agent = ClaudeJsonAgent(model_id="claude-opus-4-7[1m]")
    with (
        patch("quikode.agents.json_protocol.exec_in", side_effect=fake_exec_in),
        patch("quikode.agents.ccusage.fetch_session_stats", return_value=None),
    ):
        raw = agent.invoke(
            "p",
            output_schema=ProgressVerdict,
            handle=object(),
            log_path=None,
            timeout=60,
        )
    assert raw.rc == 0
    assert raw.structured is None
    assert raw.raw_text == "just prose, no structured_output"


def test_claude_handles_invalid_envelope() -> None:
    def fake_exec_in(handle, cmd, log_path=None, stdin=None, timeout=None):
        return (0, "not a JSON envelope", "")

    agent = ClaudeJsonAgent(model_id="claude-opus-4-7[1m]")
    with (
        patch("quikode.agents.json_protocol.exec_in", side_effect=fake_exec_in),
        patch("quikode.agents.ccusage.fetch_session_stats", return_value=None),
    ):
        raw = agent.invoke(
            "p",
            output_schema=ProgressVerdict,
            handle=object(),
            log_path=None,
            timeout=60,
        )
    assert raw.rc == 0
    assert raw.structured is None
    assert raw.raw_text == "not a JSON envelope"


def test_claude_handles_nonzero_rc() -> None:
    def fake_exec_in(handle, cmd, log_path=None, stdin=None, timeout=None):
        return (1, "", "claude error")

    agent = ClaudeJsonAgent(model_id="claude-opus-4-7[1m]")
    with (
        patch("quikode.agents.json_protocol.exec_in", side_effect=fake_exec_in),
        patch("quikode.agents.ccusage.fetch_session_stats", return_value=None),
    ):
        raw = agent.invoke(
            "p",
            output_schema=ProgressVerdict,
            handle=object(),
            log_path=None,
            timeout=60,
        )
    assert raw.rc == 1
    assert raw.structured is None


def test_claude_requires_output_schema() -> None:
    agent = ClaudeJsonAgent(model_id="claude-opus-4-7[1m]")
    try:
        agent.invoke("p", output_schema=None, handle=object(), log_path=None, timeout=60)
    except ValueError as e:
        assert "output_schema" in str(e)
    else:
        raise AssertionError("expected ValueError")
