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

from quikode.agent_schemas import ConflictResolverEnvelope, ProgressVerdict
from quikode.agents.json_fallback import QuotaFallbackJsonAgent
from quikode.agents.json_protocol import (
    JsonOutputAgent,
    RawTransportResult,
    WritesFilesAgent,
    _run_with_retry,
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

    def invoke_raw(
        self,
        prompt: str,
        *,
        handle: Any,
        log_path: Path | None,
        timeout: int,
    ) -> RawTransportResult:
        """Plan 47: StubTransport satisfies the no-schema contract too."""
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


def test_cli_native_returns_invalid_structured_dict_gets_one_reprompt() -> None:
    """If the CLI claims cli_native but returns a malformed dict, issue one
    schema repair prompt. Codex can return a sibling-role-shaped object even
    when `--output-schema` is present."""
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
            ),
            RawTransportResult(
                raw_text=None,
                structured={"verdict": "uncertain", "rationale": "after repair"},
                rc=0,
                transient=False,
                duration_s=1.0,
            ),
        ],
    )
    wrapper = JsonOutputAgent(transport=transport, output_schema=ProgressVerdict)
    result = wrapper.invoke("hello", handle=object(), timeout=60)
    assert isinstance(result.structured, ProgressVerdict)
    assert result.structured.verdict == "uncertain"
    assert result.parse_errors == ()
    assert len(transport.invocations) == 2
    assert "Your previous response failed schema validation" in transport.invocations[1]


def test_cli_native_second_invalid_structured_dict_surfaces_parse_errors() -> None:
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
            ),
            RawTransportResult(
                raw_text=None,
                structured={"verdict": "still-bogus", "rationale": "x"},
                rc=0,
                transient=False,
                duration_s=1.0,
            ),
        ],
    )
    wrapper = JsonOutputAgent(transport=transport, output_schema=ProgressVerdict)
    result = wrapper.invoke("hello", handle=object(), timeout=60)
    assert result.structured is None
    assert len(result.parse_errors) >= 2
    assert len(transport.invocations) == 2


def test_cli_native_missing_payload_gets_one_reprompt_and_recovers() -> None:
    transport = StubTransport(
        name="codex-direct-stub",
        schema_enforcement="cli_native",
        responses=[
            RawTransportResult(
                raw_text=None,
                structured=None,
                rc=0,
                transient=False,
                duration_s=1.0,
                stderr_excerpt="[quikode] codex output was empty",
            ),
            RawTransportResult(
                raw_text=None,
                structured={"verdict": "uncertain", "rationale": "after repair"},
                rc=0,
                transient=False,
                duration_s=1.0,
            ),
        ],
    )
    wrapper = JsonOutputAgent(transport=transport, output_schema=ProgressVerdict)
    result = wrapper.invoke("hello", handle=object(), timeout=60)
    assert isinstance(result.structured, ProgressVerdict)
    assert result.structured.verdict == "uncertain"
    assert result.parse_errors == ()
    assert len(transport.invocations) == 2
    assert "did not produce the required structured JSON payload" in transport.invocations[1]


def test_cli_native_second_missing_payload_surfaces_parse_errors() -> None:
    transport = StubTransport(
        name="codex-direct-stub",
        schema_enforcement="cli_native",
        responses=[
            RawTransportResult(
                raw_text=None,
                structured=None,
                rc=0,
                transient=False,
                duration_s=1.0,
            ),
            RawTransportResult(
                raw_text=None,
                structured=None,
                rc=0,
                transient=False,
                duration_s=1.0,
            ),
        ],
    )
    wrapper = JsonOutputAgent(transport=transport, output_schema=ProgressVerdict)
    result = wrapper.invoke("hello", handle=object(), timeout=60)
    assert result.structured is None
    assert len(result.parse_errors) == 2
    assert "CLI envelope was missing" in result.parse_errors[0]
    assert len(transport.invocations) == 2


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


def test_client_side_accepts_trailing_json_envelope_in_noisy_output() -> None:
    payload = json.dumps({"verdict": "progressing", "rationale": "trailing object"})
    noisy = f"I ran the checks and here is the envelope:\n```json\n{payload}\n```"
    transport = StubTransport(
        name="codex-litellm-stub",
        schema_enforcement="client_side",
        responses=[
            RawTransportResult(
                raw_text=noisy,
                structured=None,
                rc=0,
                transient=False,
                duration_s=2.0,
            )
        ],
    )
    wrapper = JsonOutputAgent(transport=transport, output_schema=ProgressVerdict)
    result = wrapper.invoke("hello", handle=object(), timeout=60)
    assert isinstance(result.structured, ProgressVerdict)
    assert result.structured.verdict == "progressing"
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


def test_client_side_reprompt_accepts_noisy_repair_output() -> None:
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
                raw_text=f"Done.\n{good}",
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
    assert len(transport.invocations) == 2


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


