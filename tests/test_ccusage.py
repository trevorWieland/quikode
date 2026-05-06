"""ccusage helper + agent-wrapper enrichment.

The helper shells out (host-side or via `exec_in` inside a task container)
to one of the three ccusage variants. We mock subprocess.run / exec_in
here — no real ccusage invocation, no real container needed.

Fixtures use real-shape JSON captured from the live variants on
2026-05-02 so we exercise the actual schema, not a hand-rolled stub.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from quikode.agents import ccusage
from quikode.agents import codex as codex_mod
from quikode.agents.claude import ClaudeAgent
from quikode.agents.codex import CodexAgent
from quikode.agents.opencode import OpencodeAgent
from quikode.types import AgentResult

# --- Real-shape fixtures (subset) -------------------------------------------

CLAUDE_SESSION_JSON = json.dumps(
    {
        "sessions": [
            {
                "sessionId": "session-1",
                "inputTokens": 1000,
                "outputTokens": 500,
                "cacheCreationTokens": 200,
                "cacheReadTokens": 5000,
                "totalTokens": 6700,
                "totalCost": 0.05,
                "lastActivity": "2026-05-02",
                "modelsUsed": ["claude-opus-4-7"],
                "projectPath": "Unknown Project",
            },
            {
                "sessionId": "session-2",
                "inputTokens": 2000,
                "outputTokens": 1000,
                "cacheCreationTokens": 0,
                "cacheReadTokens": 100,
                "totalTokens": 3100,
                "totalCost": 0.02,
                "lastActivity": "2026-05-02",
                "modelsUsed": ["claude-opus-4-7"],
                "projectPath": "Unknown Project",
            },
        ]
    }
)

CODEX_SESSION_JSON = json.dumps(
    {
        "sessions": [
            {
                "sessionId": "rollout-2026-05-02-foo",
                "lastActivity": "2026-05-02T12:00:00.000Z",
                "inputTokens": 10000,
                "cachedInputTokens": 8000,
                "outputTokens": 500,
                "reasoningOutputTokens": 200,
                "totalTokens": 10500,
                "costUSD": 0.30,
                "models": {"gpt-5.3-codex": {"isFallback": False}},
            }
        ]
    }
)

# Opencode prints a banner + warn line on stdout BEFORE the JSON. Verify our
# parser recovers from that.
OPENCODE_SESSION_RAW = (
    "[@ccusage/opencode]  WARN  Fetching latest model pricing from LiteLLM...\n"
    "[@ccusage/opencode] ℹ Loaded pricing for 2691 models\n"
    + json.dumps(
        {
            "sessions": [
                {
                    "sessionID": "ses_abc",
                    "sessionTitle": "test",
                    "parentID": None,
                    "inputTokens": 5000,
                    "outputTokens": 800,
                    "cacheCreationTokens": 0,
                    "cacheReadTokens": 12000,
                    "totalTokens": 17800,
                    "totalCost": 0.07,
                    "modelsUsed": ["zai-coding-plan/glm-5.1"],
                    "lastActivity": "2026-05-02T13:00:00.000Z",
                }
            ]
        }
    )
)

EMPTY_SESSIONS_JSON = json.dumps({"sessions": []})


# --- Test setup --------------------------------------------------------------


def _reset_cache() -> None:
    ccusage._reset_availability_cache()


def _fake_proc(rc: int, stdout: str, stderr: str = "") -> Any:
    p = MagicMock()
    p.returncode = rc
    p.stdout = stdout
    p.stderr = stderr
    return p


# --- Variant registry -------------------------------------------------------


def test_variant_for_known_clis():
    assert ccusage.variant_for("claude") == "ccusage@latest"
    assert ccusage.variant_for("codex") == "@ccusage/codex@latest"
    assert ccusage.variant_for("opencode") == "@ccusage/opencode@latest"


def test_variant_for_unknown_cli_returns_none():
    assert ccusage.variant_for("unknown") is None


# --- is_available ----------------------------------------------------------


def test_is_available_true_when_help_exits_zero():
    _reset_cache()
    with patch("subprocess.run", return_value=_fake_proc(0, "USAGE: ccusage")):
        assert ccusage.is_available("claude") is True


def test_is_available_false_when_command_missing():
    _reset_cache()
    with patch("subprocess.run", side_effect=FileNotFoundError("npx not found")):
        assert ccusage.is_available("claude") is False


def test_is_available_false_when_help_exits_nonzero():
    _reset_cache()
    with patch("subprocess.run", return_value=_fake_proc(1, "")):
        assert ccusage.is_available("opencode") is False


def test_is_available_false_for_unknown_cli():
    _reset_cache()
    # No subprocess call should happen — variant lookup short-circuits.
    with patch("subprocess.run") as run:
        assert ccusage.is_available("nonexistent") is False
        run.assert_not_called()


def test_is_available_caches_per_process():
    """Probe should run once across N is_available() calls for the same cli."""
    _reset_cache()
    with patch("subprocess.run", return_value=_fake_proc(0, "USAGE")) as run:
        for _ in range(5):
            ccusage.is_available("codex")
        assert run.call_count == 1


def test_is_available_cache_separates_clis():
    _reset_cache()
    with patch("subprocess.run", return_value=_fake_proc(0, "USAGE")) as run:
        ccusage.is_available("claude")
        ccusage.is_available("codex")
        ccusage.is_available("opencode")
        assert run.call_count == 3


def test_is_available_handles_subprocess_timeout():
    _reset_cache()
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("npx", 30)):
        assert ccusage.is_available("claude") is False


# --- fetch_session_stats: parsing -------------------------------------------


def test_fetch_session_stats_parses_claude_envelope():
    _reset_cache()
    with patch("subprocess.run", return_value=_fake_proc(0, CLAUDE_SESSION_JSON)):
        stats = ccusage.fetch_session_stats("claude")
    assert stats is not None
    assert stats.tokens_input == 3000
    assert stats.tokens_output == 1500
    assert stats.tokens_cached_read == 5100
    assert stats.tokens_cached_creation == 200
    assert abs(stats.cost_usd - 0.07) < 1e-9
    assert stats.total_tokens == 4500


def test_fetch_session_stats_parses_codex_envelope():
    _reset_cache()
    with patch("subprocess.run", return_value=_fake_proc(0, CODEX_SESSION_JSON)):
        stats = ccusage.fetch_session_stats("codex")
    assert stats is not None
    assert stats.tokens_input == 10000
    assert stats.tokens_output == 500
    # codex uses cachedInputTokens; cacheCreation isn't reported.
    assert stats.tokens_cached_read == 8000
    assert stats.tokens_cached_creation == 0
    assert abs(stats.cost_usd - 0.30) < 1e-9


def test_fetch_session_stats_parses_opencode_envelope_with_banner():
    """Opencode prints a banner before the JSON; we must skip past it."""
    _reset_cache()
    with patch("subprocess.run", return_value=_fake_proc(0, OPENCODE_SESSION_RAW)):
        stats = ccusage.fetch_session_stats("opencode")
    assert stats is not None
    assert stats.tokens_input == 5000
    assert stats.tokens_output == 800
    assert stats.tokens_cached_read == 12000


def test_fetch_session_stats_handles_empty_sessions():
    _reset_cache()
    with patch("subprocess.run", return_value=_fake_proc(0, EMPTY_SESSIONS_JSON)):
        stats = ccusage.fetch_session_stats("claude")
    assert stats is not None
    assert stats.tokens_input == 0
    assert stats.tokens_output == 0
    assert stats.cost_usd == 0.0


# --- fetch_session_stats: failure modes -------------------------------------


def test_fetch_session_stats_returns_none_on_timeout():
    _reset_cache()
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("npx", 5)):
        assert ccusage.fetch_session_stats("claude") is None


def test_fetch_session_stats_returns_none_on_nonzero_rc():
    _reset_cache()
    with patch("subprocess.run", return_value=_fake_proc(1, "", "boom")):
        assert ccusage.fetch_session_stats("claude") is None


def test_fetch_session_stats_returns_none_on_parse_error():
    _reset_cache()
    with patch("subprocess.run", return_value=_fake_proc(0, "not json at all")):
        assert ccusage.fetch_session_stats("claude") is None


def test_fetch_session_stats_returns_none_on_unknown_cli():
    _reset_cache()
    assert ccusage.fetch_session_stats("nonexistent") is None


def test_fetch_session_stats_handles_missing_sessions_field():
    _reset_cache()
    with patch("subprocess.run", return_value=_fake_proc(0, '{"foo": "bar"}')):
        assert ccusage.fetch_session_stats("claude") is None


def test_fetch_session_stats_returns_none_when_npx_missing():
    _reset_cache()
    with patch("subprocess.run", side_effect=FileNotFoundError("no npx")):
        assert ccusage.fetch_session_stats("claude") is None


# --- snapshot_delta ---------------------------------------------------------


def test_snapshot_delta_subtracts_before_from_after():
    before = ccusage.CCUsageStats(
        tokens_input=100,
        tokens_output=50,
        tokens_cached_read=200,
        tokens_cached_creation=10,
        cost_usd=0.01,
        raw_json="",
    )
    after = ccusage.CCUsageStats(
        tokens_input=300,
        tokens_output=80,
        tokens_cached_read=500,
        tokens_cached_creation=15,
        cost_usd=0.05,
        raw_json="",
    )
    delta = ccusage.snapshot_delta("claude", before, after)
    assert delta is not None
    assert delta.tokens_input == 200
    assert delta.tokens_output == 30
    assert delta.tokens_cached_read == 300
    assert delta.tokens_cached_creation == 5
    assert abs(delta.cost_usd - 0.04) < 1e-9


def test_snapshot_delta_no_baseline_returns_after_unchanged():
    after = ccusage.CCUsageStats(
        tokens_input=300,
        tokens_output=80,
        tokens_cached_read=500,
        tokens_cached_creation=15,
        cost_usd=0.05,
        raw_json="",
    )
    delta = ccusage.snapshot_delta("claude", None, after)
    assert delta == after


def test_snapshot_delta_no_after_returns_none():
    before = ccusage.CCUsageStats(
        tokens_input=10,
        tokens_output=10,
        tokens_cached_read=0,
        tokens_cached_creation=0,
        cost_usd=0.01,
        raw_json="",
    )
    assert ccusage.snapshot_delta("claude", before, None) is None


def test_snapshot_delta_clamps_outlier_cost_to_zero():
    """Live regression on R-0008: a single 86s subtask_doer call reported
    cost=$292.89 due to ccusage misattributing cumulative session totals
    to a fresh-baseline call. Sanity cap is $50; delta values above that
    are treated as parser noise and zeroed."""
    before = ccusage.CCUsageStats(
        tokens_input=0,
        tokens_output=0,
        tokens_cached_read=0,
        tokens_cached_creation=0,
        cost_usd=0.0,
        raw_json="",
    )
    after = ccusage.CCUsageStats(
        tokens_input=1000,
        tokens_output=500,
        tokens_cached_read=0,
        tokens_cached_creation=0,
        cost_usd=292.89,
        raw_json="",
    )
    delta = ccusage.snapshot_delta("opencode", before, after)
    assert delta is not None
    # Cost zeroed; tokens preserved (they're rarely the bug).
    assert delta.cost_usd == 0.0
    assert delta.tokens_input == 1000
    assert delta.tokens_output == 500


def test_snapshot_delta_no_baseline_clamps_outlier():
    """Same sanity cap applies on the no-baseline (None before) path.
    Without a baseline we attribute everything to this call, so an
    accumulated session-total can land here too."""
    after = ccusage.CCUsageStats(
        tokens_input=1000,
        tokens_output=500,
        tokens_cached_read=0,
        tokens_cached_creation=0,
        cost_usd=999.99,
        raw_json="",
    )
    delta = ccusage.snapshot_delta("opencode", None, after)
    assert delta is not None
    assert delta.cost_usd == 0.0


def test_snapshot_delta_passes_through_under_cap():
    """Normal-sized costs are not affected by the cap."""
    before = ccusage.CCUsageStats(
        tokens_input=0,
        tokens_output=0,
        tokens_cached_read=0,
        tokens_cached_creation=0,
        cost_usd=1.20,
        raw_json="",
    )
    after = ccusage.CCUsageStats(
        tokens_input=100,
        tokens_output=50,
        tokens_cached_read=0,
        tokens_cached_creation=0,
        cost_usd=2.50,
        raw_json="",
    )
    delta = ccusage.snapshot_delta("claude", before, after)
    assert delta is not None
    assert abs(delta.cost_usd - 1.30) < 1e-9


def test_snapshot_delta_clamps_negatives_to_zero():
    """Defensive: if `after` somehow has lower totals (shouldn't happen)
    we must not record a negative delta."""
    before = ccusage.CCUsageStats(
        tokens_input=100,
        tokens_output=100,
        tokens_cached_read=100,
        tokens_cached_creation=100,
        cost_usd=1.0,
        raw_json="",
    )
    after = ccusage.CCUsageStats(
        tokens_input=50,
        tokens_output=50,
        tokens_cached_read=50,
        tokens_cached_creation=50,
        cost_usd=0.5,
        raw_json="",
    )
    delta = ccusage.snapshot_delta("claude", before, after)
    assert delta is not None
    assert delta.tokens_input == 0
    assert delta.tokens_output == 0
    assert delta.cost_usd == 0.0


# --- merge_into_result ------------------------------------------------------


def test_merge_into_result_overrides_token_and_cost_fields():
    base = AgentResult(
        rc=0,
        stdout="response text",
        stderr="",
        tokens_used=0,
        duration_s=4.5,
    )
    stats = ccusage.CCUsageStats(
        tokens_input=1000,
        tokens_output=200,
        tokens_cached_read=500,
        tokens_cached_creation=10,
        cost_usd=0.04,
        raw_json="",
    )
    merged = ccusage.merge_into_result(base, stats)
    assert merged.rc == 0
    assert merged.stdout == "response text"
    assert merged.duration_s == 4.5
    assert merged.tokens_used == 1200
    assert merged.tokens_input == 1000
    assert merged.tokens_output == 200
    assert merged.tokens_cached_read == 500
    assert merged.tokens_cached_creation == 10
    assert abs(merged.cost_usd - 0.04) < 1e-9


def test_merge_into_result_preserves_transient_flag():
    base = AgentResult(
        rc=124,
        stdout="",
        stderr="timeout",
        transient=True,
        duration_s=30.0,
    )
    stats = ccusage.CCUsageStats(
        tokens_input=10,
        tokens_output=5,
        tokens_cached_read=0,
        tokens_cached_creation=0,
        cost_usd=0.001,
        raw_json="",
    )
    merged = ccusage.merge_into_result(base, stats)
    assert merged.transient is True
    assert merged.rc == 124


# --- Per-agent enrichment integration ---------------------------------------


def test_claude_wrapper_falls_back_to_ccusage_when_envelope_empty():
    """Claude wrapper: when the JSON envelope didn't produce token data,
    ccusage delta fills the gap.
    """
    agent = ClaudeAgent(model="claude-opus-4-7")
    handle = MagicMock()
    handle.container_name = "qk-test-claude"

    # _exec returns an AgentResult with non-JSON stdout (envelope parse fails).
    no_token_result = AgentResult(rc=0, stdout="not json", stderr="", duration_s=1.0)

    before_stats = ccusage.CCUsageStats(
        tokens_input=10,
        tokens_output=5,
        tokens_cached_read=0,
        tokens_cached_creation=0,
        cost_usd=0.001,
        raw_json="",
    )
    after_stats = ccusage.CCUsageStats(
        tokens_input=510,
        tokens_output=105,
        tokens_cached_read=200,
        tokens_cached_creation=20,
        cost_usd=0.05,
        raw_json="",
    )

    with (
        patch("quikode.agents.claude._exec", return_value=no_token_result),
        patch(
            "quikode.agents.ccusage.fetch_session_stats",
            side_effect=[before_stats, after_stats],
        ),
    ):
        result = agent.run("prompt", handle=handle)

    assert result.tokens_input == 500
    assert result.tokens_output == 100
    assert result.tokens_cached_read == 200
    assert result.tokens_cached_creation == 20
    assert abs(result.cost_usd - 0.049) < 1e-9


def test_claude_wrapper_keeps_envelope_when_envelope_has_data():
    """When the envelope already produced usage, ccusage isn't consulted
    for the after-snapshot path (envelope is per-call accurate).
    """
    agent = ClaudeAgent(model="claude-opus-4-7")
    handle = MagicMock()
    handle.container_name = "qk-test-claude-2"

    envelope_stdout = (
        '{"type":"result","result":"hi","total_cost_usd":0.02,'
        '"usage":{"input_tokens":300,"output_tokens":40,'
        '"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}'
    )
    envelope_result = AgentResult(rc=0, stdout=envelope_stdout, stderr="", duration_s=2.0)

    fetch = MagicMock(return_value=None)
    with (
        patch("quikode.agents.claude._exec", return_value=envelope_result),
        patch("quikode.agents.ccusage.fetch_session_stats", fetch),
    ):
        result = agent.run("prompt", handle=handle)

    assert result.tokens_input == 300
    assert result.tokens_output == 40
    assert abs(result.cost_usd - 0.02) < 1e-9
    # Envelope succeeded → after-snapshot fetch should not have been called.
    # (only the before-snapshot call happens; that's 1 invocation total)
    assert fetch.call_count == 1


def test_codex_wrapper_uses_ccusage_delta_for_uniform_data():
    """Codex wrapper: ccusage delta replaces the stderr-regex tokens_used
    with the full input/output/cost breakdown.
    """
    agent = CodexAgent(model="gpt-5.3-codex")
    handle = MagicMock()
    handle.container_name = "qk-test-codex"

    before_stats = ccusage.CCUsageStats(
        tokens_input=0,
        tokens_output=0,
        tokens_cached_read=0,
        tokens_cached_creation=0,
        cost_usd=0.0,
        raw_json="",
    )
    after_stats = ccusage.CCUsageStats(
        tokens_input=2000,
        tokens_output=300,
        tokens_cached_read=1500,
        tokens_cached_creation=0,
        cost_usd=0.08,
        raw_json="",
    )

    fetch_seq = [before_stats, after_stats]

    def fake_exec_in(_handle, _cmd, log_path=None, stdin=None, timeout=None):
        return 0, "the codex answer", "tokens used\n6,000\n"

    with (
        patch.object(codex_mod, "exec_in", side_effect=fake_exec_in),
        patch(
            "quikode.agents.ccusage.fetch_session_stats",
            side_effect=fetch_seq,
        ),
    ):
        result = agent.run("prompt", handle=handle)

    # ccusage takes precedence over the stderr regex (which extracted 6000).
    assert result.tokens_input == 2000
    assert result.tokens_output == 300
    assert result.tokens_used == 2300
    assert result.tokens_cached_read == 1500
    assert abs(result.cost_usd - 0.08) < 1e-9
    assert result.rc == 0
    assert result.stdout == "the codex answer"


def test_codex_wrapper_keeps_regex_total_when_ccusage_unavailable():
    """If ccusage returns None, the stderr-regex tokens_used is preserved."""
    agent = CodexAgent(model="gpt-5.3-codex")
    handle = MagicMock()
    handle.container_name = "qk-test-codex-fb"

    def fake_exec_in(_handle, _cmd, log_path=None, stdin=None, timeout=None):
        return 0, "answer", "tokens used\n6,201\n"

    with (
        patch.object(codex_mod, "exec_in", side_effect=fake_exec_in),
        patch("quikode.agents.ccusage.fetch_session_stats", return_value=None),
    ):
        result = agent.run("prompt", handle=handle)

    assert result.tokens_used == 6201
    # ccusage didn't fire, so we don't have a split.
    assert result.tokens_input is None


def test_opencode_wrapper_uses_ccusage_delta():
    """Opencode wrapper: previously 0 tokens; now reports ccusage delta."""
    agent = OpencodeAgent(model="zai-coding-plan/glm-5.1")
    handle = MagicMock()
    handle.container_name = "qk-test-opencode"

    no_token_result = AgentResult(rc=0, stdout="opencode response", stderr="", duration_s=8.0)
    before_stats = ccusage.CCUsageStats(
        tokens_input=100,
        tokens_output=10,
        tokens_cached_read=0,
        tokens_cached_creation=0,
        cost_usd=0.001,
        raw_json="",
    )
    after_stats = ccusage.CCUsageStats(
        tokens_input=5100,
        tokens_output=910,
        tokens_cached_read=12000,
        tokens_cached_creation=0,
        cost_usd=0.071,
        raw_json="",
    )

    with (
        patch("quikode.agents.opencode._exec", return_value=no_token_result),
        patch(
            "quikode.agents.ccusage.fetch_session_stats",
            side_effect=[before_stats, after_stats],
        ),
    ):
        result = agent.run("prompt", handle=handle)

    assert result.tokens_input == 5000
    assert result.tokens_output == 900
    assert result.tokens_cached_read == 12000
    assert abs(result.cost_usd - 0.07) < 1e-9
    assert result.rc == 0
    assert result.stdout == "opencode response"


def test_opencode_wrapper_preserves_result_when_ccusage_unavailable():
    """If ccusage fails, the result is unchanged (still 0 tokens — same as
    pre-refactor behavior; nothing is broken)."""
    agent = OpencodeAgent(model="zai-coding-plan/glm-5.1")
    handle = MagicMock()
    handle.container_name = "qk-test-opencode-fb"

    base_result = AgentResult(rc=0, stdout="response", stderr="", duration_s=5.0)

    with (
        patch("quikode.agents.opencode._exec", return_value=base_result),
        patch("quikode.agents.ccusage.fetch_session_stats", return_value=None),
    ):
        result = agent.run("prompt", handle=handle)

    assert result.rc == 0
    assert result.tokens_input is None
    assert result.tokens_used is None


def test_merge_into_result_rejects_non_agentresult():
    stats = ccusage.CCUsageStats(
        tokens_input=1,
        tokens_output=1,
        tokens_cached_read=0,
        tokens_cached_creation=0,
        cost_usd=0.0,
        raw_json="",
    )
    with pytest.raises(TypeError):
        ccusage.merge_into_result({"rc": 0}, stats)
