"""Plan 38 PR-A: JSON-mode agent protocol + shared retry/quota helper.

Three transport shims (codex_direct, codex_litellm, claude) each return
a `RawTransportResult` carrying either a CLI-validated structured dict
(Tier 1: `cli_native`) or free text awaiting client-side validation
(Tier 2: `client_side`). The two wrapper classes (`JsonOutputAgent`,
`WritesFilesAgent`) consume that raw result, run pydantic validation
when needed, re-prompt once on `ValidationError` for `client_side`,
and surface `JsonAgentResult` to the role.

The shared helper `_run_with_retry` runs a transport callable inside
the same retry/quota/transient loop as the existing `agents.base._exec`,
but returns `RawTransportResult` instead of `AgentResult` — the existing
loop is too coupled to `AgentResult` (token-regex, ccusage merge baked
in) to share without breaking the existing call sites that PR-B will
later retire. Same retry semantics, fresh return shape.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

from ..execution import exec_in
from .transient_quota import _is_quota_exhausted, _is_transient_container_failure

log = logging.getLogger("quikode.agents.json")


# ---------- raw + processed result types ----------


@dataclass(frozen=True)
class RawTransportResult:
    """One transport-shim invocation result, before pydantic validation.

    `raw_text` carries free-text output for `client_side` enforcement
    (the agent layer parses with `model_validate_json`). `structured`
    carries an already-parsed dict for `cli_native` enforcement (the
    CLI guaranteed schema conformance — the agent layer only needs to
    `model_validate(structured)`).

    Exactly one of `raw_text` / `structured` is non-None on a successful
    call. Both can be None on a transport failure (rc != 0).
    """

    raw_text: str | None
    structured: dict[str, Any] | None
    rc: int
    transient: bool
    duration_s: float
    tokens_input: int | None = None
    tokens_output: int | None = None
    cost_usd: float | None = None
    stderr_excerpt: str = ""


@dataclass(frozen=True)
class JsonAgentResult:
    """One role-layer invocation result.

    `structured` is the validated pydantic instance. `parse_errors` is
    non-empty iff schema validation failed twice (after a re-prompt on
    `client_side` enforcement). `transient=True` triggers the worker's
    free-retry path (timeout, container OOM, docker glitch).
    """

    structured: BaseModel | None
    rc: int
    transient: bool
    duration_s: float
    tokens_input: int | None = None
    tokens_output: int | None = None
    cost_usd: float | None = None
    parse_errors: tuple[str, ...] = field(default_factory=tuple)
    raw_text: str | None = None
    stderr_excerpt: str = ""


# ---------- transport protocol ----------


@runtime_checkable
class JsonAgentTransport(Protocol):
    """The CLI-shim contract.

    Each shim wraps one CLI binary and one transport flavor (codex direct
    OpenAI, codex-via-litellm, claude). The shim writes the JSON Schema
    to a temp file (or inlines it on the command line, depending on the
    CLI), invokes the CLI via `execution.exec_in`, captures the output,
    cleans up tmp artifacts, and returns a `RawTransportResult`.

    `schema_enforcement` is widened to `str` here so concrete shims can
    each declare a tighter `Literal["cli_native"]` / `Literal["client_side"]`
    on their own attribute (matching the `ModelSpec.schema_enforcement`
    Literal in the registry) without breaking protocol structural
    subtyping. The wrapper layer reads the value via equality.
    """

    name: str
    schema_enforcement: str

    def invoke(
        self,
        prompt: str,
        *,
        output_schema: type[BaseModel] | None,
        handle: Any,
        log_path: Path | None,
        timeout: int,
    ) -> RawTransportResult: ...


# ---------- shared retry/quota helper ----------


def _quota_backoff_initial_s() -> int:
    return int(os.environ.get("QUIKODE_QUOTA_BACKOFF_INITIAL_S", "300"))


def _quota_backoff_max_s() -> int:
    return int(os.environ.get("QUIKODE_QUOTA_BACKOFF_MAX_S", "1800"))


def _quota_max_total_wait_s() -> int:
    return int(os.environ.get("QUIKODE_QUOTA_MAX_TOTAL_WAIT_S", "28800"))


@dataclass(frozen=True)
class _ExecOutcome:
    """Internal result of one wrapped `exec_in` call inside `_run_with_retry`."""

    rc: int
    stdout: str
    stderr: str
    timed_out: bool


def _run_with_retry(
    handle: Any,
    cmd: list[str],
    *,
    stdin: str | None,
    log_path: Path | None,
    timeout: int,
) -> _ExecOutcome:
    """Shared retry/quota/transient-detection loop for all JSON shims.

    Mirrors `agents.base._exec` semantics exactly:
    - per-call `timeout` enforced via `subprocess.TimeoutExpired` → rc=124,
      transient=True.
    - container-infra glitches (`_is_transient_container_failure`) → rc=124,
      transient=True (free retry by the worker).
    - subscription quota exhaustion (`_is_quota_exhausted`) → sleep with
      exponential backoff (5min → 10 → 20 → 30min cap) and retry the same
      call up to `_quota_max_total_wait_s`. The FSM never sees a
      quota-exhausted result.

    Returns `_ExecOutcome` carrying rc + stdout + stderr; the caller's
    transport shim translates into `RawTransportResult` (parsing structured
    output, computing duration, etc.).
    """
    backoff_s = _quota_backoff_initial_s()
    backoff_max_s = _quota_backoff_max_s()
    max_total_wait_s = _quota_max_total_wait_s()
    waited_s = 0
    quota_retries = 0
    while True:
        try:
            rc, out, err = exec_in(handle, cmd, log_path=log_path, stdin=stdin, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            partial_out = (
                (e.stdout.decode("utf-8", errors="replace") if e.stdout else "")
                if isinstance(e.stdout, bytes)
                else (e.stdout or "")
            )
            partial_err = (
                (e.stderr.decode("utf-8", errors="replace") if e.stderr else "")
                if isinstance(e.stderr, bytes)
                else (e.stderr or "")
            )
            msg = f"\n[quikode] agent timed out after {timeout}s; treating as failed attempt"
            if log_path is not None:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a") as f:
                    f.write(msg + "\n")
            return _ExecOutcome(
                rc=124,
                stdout=partial_out,
                stderr=(partial_err + msg).strip(),
                timed_out=True,
            )
        if _is_transient_container_failure(rc, err):
            annotation = (
                f"\n[quikode] container-level transient failure detected: rc={rc}; "
                f"treating as transient retry"
            )
            if log_path is not None:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a") as f:
                    f.write(annotation + "\n")
            return _ExecOutcome(
                rc=124,
                stdout=out,
                stderr=(err or "") + annotation,
                timed_out=True,
            )
        if _is_quota_exhausted(rc, out, err):
            if waited_s >= max_total_wait_s:
                give_up = (
                    f"\n[quikode] quota wait exceeded {max_total_wait_s}s after "
                    f"{quota_retries} retries; surfacing as failure to release worker"
                )
                log.warning(give_up.strip())
                if log_path is not None:
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    with log_path.open("a") as f:
                        f.write(give_up + "\n")
                return _ExecOutcome(
                    rc=rc,
                    stdout=out,
                    stderr=(err or "") + give_up,
                    timed_out=False,
                )
            sleep_s = backoff_s
            wait_msg = (
                f"\n[quikode] quota exhausted (rc={rc}); retry {quota_retries + 1}, "
                f"sleeping {sleep_s}s (cumulative {waited_s}s of {max_total_wait_s}s cap)"
            )
            log.warning(wait_msg.strip())
            if log_path is not None:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a") as f:
                    f.write(wait_msg + "\n")
            time.sleep(sleep_s)
            waited_s += sleep_s
            backoff_s = min(backoff_s * 2, backoff_max_s)
            quota_retries += 1
            continue
        return _ExecOutcome(rc=rc, stdout=out, stderr=err, timed_out=False)


# ---------- re-prompt helper ----------


_REPROMPT_TEMPLATE = """Your previous response failed schema validation:

