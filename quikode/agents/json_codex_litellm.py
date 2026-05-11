"""Plan 38 PR-A: codex-via-litellm JSON-mode shim (Tier 2 client_side).

Same codex invocation as `CodexDirectJsonAgent` but for codex profiles
that route through the local litellm proxy at `127.0.0.1:4000`
(`glm-zai`, `glm-wafer`, `minimax`, `deepseek`, `qwen` per
`~/.codex/config.toml`). litellm 1.83.10 drops `output_schema` during
Responses → Chat Completions translation AND the upstream providers
don't honor `response_format: json_schema` either, so the response is
free text. We pass `--output-schema` anyway (in case the proxy is
fixed someday) and parse with `model_validate_json` client-side; the
wrapper handles the re-prompt-once flow on `ValidationError`.

Verified at the command line on 2026-05-08 against codex 0.128.0 +
litellm 1.83.10.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from . import ccusage
from .json_protocol import (
    RawTransportResult,
    _ExecOutcome,
    _run_with_retry,
    codex_output_schema,
)


class CodexLitellmJsonAgent:
    """JSON-mode shim for codex profiles routed through litellm.

    Same shell invocation as `CodexDirectJsonAgent`, but the captured
    output is treated as free text (`raw_text`) — `structured` is always
    None, since litellm strips `output_schema` during translation.
    The wrapper layer parses with `model_validate_json` and re-prompts
    once on `ValidationError`.
    """

    name = "codex_litellm"
    schema_enforcement: str = "client_side"

    def __init__(self, *, profile: str):
        # Plan 59 fix E': the prior `quota_max_total_wait_s` knob is
        # gone. Quota exhaustion now fast-fails inside
        # `_run_with_retry` (no in-transport sleep), so there is no
        # cumulative-wait budget to thread through.
        self.profile = profile

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
            raise ValueError("CodexLitellmJsonAgent requires output_schema (used in re-prompt feedback)")
        token = secrets.token_hex(4)
        schema_path = f"/tmp/qk_codex_schema_{token}.json"
        out_path = f"/tmp/qk_codex_out_{token}.txt"
        schema_text = json.dumps(codex_output_schema(output_schema))
        codex_parts = [
            "codex",
            "exec",
            "--profile",
            self.profile,
            "--dangerously-bypass-approvals-and-sandbox",
            "--color",
            "never",
            "--cd",
            "/workspace",
            "--skip-git-repo-check",
            "--output-schema",
            schema_path,
            "--output-last-message",
            out_path,
            "-",
        ]
        write_schema = f"cat > {schema_path} <<'__QK_SCHEMA_EOF__'\n{schema_text}\n__QK_SCHEMA_EOF__\n"
        shell_cmd = (
            "set -o pipefail\n"
            f"{write_schema}"
            f"{' '.join(codex_parts)} >&2\n"
            "_qk_rc=$?\n"
            f"cat {out_path} 2>/dev/null\n"
            f"rm -f {schema_path} {out_path}\n"
            f"exit $_qk_rc"
        )
        cmd = ["bash", "-lc", shell_cmd]
        before = ccusage.fetch_session_stats("codex", handle=handle)
        t0 = time.time()
        outcome = _run_with_retry(
            handle,
            cmd,
            stdin=prompt,
            log_path=log_path,
            timeout=timeout,
        )
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
        """Plan 47 no-schema path: run codex (via litellm) in plain mode.

        Same shape as `CodexDirectJsonAgent.invoke_raw` — no schema
        flags, just `codex exec --profile <p> -`. The result carries
        free-text stdout in `raw_text` and `structured=None`.
        """
        codex_parts = [
            "codex",
            "exec",
            "--profile",
            self.profile,
            "--dangerously-bypass-approvals-and-sandbox",
            "--color",
            "never",
            "--cd",
            "/workspace",
            "--skip-git-repo-check",
            "-",
        ]
        cmd = ["bash", "-lc", " ".join(codex_parts)]
        before = ccusage.fetch_session_stats("codex", handle=handle)
        t0 = time.time()
        outcome = _run_with_retry(
            handle,
            cmd,
            stdin=prompt,
            log_path=log_path,
            timeout=timeout,
        )
        duration_s = time.time() - t0
        return _build_raw_result(
            outcome,
            duration_s=duration_s,
            handle=handle,
            ccusage_before=before,
        )


def _build_raw_result(
    outcome: _ExecOutcome,
    *,
    duration_s: float,
    handle: Any,
    ccusage_before: ccusage.CCUsageStats | None,
) -> RawTransportResult:
    """Translate `_ExecOutcome` → `RawTransportResult` (always client_side)."""
    raw_text: str | None = None
    if outcome.rc == 0 or outcome.stdout.strip():
        raw_text = outcome.stdout
    after = ccusage.fetch_session_stats("codex", handle=handle)
    delta = ccusage.snapshot_delta("codex", ccusage_before, after)
    tokens_input: int | None = None
    tokens_output: int | None = None
    cost_usd: float | None = None
    if delta is not None and delta.total_tokens > 0:
        tokens_input = delta.tokens_input
        tokens_output = delta.tokens_output
        cost_usd = delta.cost_usd
    return RawTransportResult(
        raw_text=raw_text,
        structured=None,  # client_side: parsing happens in the wrapper
        rc=outcome.rc,
        transient=outcome.timed_out,
        duration_s=duration_s,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cost_usd=cost_usd,
        stderr_excerpt=(outcome.stderr or "")[-2000:],
        category=outcome.category,
    )


__all__ = ["CodexLitellmJsonAgent"]