def test_quota_fallback_invokes_next_transport(tmp_path) -> None:
    primary = StubTransport(
        name="glm-zai",
        schema_enforcement="client_side",
        responses=[
            RawTransportResult(
                raw_text=None,
                structured=None,
                rc=1,
                transient=False,
                duration_s=1.0,
                stderr_excerpt="HTTP 429: rate limit exceeded",
            )
        ],
    )
    fallback = StubTransport(
        name="glm-wafer",
        schema_enforcement="client_side",
        responses=[
            RawTransportResult(
                raw_text='{"verdict":"progressing","rationale":"fallback worked"}',
                structured=None,
                rc=0,
                transient=False,
                duration_s=2.0,
            )
        ],
    )
    transport = QuotaFallbackJsonAgent(primary=primary, fallbacks=(fallback,))
    wrapper = JsonOutputAgent(transport=transport, output_schema=ProgressVerdict)

    result = wrapper.invoke("hi", handle=object(), log_path=tmp_path / "agent.log", timeout=60)

    assert isinstance(result.structured, ProgressVerdict)
    assert result.structured.verdict == "progressing"
    assert result.duration_s == 3.0
    assert len(primary.invocations) == 1
    assert len(fallback.invocations) == 1
    assert "quota fallback: glm-zai -> glm-wafer" in (tmp_path / "agent.log").read_text()


def test_quota_fallback_can_end_on_cli_native_codex(tmp_path) -> None:
    primary = StubTransport(
        name="glm-zai",
        schema_enforcement="client_side",
        responses=[
            RawTransportResult(
                raw_text=None,
                structured=None,
                rc=1,
                transient=False,
                duration_s=1.0,
                stderr_excerpt="HTTP 429: z.ai exhausted",
            )
        ],
    )
    wafer = StubTransport(
        name="glm-wafer",
        schema_enforcement="client_side",
        responses=[
            RawTransportResult(
                raw_text=None,
                structured=None,
                rc=1,
                transient=False,
                duration_s=2.0,
                stderr_excerpt="HTTP 429: wafer exhausted",
            )
        ],
    )
    codex = StubTransport(
        name="codex_direct",
        schema_enforcement="cli_native",
        responses=[
            RawTransportResult(
                raw_text=None,
                structured={"verdict": "progressing", "rationale": "codex last resort"},
                rc=0,
                transient=False,
                duration_s=3.0,
            )
        ],
    )
    transport = QuotaFallbackJsonAgent(primary=primary, fallbacks=(wafer, codex))
    wrapper = JsonOutputAgent(transport=transport, output_schema=ProgressVerdict)

    result = wrapper.invoke("hi", handle=object(), log_path=tmp_path / "agent.log", timeout=60)

    assert isinstance(result.structured, ProgressVerdict)
    assert result.structured.rationale == "codex last resort"
    assert result.duration_s == 6.0
    assert len(primary.invocations) == 1
    assert len(wafer.invocations) == 1
    assert len(codex.invocations) == 1
    log_text = (tmp_path / "agent.log").read_text()
    assert "quota fallback: glm-zai -> glm-wafer" in log_text
    assert "quota fallback: glm-wafer -> codex_direct" in log_text


def test_run_with_retry_can_surface_quota_immediately(monkeypatch) -> None:
    """Plan 59 fix E': quota detection fast-fails inside `_run_with_retry`
    with no in-transport sleep. The outcome carries the original rc +
    stderr plus `category="quota_exhausted"` so the worker layer picks
    the category-aware sleep."""
    calls = {"n": 0}

    def fake_exec_in(*args: Any, **kwargs: Any) -> tuple[int, str, str]:
        calls["n"] += 1
        return 1, "", "HTTP 429: rate limit exceeded"

    monkeypatch.setattr("quikode.agents.json_protocol.exec_in", fake_exec_in)

    out = _run_with_retry(
        object(),
        ["codex"],
        stdin="prompt",
        log_path=None,
        timeout=60,
    )

    assert calls["n"] == 1
    assert out.rc == 1
    assert out.timed_out is False
    assert out.category == "quota_exhausted"
    assert "fast-fail" in out.stderr


def test_run_with_retry_retries_codex_auth_refresh_race(monkeypatch, tmp_path) -> None:
    calls = {"n": 0}

    def fake_exec_in(*args: Any, **kwargs: Any) -> tuple[int, str, str]:
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                99,
                "",
                "ERROR: Your access token could not be refreshed because your refresh token was already used. "
                '{"code":"refresh_token_reused"}',
            )
        return 0, '{"ok": true}', ""

    monkeypatch.setenv("QUIKODE_AUTH_BACKOFF_INITIAL_S", "0")
    monkeypatch.setattr("quikode.agents.json_protocol.exec_in", fake_exec_in)
    monkeypatch.setattr("quikode.agents.json_protocol.time.sleep", lambda _seconds: None)

    out = _run_with_retry(object(), ["codex"], stdin="prompt", log_path=tmp_path / "agent.log", timeout=60)

    assert calls["n"] == 2
    assert out.rc == 0
    assert out.stdout == '{"ok": true}'
    assert "agent auth refresh failure" in (tmp_path / "agent.log").read_text()


