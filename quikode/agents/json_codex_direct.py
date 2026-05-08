"""Plan 38 PR-A: codex direct-OpenAI JSON-mode shim.

Used for codex profiles that talk to api.openai.com directly (the `gpt5`
and `codex` profiles per `~/.codex/config.toml` — `gpt-5.5` and
`gpt-5.3-codex` models). The CLI honors `--output-schema` natively:
the file at `--output-last-message` is guaranteed to be a JSON object
matching the schema (Tier 1 enforcement).

Verified at the command line on 2026-05-08 against codex 0.128.0 +
direct OpenAI Responses API.
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
)


class CodexDirectJsonAgent:
    """JSON-mode transport shim for direct OpenAI codex profiles.

    The shim:
    1. Writes `output_schema.model_json_schema()` to a tmp file in the
       sandbox (`/tmp/qk_codex_schema_<hex>.json`).
    2. Invokes `codex exec --profile <p> --output-schema <tmp> \
       --output-last-message <out> --skip-git-repo-check ...`.
    3. After exit, reads the `--output-last-message` file (the
       schema-conformant final assistant message), `json.loads`-es it,
       and returns it as `RawTransportResult.structured`.
    4. Cleans up both tmp files unconditionally (try/finally).
    """

    name = "codex_direct"
    schema_enforcement: str = "cli_native"

    def __init__(self, *, profile: str):
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
            raise ValueError("CodexDirectJsonAgent requires output_schema (cli_native enforcement)")
        token = secrets.token_hex(4)
        schema_path = f"/tmp/qk_codex_schema_{token}.json"
        out_path = f"/tmp/qk_codex_out_{token}.txt"
        schema_text = json.dumps(output_schema.model_json_schema())
        # Single shell invocation:
        #   1. write schema to schema_path via heredoc-ish printf
        #   2. invoke codex with --output-schema + --output-last-message
        #   3. cat the result so the wrapper can also see it on stdout
        #   4. always rm the tmp files (regardless of codex rc)
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
        # Use python instead of printf to safely write the schema (escape-free).
        write_schema = (
            f"python3 -c 'import sys, os; "
            f'open({schema_path!r},"w").write(sys.stdin.read())'
            f"' <<'__QK_SCHEMA_EOF__'\n{schema_text}\n__QK_SCHEMA_EOF__"
        )
        shell_cmd = (
            f"set -o pipefail; "
            f"{write_schema}; "
            f"{' '.join(codex_parts)} >&2; "
            f"_qk_rc=$?; "
            f"cat {out_path} 2>/dev/null; "
            f"rm -f {schema_path} {out_path}; "
            f"exit $_qk_rc"
        )
        cmd = ["bash", "-lc", shell_cmd]
        # ccusage snapshot brackets the call (plan 38 preserves token capture).
        before = ccusage.fetch_session_stats("codex", handle=handle)
        t0 = time.time()
        outcome = _run_with_retry(handle, cmd, stdin=prompt, log_path=log_path, timeout=timeout)
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
    """Translate the `_ExecOutcome` into a `RawTransportResult`.

    On rc != 0 we return the raw text + None structured — the caller's
    wrapper surfaces the failure. On rc == 0, parse the captured stdout
    (the cat'd output file contents) as JSON and place it in
    `structured`. JSON parse failures are surfaced as structured=None
    so the wrapper records a `parse_errors` line.
    """
    structured: dict | None = None
    parse_failure_excerpt = ""
    raw_text: str | None = None
    if outcome.rc == 0 and outcome.stdout.strip():
        try:
            obj = json.loads(outcome.stdout.strip())
            if isinstance(obj, dict):
                structured = obj
            else:
                parse_failure_excerpt = f"codex output was JSON but not an object: {type(obj).__name__}"
        except json.JSONDecodeError as e:
            parse_failure_excerpt = f"codex output was not valid JSON: {e}"
            raw_text = outcome.stdout
    elif outcome.rc != 0:
        raw_text = outcome.stdout
    # Token + cost enrichment via ccusage delta.
    after = ccusage.fetch_session_stats("codex", handle=handle)
    delta = ccusage.snapshot_delta("codex", ccusage_before, after)
    tokens_input: int | None = None
    tokens_output: int | None = None
    cost_usd: float | None = None
    if delta is not None and delta.total_tokens > 0:
        tokens_input = delta.tokens_input
        tokens_output = delta.tokens_output
        cost_usd = delta.cost_usd
    stderr_excerpt = (outcome.stderr or "")[-2000:]
    if parse_failure_excerpt:
        stderr_excerpt = (stderr_excerpt + "\n[quikode] " + parse_failure_excerpt).strip()
    return RawTransportResult(
        raw_text=raw_text,
        structured=structured,
        rc=outcome.rc,
        transient=outcome.timed_out,
        duration_s=duration_s,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cost_usd=cost_usd,
        stderr_excerpt=stderr_excerpt,
    )


__all__ = ["CodexDirectJsonAgent"]
