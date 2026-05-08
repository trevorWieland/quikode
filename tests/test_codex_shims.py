"""Plan 38 PR-A: codex-shim integration tests with mocked exec_in.

Verifies the shell incantation each shim produces — correct profile,
correct flags, schema file written + cleaned up, output file read +
cleaned up. NO real CLI calls.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from quikode.agent_schemas import DoerEnvelope, ProgressVerdict
from quikode.agents.json_codex_direct import CodexDirectJsonAgent
from quikode.agents.json_codex_litellm import CodexLitellmJsonAgent


def _fake_ccusage_no_data() -> None:
    """Patch ccusage so it doesn't try to shell out during these tests."""


# ---------- codex_direct ----------


def test_codex_direct_invocation_includes_profile_and_schema() -> None:
    captured: dict = {}

    def fake_exec_in(handle, cmd, log_path=None, stdin=None, timeout=None):
        captured["cmd"] = cmd
        captured["stdin"] = stdin
        captured["timeout"] = timeout
        # Simulate a successful codex run that wrote the schema-validated payload
        # to <out_path>; the shim's shell pipeline cat's it to stdout. We
        # mimic that by returning a JSON dict-shaped output here.
        out = json.dumps({"verdict": "progressing", "rationale": "ok"})
        return (0, out, "")

    agent = CodexDirectJsonAgent(profile="gpt5")
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
    assert raw.structured is not None
    assert raw.structured["verdict"] == "progressing"
    assert raw.transient is False
    # Verify the codex command was assembled correctly.
    cmd = captured["cmd"]
    assert cmd[0] == "bash" and cmd[1] == "-lc"
    shell_cmd = cmd[2]
    assert "--profile gpt5" in shell_cmd
    assert "--output-schema /tmp/qk_codex_schema_" in shell_cmd
    assert "--output-last-message /tmp/qk_codex_out_" in shell_cmd
    assert "--skip-git-repo-check" in shell_cmd
    # Tmp file cleanup is part of the same shell pipeline.
    assert "rm -f /tmp/qk_codex_schema_" in shell_cmd
    assert "rm -f" in shell_cmd  # belt-and-suspenders
    # The schema text is embedded in the heredoc.
    assert "ProgressVerdict" in shell_cmd or "verdict" in shell_cmd
    # The prompt is passed via stdin.
    assert captured["stdin"] == "the prompt"
    assert captured["timeout"] == 60


def test_codex_direct_handles_invalid_json_on_stdout() -> None:
    """When the captured output isn't valid JSON, surface as parse failure
    (raw_text populated, structured=None) without crashing."""

    def fake_exec_in(handle, cmd, log_path=None, stdin=None, timeout=None):
        return (0, "not valid json at all", "")

    agent = CodexDirectJsonAgent(profile="gpt5")
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
    assert raw.raw_text is not None
    assert "not valid JSON" in raw.stderr_excerpt or raw.raw_text


def test_codex_direct_handles_nonzero_rc() -> None:
    def fake_exec_in(handle, cmd, log_path=None, stdin=None, timeout=None):
        return (1, "", "codex exited with error")

    agent = CodexDirectJsonAgent(profile="gpt5")
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


def test_codex_direct_requires_output_schema() -> None:
    agent = CodexDirectJsonAgent(profile="gpt5")
    try:
        agent.invoke("p", output_schema=None, handle=object(), log_path=None, timeout=60)
    except ValueError as e:
        assert "output_schema" in str(e)
    else:
        raise AssertionError("expected ValueError")


# ---------- codex_litellm ----------


def test_codex_litellm_invocation_produces_raw_text() -> None:
    """litellm path: stdout is treated as free text (raw_text), structured
    is always None — the wrapper's client_side path will parse + reprompt."""

    captured: dict = {}

    def fake_exec_in(handle, cmd, log_path=None, stdin=None, timeout=None):
        captured["cmd"] = cmd
        # litellm path: would return free text in real life. Even when the
        # text happens to be valid JSON, the shim does NOT pre-parse it.
        return (0, json.dumps({"summary": "x", "files_touched": []}), "")

    agent = CodexLitellmJsonAgent(profile="glm-zai")
    with (
        patch("quikode.agents.json_protocol.exec_in", side_effect=fake_exec_in),
        patch("quikode.agents.ccusage.fetch_session_stats", return_value=None),
    ):
        raw = agent.invoke(
            "p",
            output_schema=DoerEnvelope,
            handle=object(),
            log_path=None,
            timeout=60,
        )
    assert raw.rc == 0
    assert raw.structured is None
    assert raw.raw_text is not None
    cmd = captured["cmd"]
    shell_cmd = cmd[2]
    assert "--profile glm-zai" in shell_cmd
    assert "--output-schema /tmp/qk_codex_schema_" in shell_cmd  # passed but litellm drops it
    assert "rm -f /tmp/qk_codex_schema_" in shell_cmd  # cleanup


def test_codex_litellm_requires_output_schema() -> None:
    """Even though enforcement is client_side, the schema is required for the
    re-prompt feedback loop."""
    agent = CodexLitellmJsonAgent(profile="glm-zai")
    try:
        agent.invoke("p", output_schema=None, handle=object(), log_path=None, timeout=60)
    except ValueError as e:
        assert "output_schema" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_codex_litellm_timeout_produces_transient_result() -> None:
    """When exec_in raises subprocess.TimeoutExpired, the shared retry helper
    returns a transient outcome; the shim translates to a transient
    RawTransportResult."""

    def fake_exec_in(handle, cmd, log_path=None, stdin=None, timeout=None):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout or 1, output=b"", stderr=b"")

    agent = CodexLitellmJsonAgent(profile="glm-zai")
    with (
        patch("quikode.agents.json_protocol.exec_in", side_effect=fake_exec_in),
        patch("quikode.agents.ccusage.fetch_session_stats", return_value=None),
    ):
        raw = agent.invoke(
            "p",
            output_schema=DoerEnvelope,
            handle=object(),
            log_path=Path("/tmp/test-lit-log"),
            timeout=1,
        )
    assert raw.rc == 124
    assert raw.transient is True
