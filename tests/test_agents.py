"""Agent shell-invocation string tests. We don't actually invoke the binaries
— we just verify the constructed command line has the flags we need."""

from __future__ import annotations

import inspect

from quikode.agents.base import _is_quota_exhausted
from quikode.agents.claude import ClaudeAgent
from quikode.agents.codex import CodexAgent
from quikode.agents.opencode import OpencodeAgent


def test_claude_invocation_has_acceptedits_and_workspace_dir():
    a = ClaudeAgent(model="claude-opus-4-7")
    cmd = a._shell_invocation()
    assert "claude" in cmd
    assert "-p" in cmd
    assert "--permission-mode acceptEdits" in cmd
    assert "--add-dir /workspace" in cmd
    assert "--model claude-opus-4-7" in cmd


def test_codex_invocation_bypasses_inner_sandbox():
    """We're already inside a docker container — codex's bwrap-based sandbox
    fails inside containers and silently breaks file access. We must pass
    --dangerously-bypass-approvals-and-sandbox or the checker is blind."""
    CodexAgent(model="gpt-5.3-codex")
    # We can't easily inspect the full command since it's built dynamically per call.
    # Instead check by reading the source for the flag.
    src = inspect.getsource(CodexAgent.run)
    assert "--dangerously-bypass-approvals-and-sandbox" in src
    # ruff format may split args across lines; just check both pieces
    assert "--cd" in src and "/workspace" in src


def test_codex_invocation_captures_last_message_to_tempfile():
    """Codex prints a verbose preamble — we use --output-last-message to a
    tempfile to capture only the final answer."""
    src = inspect.getsource(CodexAgent.run)
    assert "--output-last-message" in src


def test_opencode_invocation_skips_permissions_and_sets_dir():
    a = OpencodeAgent(model="zai-coding-plan/glm-5.1")
    cmd = a._shell_invocation()
    assert "opencode run" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "--dir /workspace" in cmd
    assert "--model zai-coding-plan/glm-5.1" in cmd
    assert cmd.startswith("cat |")  # stdin pattern


# ----- quota-exhausted detection (plan 19A) -----


def test_quota_exhausted_zero_rc_never_matches():
    """Even if stdout contains '429' (e.g. an agent discussing rate-limit code),
    a successful agent call must not be classified as quota-exhausted."""
    assert _is_quota_exhausted(0, "implementing HTTP 429 handler", "") is False
    assert _is_quota_exhausted(0, "", "rate_limit_exceeded sample text") is False


def test_quota_exhausted_claude_session_limit():
    stderr = "You've hit your session limit · resets 3:45pm"
    assert _is_quota_exhausted(1, "", stderr) is True


def test_quota_exhausted_claude_weekly_limit():
    stderr = "You've hit your weekly limit · resets Mon 12:00am"
    assert _is_quota_exhausted(1, "", stderr) is True


def test_quota_exhausted_claude_opus_limit():
    stderr = "You've hit your Opus limit; please switch model."
    assert _is_quota_exhausted(1, "", stderr) is True


def test_quota_exhausted_codex_rate_limit_exceeded():
    """Codex JSONL emits turn.failed / error with code: 'rate_limit_exceeded'."""
    stderr = '{"type":"turn.failed","error":{"code":"rate_limit_exceeded"}}'
    assert _is_quota_exhausted(1, "", stderr) is True


def test_quota_exhausted_generic_429():
    """opencode forwards upstream 429s from zai-coding-plan / anthropic."""
    stderr = "HTTP 429 Too Many Requests"
    assert _is_quota_exhausted(1, "", stderr) is True


def test_quota_exhausted_quota_exceeded_phrasing():
    stderr = "Your usage limit has been exceeded for the current period."
    assert _is_quota_exhausted(1, "", stderr) is True


def test_quota_exhausted_unrelated_failure_does_not_match():
    """A normal compile error or panic shouldn't be classified as quota."""
    stderr = "error[E0599]: no method named `foo` found in scope"
    assert _is_quota_exhausted(101, "", stderr) is False
    stderr = "thread 'main' panicked at 'index out of bounds'"
    assert _is_quota_exhausted(101, "", stderr) is False
