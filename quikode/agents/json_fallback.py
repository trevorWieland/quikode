"""Quota fallback wrapper for JSON agent transports."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .json_protocol import JsonAgentTransport, RawTransportResult
from .transient_quota import _is_quota_exhausted


class QuotaFallbackJsonAgent:
    """Try fallback transports when the primary reports quota exhaustion.

    This is intentionally narrow: parse/schema failures still belong to the
    normal JSON wrapper, while provider quota failures can move to another
    configured model without burning subtask retry budget.
    """

    name = "quota_fallback"

    def __init__(
        self,
        *,
        primary: JsonAgentTransport,
        fallbacks: tuple[JsonAgentTransport, ...],
    ):
        if not fallbacks:
            raise ValueError("QuotaFallbackJsonAgent requires at least one fallback")
        schema_enforcement = primary.schema_enforcement
        for fallback in fallbacks:
            if fallback.schema_enforcement != schema_enforcement:
                raise ValueError("quota fallback transports must share schema_enforcement")
        self.primary = primary
        self.fallbacks = fallbacks
        self.schema_enforcement = schema_enforcement

    def invoke(
        self,
        prompt: str,
        *,
        output_schema: type[BaseModel] | None,
        handle: Any,
        log_path: Path | None,
        timeout: int,
    ) -> RawTransportResult:
        transports = (self.primary, *self.fallbacks)
        prior: RawTransportResult | None = None
        for index, transport in enumerate(transports):
            raw = transport.invoke(
                prompt,
                output_schema=output_schema,
                handle=handle,
                log_path=log_path,
                timeout=timeout,
            )
            combined = _combine(prior, raw) if prior is not None else raw
            if not _is_quota_exhausted(raw.rc, raw.raw_text or "", raw.stderr_excerpt):
                return combined
            if index == len(transports) - 1:
                return combined
            _append_fallback_note(log_path, transport, transports[index + 1])
            prior = combined
        return prior  # pragma: no cover - loop always returns with non-empty transports


def _append_fallback_note(
    log_path: Path | None,
    from_transport: JsonAgentTransport,
    to_transport: JsonAgentTransport,
) -> None:
    if log_path is None:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    from_label = _transport_label(from_transport)
    to_label = _transport_label(to_transport)
    with log_path.open("a") as f:
        f.write(f"\n[quikode] quota fallback: {from_label} -> {to_label}\n")


def _transport_label(transport: JsonAgentTransport) -> str:
    profile = getattr(transport, "profile", None)
    if profile:
        return f"{transport.name}/{profile}"
    return transport.name


def _combine(first: RawTransportResult | None, second: RawTransportResult) -> RawTransportResult:
    if first is None:
        return second
    stderr = "\n".join(part for part in (first.stderr_excerpt, second.stderr_excerpt) if part)
    return RawTransportResult(
        raw_text=second.raw_text,
        structured=second.structured,
        rc=second.rc,
        transient=second.transient,
        duration_s=first.duration_s + second.duration_s,
        tokens_input=_sum_opt(first.tokens_input, second.tokens_input),
        tokens_output=_sum_opt(first.tokens_output, second.tokens_output),
        cost_usd=_sum_opt(first.cost_usd, second.cost_usd),
        stderr_excerpt=stderr[-2000:],
    )


def _sum_opt(left: int | float | None, right: int | float | None) -> int | float | None:
    if left is None:
        return right
    if right is None:
        return left
    return left + right


__all__ = ["QuotaFallbackJsonAgent"]
