"""Token capture: claude `--output-format json` parser + agent_calls schema.

The claude wrapper wraps the assistant text in a JSON envelope that includes
input/output/cache_read/cache_creation tokens and total_cost_usd. We parse
that here and stash on AgentResult so the worker can persist it. Other CLIs
(codex, opencode) will be wired in follow-up work — they need different
parsing paths (codex stdout regex / ccusage shell-out for opencode).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from quikode.agents.claude import ClaudeAgent, _parse_claude_envelope
from quikode.state import Store
from quikode.types import AgentResult


def test_claude_envelope_parses_full_usage():
    raw = AgentResult(
        rc=0,
        stdout='{"type":"result","subtype":"success","result":"hello world",'
        '"total_cost_usd":0.0123,'
        '"usage":{"input_tokens":1234,"output_tokens":567,'
        '"cache_read_input_tokens":1000,"cache_creation_input_tokens":50}}',
        stderr="",
        duration_s=2.5,
    )
    parsed = _parse_claude_envelope(raw)
    assert parsed.stdout == "hello world"
    assert parsed.tokens_input == 1234
    assert parsed.tokens_output == 567
    assert parsed.tokens_cached_read == 1000
    assert parsed.tokens_cached_creation == 50
    assert parsed.tokens_used == 1234 + 567
    assert parsed.cost_usd == 0.0123
    # duration is preserved
    assert parsed.duration_s == 2.5


def test_claude_envelope_falls_back_on_non_json():
    raw = AgentResult(
        rc=0,
        stdout="just plain text from old claude version",
        stderr="",
    )
    parsed = _parse_claude_envelope(raw)
    # Non-JSON → return original; downstream code keeps working.
    assert parsed.stdout == "just plain text from old claude version"
    assert parsed.tokens_input is None


def test_claude_envelope_empty_stdout():
    raw = AgentResult(rc=1, stdout="", stderr="some error")
    parsed = _parse_claude_envelope(raw)
    assert parsed == raw


def test_claude_envelope_handles_missing_usage():
    raw = AgentResult(
        rc=0,
        stdout='{"type":"result","result":"reply text","total_cost_usd":0.001}',
        stderr="",
    )
    parsed = _parse_claude_envelope(raw)
    assert parsed.stdout == "reply text"
    # No usage block → all token fields stay None, but cost is captured.
    assert parsed.tokens_input is None
    assert parsed.tokens_output is None
    assert parsed.cost_usd == 0.001


def test_claude_envelope_handles_partial_usage():
    raw = AgentResult(
        rc=0,
        stdout='{"result":"x","usage":{"input_tokens":100,"output_tokens":50}}',
        stderr="",
    )
    parsed = _parse_claude_envelope(raw)
    assert parsed.tokens_input == 100
    assert parsed.tokens_output == 50
    assert parsed.tokens_used == 150
    assert parsed.tokens_cached_read is None
    assert parsed.tokens_cached_creation is None


def test_claude_shell_invocation_uses_json_output_format():
    """Regression: --output-format json must be in the invocation, otherwise
    we go back to plain-text and lose all the token data."""
    a = ClaudeAgent(model="claude-opus-4-7")
    cmd = a._shell_invocation()
    assert "--output-format json" in cmd
    assert "--add-dir /workspace" in cmd
    assert "--model claude-opus-4-7" in cmd


# ----- agent_calls schema -----


def test_agent_calls_has_token_split_columns(tmp_path):
    """Fresh DB should have all the v2.1 token-detail columns."""
    db = tmp_path / "fresh.db"
    Store(db).conn.close()
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_calls)")}
    conn.close()
    expected = {
        "tokens_input",
        "tokens_output",
        "tokens_cached_read",
        "tokens_cached_creation",
        "cost_usd",
    }
    missing = expected - cols
    assert not missing, f"missing token columns: {missing}"


def test_record_agent_call_persists_token_breakdown(tmp_path):
    """End-to-end: record a call with full token breakdown, read it back."""
    db = tmp_path / "live.db"
    s = Store(db)
    s.upsert_pending("R-001")
    s.record_agent_call(
        "R-001",
        phase="planner",
        cli="claude",
        model="claude-opus-4-7",
        rc=0,
        duration_s=3.4,
        tokens_used=1801,
        tokens_input=1234,
        tokens_output=567,
        tokens_cached_read=999,
        tokens_cached_creation=12,
        cost_usd=0.0123,
    )
    row = s.conn.execute(
        "SELECT tokens_used, tokens_input, tokens_output, tokens_cached_read, "
        "tokens_cached_creation, cost_usd FROM agent_calls WHERE task_id='R-001'"
    ).fetchone()
    assert row[0] == 1801
    assert row[1] == 1234
    assert row[2] == 567
    assert row[3] == 999
    assert row[4] == 12
    assert row[5] == 0.0123
    s.conn.close()


def test_record_agent_call_token_breakdown_optional(tmp_path):
    """Old call sites that don't pass the new fields should still work
    (backward compat — existing tests assume optional kwargs)."""
    db = tmp_path / "live.db"
    s = Store(db)
    s.upsert_pending("R-001")
    s.record_agent_call(
        "R-001",
        phase="doer",
        cli="opencode",
        model="zai-coding-plan/glm-5.1",
        rc=0,
        duration_s=12.0,
        tokens_used=None,
    )
    row = s.conn.execute("SELECT tokens_input, cost_usd FROM agent_calls WHERE task_id='R-001'").fetchone()
    assert row[0] is None
    assert row[1] is None
    s.conn.close()


_ = Path  # silence unused-import linter when tmp_path is the only Path use