{error}

Respond ONLY with valid JSON matching this exact schema:

{schema}

Do not include any prose, markdown fences, or explanations. The output \
must be parseable by `model_validate_json` directly."""


def build_reprompt(prompt: str, error: ValidationError, schema: type[BaseModel]) -> str:
    """Build the structured re-prompt for `client_side` validation failure.

    Embeds the pydantic ValidationError repr (capped at 2000 chars) and
    the schema's `model_json_schema()` pretty-printed. The original
    prompt is preserved as preamble so the agent retains all task context.
    """
    err_str = repr(error)[:2000]
    schema_str = json.dumps(schema.model_json_schema(), indent=2)
    feedback = _REPROMPT_TEMPLATE.format(error=err_str, schema=schema_str)
    return f"{prompt}\n\n---\n\n{feedback}"


# ---------- wrappers ----------


class JsonOutputAgent:
    """Wraps a `JsonAgentTransport` for non-writes-files roles.

    For `cli_native` enforcement: validates the transport's already-parsed
    `structured` dict via `output_schema.model_validate`. A validation
    failure here is unexpected (the CLI promised conformance) — we
    surface `parse_errors` without re-prompting.

    For `client_side` enforcement: parses `raw_text` via
    `output_schema.model_validate_json`. On `ValidationError`, re-prompts
    ONCE with `build_reprompt` and tries again. A second failure
    surfaces `parse_errors` non-empty so the worker can surface
    `parse_failure` to triage.
    """

    def __init__(self, transport: JsonAgentTransport, output_schema: type[BaseModel]):
        self.transport = transport
        self.output_schema = output_schema

    def invoke(
        self,
        prompt: str,
        *,
        handle: Any,
        log_path: Path | None = None,
        timeout: int,
    ) -> JsonAgentResult:
        return _invoke_with_validation(
            self.transport,
            prompt,
            output_schema=self.output_schema,
            handle=handle,
            log_path=log_path,
            timeout=timeout,
        )


class WritesFilesAgent:
    """Wraps a `JsonAgentTransport` for writes-files roles (doer / conflict-resolver).

    Same retry-on-validation semantics as `JsonOutputAgent`, but the
    structured output is the lightweight `DoerEnvelope` (or any envelope
    schema the role declares). The diff is the actual evidence — read
    separately by the worker via git, not extracted from this envelope.
    """

    def __init__(
        self,
        transport: JsonAgentTransport,
        envelope_schema: type[BaseModel],
    ):
        self.transport = transport
        self.envelope_schema = envelope_schema

    def invoke(
        self,
        prompt: str,
        *,
        handle: Any,
        log_path: Path | None = None,
        timeout: int,
    ) -> JsonAgentResult:
        return _invoke_with_validation(
            self.transport,
            prompt,
            output_schema=self.envelope_schema,
            handle=handle,
            log_path=log_path,
            timeout=timeout,
        )


def _result_from_raw(
    raw: RawTransportResult,
    *,
    structured: BaseModel | None,
    parse_errors: tuple[str, ...],
) -> JsonAgentResult:
    """Build a `JsonAgentResult` from a single transport result.

    Used by `_invoke_with_validation` to keep its branching shallow —
    every successful or failing path inside the cli_native branch and
    the first-try client_side branch funnels through here.
    """
    return JsonAgentResult(
        structured=structured,
        rc=raw.rc,
        transient=raw.transient,
        duration_s=raw.duration_s,
        tokens_input=raw.tokens_input,
        tokens_output=raw.tokens_output,
        cost_usd=raw.cost_usd,
        parse_errors=parse_errors,
        raw_text=raw.raw_text,
        stderr_excerpt=raw.stderr_excerpt,
    )


def _result_from_two_raw(
    first: RawTransportResult,
    second: RawTransportResult,
    *,
    structured: BaseModel | None,
    parse_errors: tuple[str, ...],
) -> JsonAgentResult:
    """Build a `JsonAgentResult` after a re-prompt round.

    Tokens / cost are summed across the two attempts; rc / transient /
    raw_text reflect the SECOND attempt (the one whose verdict the
    worker reads).
    """
    return JsonAgentResult(
        structured=structured,
        rc=second.rc,
        transient=second.transient,
        duration_s=first.duration_s + second.duration_s,
        tokens_input=_sum_opt(first.tokens_input, second.tokens_input),
        tokens_output=_sum_opt(first.tokens_output, second.tokens_output),
        cost_usd=_sum_opt(first.cost_usd, second.cost_usd),
        parse_errors=parse_errors,
        raw_text=second.raw_text,
        stderr_excerpt=second.stderr_excerpt,
    )


def _validate_cli_native(
    raw: RawTransportResult,
    output_schema: type[BaseModel],
) -> JsonAgentResult:
    """cli_native enforcement — validate the already-parsed dict, no retry."""
    if raw.structured is None:
        return _result_from_raw(
            raw,
            structured=None,
            parse_errors=(
                "cli_native transport returned structured=None; "
                "CLI envelope was missing the schema-validated payload",
            ),
        )
    try:
        instance = output_schema.model_validate(raw.structured)
    except ValidationError as e:
        return _result_from_raw(
            raw,
            structured=None,
            parse_errors=tuple(_format_validation_errors(e)),
        )
    return _result_from_raw(raw, structured=instance, parse_errors=())


def _validate_client_side(
    transport: JsonAgentTransport,
    prompt: str,
    raw: RawTransportResult,
    *,
    output_schema: type[BaseModel],
    handle: Any,
    log_path: Path | None,
    timeout: int,
) -> JsonAgentResult:
    """client_side enforcement — pydantic + structured re-prompt-once."""
    text = raw.raw_text or ""
    try:
        instance = output_schema.model_validate_json(text)
    except ValidationError as first_err:
        return _retry_after_validation_error(
            transport,
            prompt,
            raw,
            first_err=first_err,
            output_schema=output_schema,
            handle=handle,
            log_path=log_path,
            timeout=timeout,
        )
    return _result_from_raw(raw, structured=instance, parse_errors=())


def _retry_after_validation_error(
    transport: JsonAgentTransport,
    prompt: str,
    first: RawTransportResult,
    *,
    first_err: ValidationError,
    output_schema: type[BaseModel],
    handle: Any,
    log_path: Path | None,
    timeout: int,
) -> JsonAgentResult:
    """Re-prompt once after the first parse failure, parse the second
    response. Surface parse_errors non-empty if the second also fails."""
    first_errors = tuple(_format_validation_errors(first_err))
    reprompt_text = build_reprompt(prompt, first_err, output_schema)
    second = transport.invoke(
        reprompt_text,
        output_schema=output_schema,
        handle=handle,
        log_path=log_path,
        timeout=timeout,
    )
    if second.rc != 0 or second.raw_text is None:
        return _result_from_two_raw(
            first,
            second,
            structured=None,
            parse_errors=first_errors,
        )
    try:
        instance = output_schema.model_validate_json(second.raw_text)
    except ValidationError as second_err:
        second_errors = tuple(_format_validation_errors(second_err))
        return _result_from_two_raw(
            first,
            second,
            structured=None,
            parse_errors=first_errors + second_errors,
        )
    return _result_from_two_raw(first, second, structured=instance, parse_errors=())


def _invoke_with_validation(
    transport: JsonAgentTransport,
    prompt: str,
    *,
    output_schema: type[BaseModel],
    handle: Any,
    log_path: Path | None,
    timeout: int,
) -> JsonAgentResult:
    """Shared body for both `JsonOutputAgent` and `WritesFilesAgent`.

    Identical retry-on-validation semantics; the only difference between
    the two wrapper classes is the schema role and downstream usage of
    the diff.
    """
    raw = transport.invoke(
        prompt,
        output_schema=output_schema,
        handle=handle,
        log_path=log_path,
        timeout=timeout,
    )
    if raw.rc != 0 or (raw.raw_text is None and raw.structured is None):
        return _result_from_raw(raw, structured=None, parse_errors=())
    if transport.schema_enforcement == "cli_native":
        return _validate_cli_native(raw, output_schema)
    return _validate_client_side(
        transport,
        prompt,
        raw,
        output_schema=output_schema,
        handle=handle,
        log_path=log_path,
        timeout=timeout,
    )


def _format_validation_errors(e: ValidationError) -> list[str]:
    """Flatten a `ValidationError` into one string per error line for
    `parse_errors`. Used by both branches above."""
    out: list[str] = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()))
        msg = err.get("msg", "")
        out.append(f"{loc}: {msg}" if loc else msg)
    return out


def _sum_opt(a: int | float | None, b: int | float | None) -> Any:
    """Sum two optional numeric values, returning None when both are None."""
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


__all__ = [
    "JsonAgentResult",
    "JsonAgentTransport",
    "JsonOutputAgent",
    "RawTransportResult",
    "WritesFilesAgent",
    "build_reprompt",
]
