"""Common typed primitives shared across modules.

Every module that produces or consumes a "well-known shape" (verdicts, agent
results, parsed CLI flags) should put that shape here so the CLI, the worker,
the future TUI, and tests all consume the same types instead of free-form
strings or dicts.

Pydantic models carry `Field(description=...)` so `model_json_schema()` is
self-documenting — that schema feeds the TUI settings modal and any future
external tooling.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Verdict(StrEnum):
    """Outcome of a checker pass on a subtask or whole-spec slice."""

    PASS = "PASS"
    FAIL = "FAIL"


class IntentVerdict(StrEnum):
    """Outcome of an intent-gap review after a dependency merges."""

    NO_DRIFT = "NO_DRIFT"
    MINOR_DRIFT = "MINOR_DRIFT"
    INTENT_CONFLICT = "INTENT_CONFLICT"


class CriterionVerdict(StrEnum):
    """Per-criterion verdict line in a checker output."""

    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class AgentResult(BaseModel):
    """Outcome of one agent invocation."""

    model_config = ConfigDict(frozen=True)

    rc: int = Field(description="Process return code; 0 means success.")
    stdout: str = Field(description="Captured stdout from the agent CLI.")
    stderr: str = Field(description="Captured stderr from the agent CLI.")
    tokens_used: int | None = Field(
        default=None,
        ge=0,
        description="Total tokens (input + output). Best-effort. None if unknown.",
    )
    tokens_input: int | None = Field(
        default=None,
        ge=0,
        description="Input/prompt tokens. Populated when the provider reports it.",
    )
    tokens_output: int | None = Field(
        default=None,
        ge=0,
        description="Output/completion tokens.",
    )
    tokens_cached_read: int | None = Field(
        default=None,
        ge=0,
        description="Tokens read from a prompt cache (cheaper than fresh input).",
    )
    tokens_cached_creation: int | None = Field(
        default=None,
        ge=0,
        description="Tokens used to write a new cache entry.",
    )
    cost_usd: float = Field(
        default=0.0,
        ge=0,
        description="Provider-reported cost in USD for this invocation. Zero if not reported.",
    )
    duration_s: float | None = Field(
        default=None,
        ge=0,
        description="Wall-clock seconds the agent ran. None if not measured.",
    )
    transient: bool = Field(
        default=False,
        description=(
            "True when the failure looks like an infrastructure-level glitch "
            "(timeout, container OOM/SIGKILL, docker-daemon error) rather than "
            "a real agent-produced failure. Worker uses this to free-retry "
            "without burning the real-failure retry budget."
        ),
    )

    @property
    def ok(self) -> bool:
        return self.rc == 0


class IntentReviewOutcome(BaseModel):
    """Parsed intent-reviewer agent output."""

    model_config = ConfigDict(frozen=True)

    verdict: IntentVerdict = Field(description="One of NO_DRIFT, MINOR_DRIFT, INTENT_CONFLICT.")
    affected_areas: str = Field(
        default="",
        description="Comma-separated paths/areas the reviewer flagged. Free text — no schema.",
    )
    explanation: str = Field(
        default="",
        description="One-sentence rationale, surfaced to the worker for downstream prompts.",
    )


class CheckerOutcome(BaseModel):
    """Parsed checker agent output."""

    model_config = ConfigDict(frozen=True)

    verdict: Verdict = Field(description="PASS or FAIL.")
    raw: str = Field(default="", description="Full agent output, captured verbatim.")
    ci_result: str | None = Field(
        default=None,
        description="`pass` | `fail` | None when not run (subtask checker skips CI).",
    )
    ci_failure_excerpt: str | None = Field(
        default=None,
        description="When ci_result == 'fail', a short excerpt of the failure.",
    )
