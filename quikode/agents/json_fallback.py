"""Quota fallback wrapper for JSON agent transports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .json_protocol import JsonAgentTransport, RawTransportResult
from .transient_quota import _is_provider_unavailable, _is_quota_exhausted


def _should_walk_chain(raw: RawTransportResult) -> bool:
    """Plan 60 fix 2: chain-walk on EITHER subscription quota exhaustion
    OR provider-side unavailability (invalid key / 401 / 403 / session
    expired). The 2026-05-11 overnight Claude outage emitted auth-shaped
    rc=1 stderr that didn't match the quota regexes, so the chain
    walker never fired and 145 calls fast-failed across 13 tasks. The
    operator's stated intent is "any provider unavailability triggers
    the chain walk," so quota + provider-unavailable share the same
    cascade path here.
    """
    blob_out = raw.raw_text or ""
    blob_err = raw.stderr_excerpt or ""
    return _is_quota_exhausted(raw.rc, blob_out, blob_err) or _is_provider_unavailable(
        raw.rc, blob_out, blob_err
    )


class QuotaFallbackJsonAgent:
    """Try fallback transports when the primary reports quota exhaustion
    or provider-side unavailability.

    This is intentionally narrow: parse/schema failures still belong to the
    normal JSON wrapper. Provider quota AND provider-unavailable signatures
    (invalid keys, 401/403, session expired) both move the call to another
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
        self.primary = primary
        self.fallbacks = fallbacks
        self.schema_enforcement = primary.schema_enforcement

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
            raw = _normalize_for_primary_schema(raw, primary_schema_enforcement=self.schema_enforcement)
            combined = _combine(prior, raw) if prior is not None else raw
            if not _should_walk_chain(raw):
                return combined
            if index == len(transports) - 1:
                return combined
            _append_fallback_note(log_path, transport, transports[index + 1])
            prior = combined
        if prior is None:  # pragma: no cover - transports is always non-empty
            raise RuntimeError("quota fallback invoked without transports")
        return prior

    def invoke_raw(
        self,
        prompt: str,
        *,
        handle: Any,
        log_path: Path | None,
        timeout: int,
    ) -> RawTransportResult:
        """Plan 47: walk the same primary→fallback chain in no-schema mode.

        No schema-tier normalization is needed — every shim's
        `invoke_raw` already returns `structured=None` with stdout
        carried in `raw_text`.
        """
        transports = (self.primary, *self.fallbacks)
        prior: RawTransportResult | None = None
        for index, transport in enumerate(transports):
            raw = transport.invoke_raw(
                prompt,
                handle=handle,
                log_path=log_path,
                timeout=timeout,
            )
            combined = _combine(prior, raw) if prior is not None else raw
            if not _should_walk_chain(raw):
                return combined
            if index == len(transports) - 1:
                return combined
            _append_fallback_note(log_path, transport, transports[index + 1])
            prior = combined
        if prior is None:  # pragma: no cover - transports is always non-empty
            raise RuntimeError("quota fallback invoked without transports")
        return prior


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
        tokens_input=_sum_int_opt(first.tokens_input, second.tokens_input),
        tokens_output=_sum_int_opt(first.tokens_output, second.tokens_output),
        cost_usd=_sum_float_opt(first.cost_usd, second.cost_usd),
        stderr_excerpt=stderr[-2000:],
        # Plan 59 fix E': preserve category from the most recent (and
        # last-walked) transport. After the chain returns the worker
        # checks `category == "quota_exhausted"` to pick the longer
        # category-aware sleep.
        category=second.category,
    )


def _sum_int_opt(left: int | None, right: int | None) -> int | None:
    if left is None:
        return right
    if right is None:
        return left
    return left + right


def _sum_float_opt(left: float | None, right: float | None) -> float | None:
    if left is None:
        return right
    if right is None:
        return left
    return left + right


def _normalize_for_primary_schema(
    raw: RawTransportResult,
    *,
    primary_schema_enforcement: str,
) -> RawTransportResult:
    """Adapt a fallback transport result to the wrapper's exposed schema tier.

    A direct Codex fallback returns `structured` under cli-native enforcement,
    but a LiteLLM primary exposes client-side enforcement to the outer wrapper.
    Convert that structured object back into JSON text so the existing
    client-side pydantic path can validate it uniformly.
    """
    if primary_schema_enforcement != "client_side":
        return raw
    if raw.raw_text is not None or raw.structured is None:
        return raw
    return RawTransportResult(
        raw_text=json.dumps(raw.structured),
        structured=None,
        rc=raw.rc,
        transient=raw.transient,
        duration_s=raw.duration_s,
        tokens_input=raw.tokens_input,
        tokens_output=raw.tokens_output,
        cost_usd=raw.cost_usd,
        stderr_excerpt=raw.stderr_excerpt,
        category=raw.category,
    )


__all__ = ["QuotaFallbackJsonAgent"]
