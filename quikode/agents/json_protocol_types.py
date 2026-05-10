"""Shared JSON agent protocol result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel


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
