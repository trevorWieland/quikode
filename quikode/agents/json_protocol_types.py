"""Shared JSON agent protocol result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel


@dataclass(frozen=True)
class RawTransportResult:
    """One transport invocation result before pydantic validation.

    `category` (plan 59 fix E') propagates the transient failure
    classification from `_run_with_retry` so the worker layer can pick
    the matching `cfg.transient_retry_delays_s[category]` sleep.
    `"none"` (the default) means no transient category applies.
    Accepted values: `none`, `quota_exhausted`, `container_vanished`,
    `auth_refresh`.
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
    category: str = "none"


@dataclass(frozen=True)
class JsonAgentResult:
    """One role-layer invocation result.

    `category` (plan 59 fix E') mirrors `RawTransportResult.category`
    — propagated up so the worker can look up the category-aware
    transient sleep without re-deriving from rc / stderr text.
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
    category: str = "none"


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

    def invoke_raw(
        self,
        prompt: str,
        *,
        handle: Any,
        log_path: Path | None,
        timeout: int,
    ) -> RawTransportResult:
        """Invoke the underlying CLI with no JSON-schema enforcement.

        Plan 47: writes-files roles without a bookkeeping envelope (the
        post-plan-47 doer) run the CLI in apply-patch mode without
        passing `--output-schema` / `--json-schema`. The shim records
        rc, stdout, stderr, duration, transient, and tokens; no
        pydantic validation happens. `structured` is always None;
        `raw_text` carries whatever the CLI emitted on stdout (free
        text — not parsed). The diff in `/workspace` is the actual
        deliverable; this output is informational only.
        """
        ...
