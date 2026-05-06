"""claude-code headless wrapper.

Invocation:
  claude -p --permission-mode acceptEdits [--model X] [--add-dir /workspace] \
         --output-format json

`--output-format json` makes claude emit a single JSON envelope to stdout
that wraps the assistant's text response and includes per-call usage
metadata: input/output/cache_read/cache_creation tokens plus
total_cost_usd. We unpack that here so the rest of the pipeline sees a
plain text response and rich token data on AgentResult.

Prompt is piped via stdin (claude reads stdin when -p has no positional arg).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from ..types import AgentResult
from . import ccusage
from .base import _exec


class ClaudeAgent:
    name = "claude"

    def __init__(self, model: str | None = None, extra_args: list[str] | None = None):
        self.model = model
        self.extra_args = list(extra_args or [])

    def run(
        self, prompt: str, *, handle: object, log_path: Path | None = None, timeout: int | None = None
    ) -> AgentResult:
        cmd = ["bash", "-lc", self._shell_invocation()]
        # Snapshot ccusage totals before the call so we can fall back to a
        # delta if the JSON envelope didn't yield usage data (e.g. claude
        # crashed mid-stream and stdout isn't valid JSON).
        before = ccusage.fetch_session_stats("claude", handle=handle)
        result = _exec(handle, cmd, stdin=prompt, log_path=log_path, timeout=timeout)
        result = _parse_claude_envelope(result)
        if not _has_token_data(result):
            after = ccusage.fetch_session_stats("claude", handle=handle)
            delta = ccusage.snapshot_delta("claude", before, after)
            if delta is not None and delta.total_tokens > 0:
                result = ccusage.merge_into_result(result, delta)
        return result

    def _shell_invocation(self) -> str:
        # Note: -p has no positional → claude reads from stdin
        parts = [
            "claude",
            "-p",
            "--permission-mode",
            "acceptEdits",
            "--add-dir",
            "/workspace",
            "--output-format",
            "json",
        ]
        if self.model:
            parts += ["--model", self.model]
        parts += self.extra_args
        return " ".join(parts)


def _parse_claude_envelope(result: AgentResult) -> AgentResult:
    """Extract claude-code's `--output-format json` envelope.

    Shape (from `claude --output-format json` docs):
        {
          "type": "result",
          "result": "...assistant text...",
          "total_cost_usd": 0.024,
          "usage": {
            "input_tokens": 1234,
            "output_tokens": 567,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 1000
          },
          ...
        }

    On a parse failure (claude printed something non-JSON), return the
    original AgentResult so callers see whatever stdout actually contained.
    """
    if not result.stdout.strip():
        return result
    try:
        envelope = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return result
    if not isinstance(envelope, dict):
        return result
    text = envelope.get("result", "")
    if not isinstance(text, str):
        return result
    usage = envelope.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    inp = _safe_int(usage.get("input_tokens"))
    out = _safe_int(usage.get("output_tokens"))
    cache_read = _safe_int(usage.get("cache_read_input_tokens"))
    cache_create = _safe_int(usage.get("cache_creation_input_tokens"))
    total = (inp or 0) + (out or 0) if (inp is not None or out is not None) else None
    cost = envelope.get("total_cost_usd")
    cost_f: float | None
    try:
        cost_f = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost_f = None

    return result.model_copy(
        update={
            "stdout": text,
            "tokens_used": total if total is not None else result.tokens_used,
            "tokens_input": inp,
            "tokens_output": out,
            "tokens_cached_read": cache_read,
            "tokens_cached_creation": cache_create,
            "cost_usd": cost_f,
        }
    )


def _has_token_data(result: AgentResult) -> bool:
    """True iff the JSON envelope produced any usable token data."""
    return bool(result.tokens_used or result.tokens_input or result.tokens_output)


def _safe_int(v: object) -> int | None:
    if v is None:
        return None
    try:
        n = int(cast(Any, v))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None
