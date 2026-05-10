"""Schema validation and repair prompts for JSON-mode agents."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from .json_protocol_types import JsonAgentResult, JsonAgentTransport, RawTransportResult

_REPROMPT_TEMPLATE = """Your previous response failed schema validation:

{error}

Respond ONLY with valid JSON matching this exact schema:

{schema}

Do not include any prose, markdown fences, or explanations. The output \
must be parseable by `model_validate_json` directly."""

_MISSING_PAYLOAD_REPROMPT_TEMPLATE = """Your previous response did not produce the required structured JSON payload.

Reason:
{error}

Respond ONLY with valid JSON matching this exact schema:

{schema}

Do not include any prose, markdown fences, or explanations. The output \
must be parseable by `model_validate_json` directly."""


def build_reprompt(prompt: str, error: ValidationError | str, schema: type[BaseModel]) -> str:
    """Build the structured re-prompt for schema validation failure."""
    err_str = (error if isinstance(error, str) else repr(error))[:2000]
    schema_str = json.dumps(schema.model_json_schema(), indent=2)
    feedback = _REPROMPT_TEMPLATE.format(error=err_str, schema=schema_str)
    return f"{prompt}\n\n---\n\n{feedback}"


def build_missing_payload_reprompt(prompt: str, error: str, schema: type[BaseModel]) -> str:
    """Build a structured re-prompt when cli-native output produced no JSON."""
    schema_str = json.dumps(schema.model_json_schema(), indent=2)
    feedback = _MISSING_PAYLOAD_REPROMPT_TEMPLATE.format(error=error[:2000], schema=schema_str)
    return f"{prompt}\n\n---\n\n{feedback}"


def codex_output_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Return a Codex/OpenAI-compatible structured-output schema."""
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


def invoke_with_validation(
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
    if raw.rc != 0:
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
        return _retry_after_cli_native_missing_payload(
            transport,
            prompt,
            raw,
            output_schema=output_schema,
            handle=handle,
            log_path=log_path,
            timeout=timeout,
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


def _missing_payload_error(raw: RawTransportResult) -> str:
    detail = (
        "cli_native transport returned structured=None; CLI envelope was missing the schema-validated payload"
    )
    if raw.raw_text:
        detail += f"; raw output excerpt: {raw.raw_text[:1000]}"
    if raw.stderr_excerpt:
        detail += f"; stderr excerpt: {raw.stderr_excerpt[:1000]}"
    return detail


def _retry_after_cli_native_missing_payload(
    transport: JsonAgentTransport,
    prompt: str,
    first: RawTransportResult,
    *,
    output_schema: type[BaseModel],
    handle: Any,
    log_path: Path | None,
    timeout: int,
) -> JsonAgentResult:
    """Re-prompt once when cli_native exits cleanly with no structured output."""
    first_error = _missing_payload_error(first)
    reprompt_text = build_missing_payload_reprompt(prompt, first_error, output_schema)
    second = transport.invoke(
        reprompt_text,
        output_schema=output_schema,
        handle=handle,
        log_path=log_path,
        timeout=timeout,
    )
    if second.rc != 0 or second.structured is None:
        errors = (first_error,)
        if second.rc == 0 and second.structured is None:
            errors += (_missing_payload_error(second),)
        return _result_from_two_raw(first, second, structured=None, parse_errors=errors)
    try:
        instance = output_schema.model_validate(second.structured)
    except ValidationError as second_err:
        return _result_from_two_raw(
            first,
            second,
            structured=None,
            parse_errors=(first_error, *tuple(_format_validation_errors(second_err))),
        )
    return _result_from_two_raw(first, second, structured=instance, parse_errors=())


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
        return _result_from_two_raw(first, second, structured=None, parse_errors=first_errors)
    try:
        instance = output_schema.model_validate(second.structured)
    except ValidationError as second_err:
        return _result_from_two_raw(
            first,
            second,
            structured=None,
            parse_errors=first_errors + tuple(_format_validation_errors(second_err)),
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
    """client_side enforcement: pydantic plus structured re-prompt-once."""
    instance, first_errors = _validate_client_side_payload(raw.raw_text or "", output_schema)
    if instance is None:
        return _retry_after_validation_error(
            transport,
            prompt,
            raw,
            first_errors=first_errors,
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
    first_errors: tuple[str, ...],
    output_schema: type[BaseModel],
    handle: Any,
    log_path: Path | None,
    timeout: int,
) -> JsonAgentResult:
    """Re-prompt once after the first client-side parse failure."""
    reprompt_text = build_reprompt(prompt, "\n".join(first_errors), output_schema)
    second = transport.invoke(
        reprompt_text,
        output_schema=output_schema,
        handle=handle,
        log_path=log_path,
        timeout=timeout,
    )
    if second.rc != 0 or second.raw_text is None:
        return _result_from_two_raw(first, second, structured=None, parse_errors=first_errors)
    instance, second_errors = _validate_client_side_payload(second.raw_text, output_schema)
    if instance is None:
        return _result_from_two_raw(
            first,
            second,
            structured=None,
            parse_errors=first_errors + second_errors,
        )
    return _result_from_two_raw(first, second, structured=instance, parse_errors=())


def _validate_client_side_payload(
    text: str,
    output_schema: type[BaseModel],
) -> tuple[BaseModel | None, tuple[str, ...]]:
    """Validate client-side JSON, tolerating provider prose around the object."""
    try:
        return output_schema.model_validate_json(text), ()
    except ValidationError as direct_err:
        direct_errors = tuple(_format_validation_errors(direct_err))

    for candidate in _json_object_candidates(text):
        try:
            return output_schema.model_validate_json(candidate), ()
        except ValidationError:
            continue
    return None, direct_errors


def _json_object_candidates(text: str) -> tuple[str, ...]:
    """Return balanced JSON-object substrings from newest to oldest."""
    candidates: list[str] = []
    start: int | None = None
    depth = 0
    in_string = False
    escaped = False

    for idx, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(text[start : idx + 1])
                start = None

    return tuple(reversed(candidates[-20:]))


def _format_validation_errors(e: ValidationError) -> list[str]:
    """Flatten a `ValidationError` into one string per error line."""
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
    "build_missing_payload_reprompt",
    "build_reprompt",
    "codex_output_schema",
    "invoke_with_validation",
]