def test_run_with_retry_exhausted_codex_auth_refresh_is_transient(monkeypatch) -> None:
    def fake_exec_in(*args: Any, **kwargs: Any) -> tuple[int, str, str]:
        return 99, "", '{"code":"token_revoked","message":"Encountered invalidated oauth token"}'

    monkeypatch.setenv("QUIKODE_AUTH_BACKOFF_INITIAL_S", "0")
    monkeypatch.setenv("QUIKODE_AUTH_MAX_TOTAL_WAIT_S", "0")
    monkeypatch.setattr("quikode.agents.json_protocol.exec_in", fake_exec_in)

    out = _run_with_retry(object(), ["codex"], stdin="prompt", log_path=None, timeout=60)

    assert out.rc == 124
    assert out.timed_out is True
    assert "agent auth refresh retry exceeded" in out.stderr


# ---------- WritesFilesAgent shares the same wrapper logic ----------


def test_writes_files_agent_validates_envelope_cli_native() -> None:
    transport = StubTransport(
        name="claude-stub",
        schema_enforcement="cli_native",
        responses=[
            RawTransportResult(
                raw_text=None,
                structured={
                    "summary": "resolved markers",
                    "files_touched": ["a.py"],
                    "gave_up": False,
                    "give_up_reason": "",
                    "notes": "",
                },
                rc=0,
                transient=False,
                duration_s=10.0,
            )
        ],
    )
    wrapper = WritesFilesAgent(transport=transport, envelope_schema=ConflictResolverEnvelope)
    result = wrapper.invoke("doer prompt", handle=object(), timeout=60)
    assert isinstance(result.structured, ConflictResolverEnvelope)
    assert result.structured.summary == "resolved markers"
    assert result.structured.files_touched == ["a.py"]


def test_writes_files_agent_client_side_reprompt() -> None:
    bad = "{prose response without json}"
    good = json.dumps(
        {
            "summary": "ok",
            "files_touched": [],
            "gave_up": False,
            "give_up_reason": "",
            "notes": "",
        }
    )
    transport = StubTransport(
        name="codex-litellm-stub",
        schema_enforcement="client_side",
        responses=[
            RawTransportResult(raw_text=bad, structured=None, rc=0, transient=False, duration_s=1.0),
            RawTransportResult(raw_text=good, structured=None, rc=0, transient=False, duration_s=1.0),
        ],
    )
    wrapper = WritesFilesAgent(transport=transport, envelope_schema=ConflictResolverEnvelope)
    result = wrapper.invoke("doer prompt", handle=object(), timeout=60)
    assert isinstance(result.structured, ConflictResolverEnvelope)
    assert len(transport.invocations) == 2


def test_writes_files_agent_no_schema_runs_invoke_raw() -> None:
    """Plan 47: when envelope_schema is None (the doer), the wrapper
    invokes `transport.invoke_raw` and returns a `JsonAgentResult`
    with `structured=None`, `parse_errors=()`, and `raw_text` from the
    CLI's stdout. No re-prompt loop, no schema validation."""

    @dataclass
    class RawCapturingStub:
        name: str = "raw-stub"
        schema_enforcement: str = "client_side"
        invocations: list[str] = field(default_factory=list)
        raw_invocations: list[str] = field(default_factory=list)

        def invoke(
            self,
            prompt: str,
            *,
            output_schema: type[BaseModel] | None,
            handle: Any,
            log_path: Path | None,
            timeout: int,
        ) -> RawTransportResult:
            raise AssertionError("no-schema path must not call .invoke()")

        def invoke_raw(
            self,
            prompt: str,
            *,
            handle: Any,
            log_path: Path | None,
            timeout: int,
        ) -> RawTransportResult:
            self.raw_invocations.append(prompt)
            return RawTransportResult(
                raw_text="apply_patch ok\nsummary line",
                structured=None,
                rc=0,
                transient=False,
                duration_s=12.5,
                tokens_input=100,
                tokens_output=50,
                cost_usd=0.01,
                stderr_excerpt="",
            )

    transport = RawCapturingStub()
    wrapper = WritesFilesAgent(transport=transport, envelope_schema=None)
    result = wrapper.invoke("doer prompt", handle=object(), timeout=60)
    assert result.structured is None
    assert result.parse_errors == ()
    assert result.raw_text == "apply_patch ok\nsummary line"
    assert result.rc == 0
    assert result.duration_s == 12.5
    assert result.tokens_input == 100
    assert result.tokens_output == 50
    assert result.cost_usd == 0.01
    assert transport.raw_invocations == ["doer prompt"]


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
