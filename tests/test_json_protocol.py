"""Plan 38 PR-A: JsonOutputAgent / WritesFilesAgent wrapper tests with stub transports.

Verifies:
- cli_native happy path returns a validated BaseModel instance
- client_side validation failure triggers exactly one re-prompt
- second client_side failure surfaces parse_errors non-empty
- the re-prompt prompt includes the ValidationError + the schema
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from quikode.agent_schemas import DoerEnvelope, ProgressVerdict
from quikode.agents.json_protocol import (
    JsonOutputAgent,
    RawTransportResult,
    WritesFilesAgent,
    build_reprompt,
)


@dataclass
class StubTransport:
    """In-memory transport stub. Each `invoke` consumes one queued response.

    `responses` is a list of `RawTransportResult`. `invocations` records
    each call's prompt for assertion. `name` and `schema_enforcement`
    mirror the real transport protocol — declared as `str` to match
    the protocol's widened type so structural subtyping accepts the stub.
    """

    name: str = "stub"
    schema_enforcement: str = "cli_native"
    responses: list[RawTransportResult] = field(default_factory=list)
    invocations: list[str] = field(default_factory=list)

    def invoke(
        self,
        prompt: str,
        *,
        output_schema: type[BaseModel] | None,
        handle: Any,
        log_path: Path | None,
        timeout: int,
    ) -> RawTransportResult:
        self.invocations.append(prompt)
        if not self.responses:
            raise AssertionError("StubTransport: no queued response left")
        return self.responses.pop(0)


# ---------- cli_native happy path ----------


def test_cli_native_happy_path_returns_validated_instance() -> None:
    transport = StubTransport(
        name="claude-stub",
        schema_enforcement="cli_native",
        responses=[
            RawTransportResult(
                raw_text=None,
                structured={"verdict": "progressing", "rationale": "rc shifted"},
                rc=0,
                transient=False,
                duration_s=1.5,
                tokens_input=100,
                tokens_output=50,
                cost_usd=0.001,
            )
        ],
    )
    wrapper = JsonOutputAgent(transport=transport, output_schema=ProgressVerdict)
    result = wrapper.invoke("hello", handle=object(), timeout=60)
    assert isinstance(result.structured, ProgressVerdict)
    assert result.structured.verdict == "progressing"
    assert result.parse_errors == ()
    assert result.rc == 0
    assert result.tokens_input == 100
    # Single invocation — no re-prompt on cli_native.
    assert len(transport.invocations) == 1


def test_cli_native_returns_invalid_structured_dict_surfaces_parse_errors() -> None:
    """If the CLI claims cli_native but returns a malformed dict, surface a
    parse_errors entry — do NOT re-prompt (cli_native promised conformance)."""
    transport = StubTransport(
        name="claude-stub",
        schema_enforcement="cli_native",
        responses=[
            RawTransportResult(
                raw_text=None,
                structured={"verdict": "totally-bogus-value", "rationale": "x"},
                rc=0,
                transient=False,
                duration_s=1.0,
            )
        ],
    )
    wrapper = JsonOutputAgent(transport=transport, output_schema=ProgressVerdict)
    result = wrapper.invoke("hello", handle=object(), timeout=60)
    assert result.structured is None
    assert len(result.parse_errors) >= 1
    # No re-prompt issued.
    assert len(transport.invocations) == 1


# ---------- client_side: happy path on first try ----------


def test_client_side_happy_path_returns_validated_instance() -> None:
    payload = json.dumps({"verdict": "flatline", "rationale": "same root cause"})
    transport = StubTransport(
        name="codex-litellm-stub",
        schema_enforcement="client_side",
        responses=[
            RawTransportResult(
                raw_text=payload,
                structured=None,
                rc=0,
                transient=False,
                duration_s=2.0,
                tokens_input=80,
                tokens_output=40,
            )
        ],
    )
    wrapper = JsonOutputAgent(transport=transport, output_schema=ProgressVerdict)
    result = wrapper.invoke("hello", handle=object(), timeout=60)
    assert isinstance(result.structured, ProgressVerdict)
    assert result.structured.verdict == "flatline"
    assert result.parse_errors == ()
    assert len(transport.invocations) == 1


# ---------- client_side: re-prompt-once on validation failure ----------


def test_client_side_validation_failure_triggers_one_reprompt_and_recovers() -> None:
    bad = "not even json"
    good = json.dumps({"verdict": "uncertain", "rationale": "after re-prompt"})
    transport = StubTransport(
        name="codex-litellm-stub",
        schema_enforcement="client_side",
        responses=[
            RawTransportResult(
                raw_text=bad,
                structured=None,
                rc=0,
                transient=False,
                duration_s=1.0,
            ),
            RawTransportResult(
                raw_text=good,
                structured=None,
                rc=0,
                transient=False,
                duration_s=1.0,
            ),
        ],
    )
    wrapper = JsonOutputAgent(transport=transport, output_schema=ProgressVerdict)
    result = wrapper.invoke("original prompt", handle=object(), timeout=60)
    assert isinstance(result.structured, ProgressVerdict)
    assert result.structured.verdict == "uncertain"
    assert result.parse_errors == ()
    # Exactly two invocations: original + one re-prompt.
    assert len(transport.invocations) == 2
    # The re-prompt embeds the schema and the validation error context.
    reprompt_text = transport.invocations[1]
    assert "ProgressVerdict" in reprompt_text or "verdict" in reprompt_text
    assert "schema" in reprompt_text.lower()
    # The original prompt is preserved as preamble.
    assert "original prompt" in reprompt_text


def test_client_side_two_failures_surface_parse_errors() -> None:
    transport = StubTransport(
        name="codex-litellm-stub",
        schema_enforcement="client_side",
        responses=[
            RawTransportResult(
                raw_text="garbage",
                structured=None,
                rc=0,
                transient=False,
                duration_s=1.0,
            ),
            RawTransportResult(
                raw_text="still garbage",
                structured=None,
                rc=0,
                transient=False,
                duration_s=1.0,
            ),
        ],
    )
    wrapper = JsonOutputAgent(transport=transport, output_schema=ProgressVerdict)
    result = wrapper.invoke("hi", handle=object(), timeout=60)
    assert result.structured is None
    assert len(result.parse_errors) >= 1
    # Sum of durations is preserved.
    assert result.duration_s >= 2.0
    # Two invocations (original + reprompt); no third attempt.
    assert len(transport.invocations) == 2


# ---------- transport failure (rc != 0) — no parsing attempted ----------


def test_transport_failure_returns_no_structured_no_reprompt() -> None:
    transport = StubTransport(
        name="codex-litellm-stub",
        schema_enforcement="client_side",
        responses=[
            RawTransportResult(
                raw_text=None,
                structured=None,
                rc=124,
                transient=True,
                duration_s=300.0,
                stderr_excerpt="agent timed out",
            )
        ],
    )
    wrapper = JsonOutputAgent(transport=transport, output_schema=ProgressVerdict)
    result = wrapper.invoke("hi", handle=object(), timeout=60)
    assert result.structured is None
    assert result.rc == 124
    assert result.transient is True
    assert result.parse_errors == ()  # didn't try to parse — transport failed
    assert len(transport.invocations) == 1


# ---------- WritesFilesAgent shares the same wrapper logic ----------


def test_writes_files_agent_validates_envelope_cli_native() -> None:
    transport = StubTransport(
        name="claude-stub",
        schema_enforcement="cli_native",
        responses=[
            RawTransportResult(
                raw_text=None,
                structured={
                    "summary": "did the work",
                    "files_touched": ["a.py"],
                    "witness_commands_run": [],
                    "notes": "",
                },
                rc=0,
                transient=False,
                duration_s=10.0,
            )
        ],
    )
    wrapper = WritesFilesAgent(transport=transport, envelope_schema=DoerEnvelope)
    result = wrapper.invoke("doer prompt", handle=object(), timeout=60)
    assert isinstance(result.structured, DoerEnvelope)
    assert result.structured.summary == "did the work"
    assert result.structured.files_touched == ["a.py"]


def test_writes_files_agent_client_side_reprompt() -> None:
    bad = "{prose response without json}"
    good = json.dumps({"summary": "ok", "files_touched": [], "witness_commands_run": [], "notes": ""})
    transport = StubTransport(
        name="codex-litellm-stub",
        schema_enforcement="client_side",
        responses=[
            RawTransportResult(raw_text=bad, structured=None, rc=0, transient=False, duration_s=1.0),
            RawTransportResult(raw_text=good, structured=None, rc=0, transient=False, duration_s=1.0),
        ],
    )
    wrapper = WritesFilesAgent(transport=transport, envelope_schema=DoerEnvelope)
    result = wrapper.invoke("doer prompt", handle=object(), timeout=60)
    assert isinstance(result.structured, DoerEnvelope)
    assert len(transport.invocations) == 2


# ---------- build_reprompt: standalone unit test ----------


def test_build_reprompt_includes_error_and_schema() -> None:
    try:
        ProgressVerdict.model_validate({"verdict": "totally-bogus"})
    except ValidationError as e:
        text = build_reprompt("ORIGINAL PROMPT", e, ProgressVerdict)
        assert "ORIGINAL PROMPT" in text
        # The schema dump includes field descriptions / type info.
        assert "rationale" in text
        # The validation error excerpt is embedded.
        assert "verdict" in text.lower()
        # Cap on error length applied.
        assert "Respond ONLY with valid JSON" in text
        return
    raise AssertionError("expected ValidationError from invalid verdict")
