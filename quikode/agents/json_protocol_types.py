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
