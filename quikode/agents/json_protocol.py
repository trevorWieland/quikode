"""JSON-mode agent protocol, schema validation, and retry/quota helpers."""

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
from .transient_quota import (
    _is_quota_exhausted,
    _is_transient_agent_auth_failure,
    _is_transient_container_failure,
)

log = logging.getLogger("quikode.agents.json")


# ---------- raw + processed result types ----------


@dataclass(frozen=True)
class RawTransportResult:
    """One transport invocation result before pydantic validation."""

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
    """One role-layer invocation result."""

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
    """The CLI-shim contract."""

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


def _auth_backoff_initial_s() -> int:
    return int(os.environ.get("QUIKODE_AUTH_BACKOFF_INITIAL_S", "15"))


def _auth_backoff_max_s() -> int:
    return int(os.environ.get("QUIKODE_AUTH_BACKOFF_MAX_S", "120"))


def _auth_max_total_wait_s() -> int:
    return int(os.environ.get("QUIKODE_AUTH_MAX_TOTAL_WAIT_S", "900"))


@dataclass(frozen=True)
class _ExecOutcome:
    """Internal result of one wrapped `exec_in` call inside `_run_with_retry`."""

    rc: int
    stdout: str
    stderr: str
    timed_out: bool


@dataclass
class _BackoffState:
    next_s: int
    max_s: int
    waited_s: int
    max_total_s: int
    retries: int = 0


def _append_agent_log(log_path: Path | None, message: str) -> None:
    if log_path is None:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(message + "\n")


def _handle_agent_auth_retry(
    *,
    rc: int,
    out: str,
    err: str,
    state: _BackoffState,
    log_path: Path | None,
) -> tuple[bool, _ExecOutcome | None]:
    if not _is_transient_agent_auth_failure(rc, out, err):
        return False, None
    if state.waited_s >= state.max_total_s:
        give_up = (
            f"\n[quikode] agent auth refresh retry exceeded {state.max_total_s}s "
            f"after {state.retries} retries; surfacing as transient transport failure"
        )
        log.warning(give_up.strip())
        _append_agent_log(log_path, give_up)
        return True, _ExecOutcome(
            rc=124,
            stdout=out,
            stderr=(err or "") + give_up,
            timed_out=True,
        )
    sleep_s = state.next_s
    wait_msg = (
        f"\n[quikode] agent auth refresh failure (rc={rc}); retry {state.retries + 1}, "
        f"sleeping {sleep_s}s (cumulative {state.waited_s}s of {state.max_total_s}s cap)"
    )
    log.warning(wait_msg.strip())
    _append_agent_log(log_path, wait_msg)
    time.sleep(sleep_s)
    state.waited_s += sleep_s
    state.next_s = min(state.next_s * 2, state.max_s)
    state.retries += 1
    return True, None


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
    - agent auth refresh races (`token_revoked` / `refresh_token_reused`) →
      short exponential backoff and retry. If the auth race never clears,
      return a transient rc=124 outcome so worker-level transient handling
      can avoid misclassifying it as a task/content failure.

    Returns `_ExecOutcome` carrying rc + stdout + stderr; the caller's
    transport shim translates into `RawTransportResult` (parsing structured
    output, computing duration, etc.).
    """
    backoff_s = _quota_backoff_initial_s()
    backoff_max_s = _quota_backoff_max_s()
    max_total_wait_s = _quota_max_total_wait_s()
    waited_s = 0
    quota_retries = 0
    auth_backoff = _BackoffState(
        next_s=_auth_backoff_initial_s(),
        max_s=_auth_backoff_max_s(),
        waited_s=0,
        max_total_s=_auth_max_total_wait_s(),
    )
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
            _append_agent_log(log_path, annotation)
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
        auth_handled, auth_outcome = _handle_agent_auth_retry(
            rc=rc,
            out=out,
            err=err,
            state=auth_backoff,
            log_path=log_path,
        )
        if auth_outcome is not None:
            return auth_outcome
        if auth_handled:
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


def codex_output_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Return a Codex/OpenAI-compatible structured-output schema.

    The Responses API's JSON schema subset requires every object property
    to be listed in `required`, even when the Pydantic model has defaults.
    Pydantic emits those defaulted fields as optional, so normalize before
    handing the schema to `codex exec --output-schema`.
    """
    raw_schema = schema.model_json_schema()
    _normalize_object_required(raw_schema)
    return raw_schema


def _normalize_object_required(value: Any) -> None:
    """Recursively tighten object schemas in-place for Codex JSON mode."""
    if isinstance(value, dict):
        value.pop("default", None)
        properties = value.get("properties")
        if isinstance(properties, dict):
            value["additionalProperties"] = False
            value["required"] = list(properties.keys())
        for child in value.values():
            _normalize_object_required(child)
        return
    if isinstance(value, list):
        for child in value:
            _normalize_object_required(child)


# ---------- wrappers ----------


class JsonOutputAgent:
    """Wraps a `JsonAgentTransport` for non-writes-files roles."""

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
    """Wraps a `JsonAgentTransport` for writes-files roles."""

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
    """Build a `JsonAgentResult` from a single transport result."""
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
    """Build a `JsonAgentResult` after a re-prompt round."""
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
    transport: JsonAgentTransport,
    prompt: str,
    output_schema: type[BaseModel],
    *,
    handle: Any,
    log_path: Path | None,
    timeout: int,
) -> JsonAgentResult:
    """cli_native enforcement with one repair prompt on Pydantic failure."""
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
        return _retry_after_cli_native_validation_error(
            transport,
            prompt,
            raw,
            first_err=e,
            output_schema=output_schema,
            handle=handle,
            log_path=log_path,
            timeout=timeout,
        )
    return _result_from_raw(raw, structured=instance, parse_errors=())


def _retry_after_cli_native_validation_error(
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
    """Re-prompt once when a cli_native structured payload fails Pydantic."""
    first_errors = tuple(_format_validation_errors(first_err))
    reprompt_text = build_reprompt(prompt, first_err, output_schema)
    second = transport.invoke(
        reprompt_text,
        output_schema=output_schema,
        handle=handle,
        log_path=log_path,
        timeout=timeout,
    )
    if second.rc != 0 or second.structured is None:
        return _result_from_two_raw(
            first,
            second,
            structured=None,
            parse_errors=first_errors,
        )
    try:
        instance = output_schema.model_validate(second.structured)
    except ValidationError as second_err:
        second_errors = tuple(_format_validation_errors(second_err))
        return _result_from_two_raw(
            first,
            second,
            structured=None,
            parse_errors=first_errors + second_errors,
        )
    return _result_from_two_raw(first, second, structured=instance, parse_errors=())


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
    """Re-prompt once after the first client-side parse failure."""
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
    """Shared body for both `JsonOutputAgent` and `WritesFilesAgent`."""
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
        return _validate_cli_native(
            raw,
            transport,
            prompt,
            output_schema,
            handle=handle,
            log_path=log_path,
            timeout=timeout,
        )
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
    "codex_output_schema",
]
