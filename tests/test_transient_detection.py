"""Phase A.4: transient-vs-real failure detection at the agent layer.

`_exec` (and the codex wrapper, which builds its own AgentResult) must
recognize docker/container-level glitches — timeouts, OOM SIGKILLs,
docker-daemon errors — and surface them as `AgentResult(rc=124,
transient=True)`. The worker (next batch) will use that flag to free-retry
instead of burning the real-failure retry budget.

This batch only validates the detection primitive; worker.py is untouched.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from quikode.agents.base import _exec, _is_transient_container_failure
from quikode.agents.claude import ClaudeAgent
from quikode.agents.codex import CodexAgent
from quikode.types import AgentResult


class _StubHandle:
    container_name = "qk-stub"


# ----- _is_transient_container_failure -----


def test_is_transient_rc137_with_any_stderr():
    """OOM SIGKILL almost always means the box died mid-exec."""
    assert _is_transient_container_failure(137, "")
    assert _is_transient_container_failure(137, "anything at all")
    assert _is_transient_container_failure(137, "killed by signal")


def test_is_transient_daemon_error_phrase():
    assert _is_transient_container_failure(125, "Error response from daemon: oh no")


def test_is_transient_cannot_connect_phrase():
    assert _is_transient_container_failure(
        126, "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
    )


def test_is_transient_context_deadline():
    assert _is_transient_container_failure(1, "context deadline exceeded while waiting for x")


def test_is_transient_container_not_running():
    assert _is_transient_container_failure(1, "Error: container not running")


def test_not_transient_rc0():
    assert not _is_transient_container_failure(0, "Error response from daemon")
    assert not _is_transient_container_failure(0, "")


def test_not_transient_normal_checker_fail():
    """The bread-and-butter case: checker returned VERDICT: FAIL with a real
    root cause. That is NOT transient — it should burn a real retry."""
    stderr = "VERDICT: FAIL\nROOT_CAUSE: missing import in foo.py at line 12\n"
    assert not _is_transient_container_failure(1, stderr)


def test_not_transient_rc1_empty_stderr():
    assert not _is_transient_container_failure(1, "")


# ----- AgentResult.transient default -----


def test_agent_result_transient_defaults_false():
    r = AgentResult(rc=0, stdout="", stderr="")
    assert r.transient is False


# ----- _exec → transient on TimeoutExpired -----


def test_exec_timeout_marks_transient():
    """Timeout was the seed case for this whole feature; the synthetic
    AgentResult must now also carry transient=True so the worker (next
    batch) can free-retry."""

    def fake_exec_in(handle, cmd, log_path=None, stdin=None, timeout=None):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout or 0, output=b"partial", stderr=b"")

    with patch("quikode.agents.base.exec_in", side_effect=fake_exec_in):
        result = _exec(_StubHandle(), ["bash", "-lc", "x"], timeout=5)
    assert result.rc == 124
    assert result.transient is True


# ----- _exec → transient on rc=137 (OOM) -----


def test_exec_rc137_marks_transient():
    def fake_exec_in(handle, cmd, log_path=None, stdin=None, timeout=None):
        return 137, "", "killed"

    with patch("quikode.agents.base.exec_in", side_effect=fake_exec_in):
        result = _exec(_StubHandle(), ["x"], timeout=5)
    assert result.rc == 124  # rewritten to canonical "transient" rc
    assert result.transient is True
    assert "rc=137" in result.stderr
    assert "transient retry" in result.stderr


def test_exec_daemon_error_marks_transient():
    def fake_exec_in(handle, cmd, log_path=None, stdin=None, timeout=None):
        return 125, "", "Error response from daemon: container is not running"

    with patch("quikode.agents.base.exec_in", side_effect=fake_exec_in):
        result = _exec(_StubHandle(), ["x"])
    assert result.rc == 124
    assert result.transient is True
    assert "rc=125" in result.stderr  # original rc preserved in diagnostic text


# ----- _exec → NOT transient for normal agent failure -----


def test_exec_normal_rc1_not_transient():
    def fake_exec_in(handle, cmd, log_path=None, stdin=None, timeout=None):
        return 1, "VERDICT: FAIL\nROOT_CAUSE: real bug\n", ""

    with patch("quikode.agents.base.exec_in", side_effect=fake_exec_in):
        result = _exec(_StubHandle(), ["x"])
    assert result.rc == 1
    assert result.transient is False


def test_exec_rc0_not_transient():
    def fake_exec_in(handle, cmd, log_path=None, stdin=None, timeout=None):
        return 0, "ok", ""

    with patch("quikode.agents.base.exec_in", side_effect=fake_exec_in):
        result = _exec(_StubHandle(), ["x"])
    assert result.rc == 0
    assert result.transient is False


# ----- Wrapper agents propagate transient through their post-processing -----


def test_claude_agent_preserves_transient_flag():
    """ClaudeAgent.run wraps _exec output via model_copy(update=...) — that
    must not strip the transient field set by the timeout/rc-137 catches."""
    transient_result = AgentResult(rc=124, stdout="", stderr="timed out", transient=True)
    with patch("quikode.agents.claude._exec", return_value=transient_result):
        out = ClaudeAgent().run("prompt", handle=_StubHandle())
    assert out.rc == 124
    assert out.transient is True


def test_claude_agent_envelope_parse_preserves_transient():
    """Even when claude returns a parseable JSON envelope, the wrapper must
    not silently drop transient=True (e.g., from a partial-success
    timeout). It's an extreme edge case but the model_copy path should
    just preserve the field by default."""
    envelope_stdout = (
        '{"type":"result","result":"hi","total_cost_usd":0.01,"usage":{"input_tokens":10,"output_tokens":5}}'
    )
    transient_result = AgentResult(rc=0, stdout=envelope_stdout, stderr="", transient=True)
    with patch("quikode.agents.claude._exec", return_value=transient_result):
        out = ClaudeAgent().run("prompt", handle=_StubHandle())
    # Envelope unwrapped, but transient flag preserved.
    assert out.stdout == "hi"
    assert out.transient is True


def test_codex_agent_marks_transient_on_rc137():
    """Codex builds a fresh AgentResult locally (doesn't go through _exec),
    so it has its own transient detection. Validate that path."""

    def fake_exec_in(handle, cmd, log_path=None, stdin=None, timeout=None):
        return 137, "", "oom-killed"

    with patch("quikode.agents.codex.exec_in", side_effect=fake_exec_in):
        out = CodexAgent().run("prompt", handle=_StubHandle())
    assert out.rc == 124
    assert out.transient is True
    assert "rc=137" in out.stderr


def test_codex_agent_normal_failure_not_transient():
    def fake_exec_in(handle, cmd, log_path=None, stdin=None, timeout=None):
        return 1, "VERDICT: FAIL", ""

    with patch("quikode.agents.codex.exec_in", side_effect=fake_exec_in):
        out = CodexAgent().run("prompt", handle=_StubHandle())
    assert out.rc == 1
    assert out.transient is False
