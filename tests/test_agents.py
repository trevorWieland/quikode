"""Agent shell-invocation string tests. We don't actually invoke the binaries
— we just verify the constructed command line has the flags we need."""

from __future__ import annotations

import inspect

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
