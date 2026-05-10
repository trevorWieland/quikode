"""Plan 38 PR-A: claude JSON-mode shim (Tier 1 cli_native).

Invokes `claude -p --permission-mode acceptEdits --add-dir /workspace \
--output-format json --json-schema "$(...)" --model <id>`. The CLI
returns a JSON envelope with a `structured_output` field already
validated against the schema (Tier 1 — cli_native enforcement).

Verified at the command line on 2026-05-08: with `--json-schema` set
to a hello-world schema, the envelope's `structured_output` is the
schema-shaped payload (e.g. `{"greeting": "Hello!", "lucky_number": 7}`).

This shim refactors the JSON-envelope parsing of the existing
`agents/claude.py` cleanly — but does NOT modify the existing entry point.
PR-B will retire the existing when workers switch over.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from . import ccusage
from .json_protocol import (
    RawTransportResult,
    _ExecOutcome,
    _run_with_retry,
)


class ClaudeJsonAgent:
    """JSON-mode transport shim for the claude CLI."""

    name = "claude"
    schema_enforcement: str = "cli_native"

    def __init__(self, *, model_id: str):
        self.model_id = model_id

    def invoke(
        self,
        prompt: str,
        *,
        output_schema: type[BaseModel] | None,
        handle: Any,
        log_path: Path | None,
        timeout: int,
    ) -> RawTransportResult:
        if output_schema is None:
            raise ValueError("ClaudeJsonAgent requires output_schema (cli_native enforcement)")
        schema_text = json.dumps(output_schema.model_json_schema())
        # Inline the schema via $(cat <<'EOF') so we don't need a tmp file.
        # The single-quoted heredoc terminator preserves every byte of
        # `schema_text` literally. The outer `"$(...)"` then becomes the
        # --json-schema argument.
        invocation = (
            "claude -p --permission-mode acceptEdits --add-dir /workspace "
            "--output-format json "
            f"--model {self.model_id} "
            f"--json-schema \"$(cat <<'__QK_SCHEMA_EOF__'\n{schema_text}\n__QK_SCHEMA_EOF__\n)\""
        )
        cmd = ["bash", "-lc", invocation]
        before = ccusage.fetch_session_stats("claude", handle=handle)
        t0 = time.time()
        outcome = _run_with_retry(handle, cmd, stdin=prompt, log_path=log_path, timeout=timeout)
        duration_s = time.time() - t0
        return _build_raw_result(
            outcome,
            duration_s=duration_s,
            handle=handle,
            ccusage_before=before,
        )

    def invoke_raw(
        self,
        prompt: str,
        *,
        handle: Any,
        log_path: Path | None,
        timeout: int,
    ) -> RawTransportResult:
        """Plan 47 no-schema path: run claude in apply-edits mode.

        Drops `--json-schema` (and `--output-format json`) so the CLI
        emits plain assistant text. The doer's deliverable is the
        worktree diff, captured separately by the worker; this text
        is informational only. ccusage delta gives us tokens / cost.
        """
        invocation = f"claude -p --permission-mode acceptEdits --add-dir /workspace --model {self.model_id}"
        cmd = ["bash", "-lc", invocation]
        before = ccusage.fetch_session_stats("claude", handle=handle)
        t0 = time.time()
        outcome = _run_with_retry(handle, cmd, stdin=prompt, log_path=log_path, timeout=timeout)
        duration_s = time.time() - t0
        return _build_raw_text_result(
            outcome,
            duration_s=duration_s,
            handle=handle,
            ccusage_before=before,
        )


@dataclass
class _ParsedEnvelope:
    structured: dict | None
    raw_text: str | None
    tokens_input: int | None
    tokens_output: int | None
    cost_usd: float | None
    parse_failure_excerpt: str


def _parse_claude_envelope(stdout: str) -> _ParsedEnvelope:
    """Pull `structured_output` + usage + cost from the claude json envelope."""
    try:
        envelope = json.loads(stdout.strip())
    except json.JSONDecodeError as e:
        return _ParsedEnvelope(
            structured=None,
            raw_text=stdout,
            tokens_input=None,
            tokens_output=None,
            cost_usd=None,
            parse_failure_excerpt=f"claude envelope was not JSON: {e}",
        )
    if not isinstance(envelope, dict):
        return _ParsedEnvelope(
            structured=None,
            raw_text=stdout,
            tokens_input=None,
            tokens_output=None,
            cost_usd=None,
            parse_failure_excerpt="claude envelope was not a JSON object",
        )
    structured: dict | None = None
    raw_text: str | None = None
    parse_failure_excerpt = ""
    so = envelope.get("structured_output")
    if isinstance(so, dict):
        structured = so
    else:
        result_text = envelope.get("result")
        if isinstance(result_text, str):
            raw_text = result_text
        else:
            parse_failure_excerpt = "claude envelope had no structured_output and no string result"
    usage = envelope.get("usage") or {}
    tokens_input = _safe_int(usage.get("input_tokens")) if isinstance(usage, dict) else None
    tokens_output = _safe_int(usage.get("output_tokens")) if isinstance(usage, dict) else None
    cost_raw = envelope.get("total_cost_usd")
    try:
        cost_usd = float(cost_raw) if cost_raw is not None else None
    except (TypeError, ValueError):
        cost_usd = None
    return _ParsedEnvelope(
        structured=structured,
        raw_text=raw_text,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cost_usd=cost_usd,
        parse_failure_excerpt=parse_failure_excerpt,
    )


def _build_raw_result(
    outcome: _ExecOutcome,
    *,
    duration_s: float,
    handle: Any,
    ccusage_before: ccusage.CCUsageStats | None,
) -> RawTransportResult:
    """Parse the claude `--output-format json` envelope.

    Envelope shape (per `claude --output-format json` docs):

        {
          "type": "result",
          "result": "...assistant text...",
          "structured_output": {<schema-shaped JSON object>},
          "total_cost_usd": 0.024,
          "usage": {
            "input_tokens": 1234, "output_tokens": 567,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 1000
          }
        }

    `structured_output` is the schema-validated payload (Tier 1). Token
    + cost data come from the envelope directly; ccusage delta serves
    as a fallback when the envelope's usage block is missing.
    """
    if outcome.rc == 0 and outcome.stdout.strip():
        parsed = _parse_claude_envelope(outcome.stdout)
    else:
        parsed = _ParsedEnvelope(
            structured=None,
            raw_text=outcome.stdout if outcome.rc != 0 else None,
            tokens_input=None,
            tokens_output=None,
            cost_usd=None,
            parse_failure_excerpt="",
        )
    tokens_input = parsed.tokens_input
    tokens_output = parsed.tokens_output
    cost_usd = parsed.cost_usd
    if tokens_input is None and tokens_output is None and cost_usd is None:
        after = ccusage.fetch_session_stats("claude", handle=handle)
        delta = ccusage.snapshot_delta("claude", ccusage_before, after)
        if delta is not None and delta.total_tokens > 0:
            tokens_input = delta.tokens_input
            tokens_output = delta.tokens_output
            cost_usd = delta.cost_usd
    stderr_excerpt = (outcome.stderr or "")[-2000:]
    if parsed.parse_failure_excerpt:
        stderr_excerpt = (stderr_excerpt + "\n[quikode] " + parsed.parse_failure_excerpt).strip()
    return RawTransportResult(
        raw_text=parsed.raw_text,
        structured=parsed.structured,
        rc=outcome.rc,
        transient=outcome.timed_out,
        duration_s=duration_s,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cost_usd=cost_usd,
        stderr_excerpt=stderr_excerpt,
    )


def _safe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _build_raw_text_result(
    outcome: _ExecOutcome,
    *,
    duration_s: float,
    handle: Any,
    ccusage_before: ccusage.CCUsageStats | None,
) -> RawTransportResult:
    """Plan 47: package a no-schema claude invocation as a raw-text result.

    No JSON envelope to parse — claude's `-p` plain mode just emits
    assistant text on stdout. ccusage delta gives us tokens / cost.
    """
    raw_text: str | None = outcome.stdout if outcome.stdout else None
    after = ccusage.fetch_session_stats("claude", handle=handle)
    delta = ccusage.snapshot_delta("claude", ccusage_before, after)
    tokens_input: int | None = None
    tokens_output: int | None = None
    cost_usd: float | None = None
    if delta is not None and delta.total_tokens > 0:
        tokens_input = delta.tokens_input
        tokens_output = delta.tokens_output
        cost_usd = delta.cost_usd
    return RawTransportResult(
        raw_text=raw_text,
        structured=None,
        rc=outcome.rc,
        transient=outcome.timed_out,
        duration_s=duration_s,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cost_usd=cost_usd,
        stderr_excerpt=(outcome.stderr or "")[-2000:],
    )


__all__ = ["ClaudeJsonAgent"]
