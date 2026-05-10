"""Plan 38 PR-B.5: end-to-end smoke for the per-subtask doer→diff→checker→
triage pipeline on the JsonAgent layer.

The doer / checker / triage agents are stubbed via `make_agent`; the
witness runner and git helpers are mocked too. The tests verify wiring
(envelope flows in, diff is captured, witnesses run, malformed doer
bookkeeping does not replace diff grading) rather than agent-CLI behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from quikode.agent_schemas import (
    DoerEnvelope,
    SubtaskCheckerFinding,
    SubtaskCheckerOutput,
    SubtaskTriageOutput,
)
from quikode.config import Config
from quikode.dag import Node
from quikode.evaluation_contract import build_for
from quikode.subtask_schema import RubricTarget, Subtask
from quikode.types import Verdict
from quikode.worker import TaskWorker
from quikode.workers.subtask_execution import _DoerCallResult


@dataclass
class _StubAgentResult:
    structured: Any
    rc: int = 0
    transient: bool = False
    duration_s: float = 1.0
    tokens_input: int | None = None
    tokens_output: int | None = None
    cost_usd: float | None = None
    parse_errors: tuple[str, ...] = ()
    raw_text: str | None = None
    stderr_excerpt: str = ""


class _StubAgent:
    """Stand-in for `JsonOutputAgent` / `WritesFilesAgent` returned by
    `make_agent`. Captures the prompt for inspection and returns a
    pre-canned `_StubAgentResult`."""

    def __init__(self, result: _StubAgentResult):
        self.result = result
        self.last_prompt: str | None = None
        self.last_kwargs: dict[str, Any] = {}

    def invoke(self, prompt: str, **kwargs: Any) -> _StubAgentResult:
        self.last_prompt = prompt
        self.last_kwargs = kwargs
        return self.result


_R0050_NODE = Node(
    id="R-0050",
    kind="behavior",
    milestone="M-1",
    title="Project archival",
    scope="Users can mark projects as archived; archived projects are excluded from default list views.",
    depends_on=(),
    completes_behaviors=("B-0061",),
    supports_behaviors=(),
    boundary_with_neighbors="",
    expected_evidence=(
        {
            "behavior_id": "B-0061",
            "kind": "test",
            "interfaces": ["web"],
            "witnesses": ["positive"],
            "command": "npm run test:e2e -- list-excludes-archived",
            "description": "archive a project, list view excludes it",
        },
    ),
    playbook=(),
    rationale="",
    risks=(),
    raw={},
)


_S04_WEB_SUBTASK = Subtask(
    id="S-04-web",
    title="Web list view filter",
    depends_on=(),
    files_to_touch=("apps/web/src/projects/list.tsx",),
    boundary="Web surface only.",
    acceptance=("list view excludes archived",),
    notes="",
    rubric_targets=(
        RubricTarget(category="security", predicted_score=8),
        RubricTarget(category="code_quality", predicted_score=8),
    ),
    standards_referenced=(),
    behavior_evidence_advanced=("B-0061-test-positive",),
)


def _cfg(tmp_path) -> Config:
    return Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        pre_pr_rubric_categories=["security", "code_quality"],
        pre_pr_rubric_min_score=7,
        subtask_witness_timeout_seconds=15,
        # Disable the objective subtask check — these tests exercise the
        # LLM checker path, not the local-CI gate.
        subtask_check_command="",
    )


def _build_worker(cfg: Config) -> Any:
    w = TaskWorker.__new__(TaskWorker)
    w.cfg = cfg
    w.node = _R0050_NODE
    w.handle = MagicMock()
    w.handle.container_name = "qk-stub"
    w.log_path = cfg.repo_path / "task.log"
    w.store = MagicMock()
    w.store.latest_subtask_doer_output.return_value = None
    w.last_doer_summary = ""
    w.plan = None
    w._contract = build_for(_R0050_NODE, cfg)
    w._last_doer_envelope = None
    w._last_doer_parse_errors = ()
    w._last_diff_text = ""
    w._last_witness_results = {}
    return w


# ---------- doer happy path ----------


def test_doer_envelope_persisted_as_subtask_doer_artifact(tmp_path) -> None:
    """Plan 38 PR-B.5: the doer artifact is the DoerEnvelope JSON
    (replaces the SELF_AUDIT prose). Plan 22 carry-forward reads this
    artifact on the next attempt and re-parses via pydantic."""
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    envelope = DoerEnvelope(
        summary="implemented filter",
        files_touched=["apps/web/src/projects/list.tsx"],
        witness_commands_run=["npm run test:e2e -- list-excludes-archived"],
        notes="",
    )
    stub = _StubAgent(_StubAgentResult(structured=envelope, rc=0))
    with (
        patch("quikode.workers.subtask_execution.make_agent", return_value=stub),
    ):
        result = w._run_doer_agent(_S04_WEB_SUBTASK, "doer prompt", attempt=1)
    assert isinstance(result, _DoerCallResult)
    assert result.envelope == envelope
    assert result.parse_errors == ()
    # The artifact body should be the envelope JSON (round-trippable).
    artifact_calls = w.store.add_artifact.call_args_list
    kinds = [c[0][1] for c in artifact_calls]
    assert "subtask_doer:S-04-web" in kinds
    body = next(c for c in artifact_calls if c[0][1] == "subtask_doer:S-04-web")[0][2]
    re_parsed = DoerEnvelope.model_validate_json(body)
    assert re_parsed == envelope


def test_clean_doer_envelope_runs_witnesses(tmp_path) -> None:
    """When the doer envelope validates, `_cache_doer_state` runs the
    scoped witness runner with the subtask's evidence ids."""
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    envelope = DoerEnvelope(summary="ok", files_touched=["x"], witness_commands_run=["just tests"])
    captured: dict[str, Any] = {}

    def fake_run_scoped(**kwargs):
        captured.update(kwargs)
        return {
            "B-0061-test-positive": {
                "rc": 0,
                "stdout_excerpt": "PASS",
                "stderr_excerpt": "",
                "runtime_ms": 100,
                "classification": "OK",
                "note": "ok",
            }
        }

    with (
        patch("quikode.workers.subtask_execution.run_scoped_witnesses", side_effect=fake_run_scoped),
        patch.object(w, "_compute_subtask_diff_excerpt", return_value="diff text"),
    ):
        w._cache_doer_state(
            _S04_WEB_SUBTASK, _DoerCallResult(envelope=envelope, raw_text="", parse_errors=())
        )
    assert captured["evidence_ids"] == ["B-0061-test-positive"]
    assert captured["per_witness_timeout_s"] == cfg.subtask_witness_timeout_seconds
    assert captured["fallback_commands"] == ["just tests"]
    assert w._last_diff_text == "diff text"
    assert w._last_witness_results["B-0061-test-positive"]["classification"] == "OK"


# ---------- parse failure path ----------


def test_doer_parse_failure_continues_to_diff_checker(tmp_path) -> None:
    """Malformed doer bookkeeping is telemetry; the diff still gets checked."""
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    with (
        patch("quikode.workers.subtask_execution.run_scoped_witnesses", return_value={}),
        patch.object(w, "_compute_subtask_diff_excerpt", return_value="diff --git a/x b/x"),
    ):
        w._cache_doer_state(
            _S04_WEB_SUBTASK,
            _DoerCallResult(envelope=None, raw_text="bad json", parse_errors=("summary: field required",)),
        )
    checker_out = SubtaskCheckerOutput(
        verdict="pass",
        findings=[SubtaskCheckerFinding(category="security", verdict="pass", rationale="diff is adequate")],
        overall_assessment="diff passed",
    )
    stub = _StubAgent(_StubAgentResult(structured=checker_out, rc=0))
    with (
        patch("quikode.workers.subtask_execution.make_agent", return_value=stub),
        patch("quikode.workers.subtask_execution.fsm_runtime"),
    ):
        outcome = w._check_subtask(_S04_WEB_SUBTASK)
    assert outcome.verdict is Verdict.PASS
    assert "parse_failure" not in outcome.checker_text
    assert w._last_doer_envelope is not None
    assert "summary: field required" in w._last_doer_envelope.notes
    assert stub.last_prompt is not None
    assert "bookkeeping envelope was invalid" in stub.last_prompt


def test_doer_parse_failure_still_runs_witnesses(tmp_path) -> None:
    """Witnesses are evidence, so malformed bookkeeping must not suppress them."""
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    with (
        patch("quikode.workers.subtask_execution.run_scoped_witnesses") as mock_runner,
        patch.object(w, "_compute_subtask_diff_excerpt", return_value=""),
    ):
        w._cache_doer_state(
            _S04_WEB_SUBTASK,
            _DoerCallResult(envelope=None, raw_text="bad json", parse_errors=("err",)),
        )
    mock_runner.assert_called_once()
    assert w._last_doer_envelope is not None
    assert "err" in w._last_doer_envelope.notes


# ---------- checker happy + fail paths ----------


def test_checker_pass_returns_pass_verdict(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    w._last_doer_envelope = DoerEnvelope(summary="ok")
    w._last_diff_text = "diff --git a/x b/x"
    w._last_witness_results = {}
    checker_out = SubtaskCheckerOutput(
        verdict="pass",
        findings=[SubtaskCheckerFinding(category="security", verdict="pass", rationale="filter at boundary")],
        overall_assessment="all rows pass",
    )
    stub = _StubAgent(_StubAgentResult(structured=checker_out, rc=0))
    with (
        patch("quikode.workers.subtask_execution.make_agent", return_value=stub),
        patch("quikode.workers.subtask_execution.fsm_runtime"),
    ):
        outcome = w._check_subtask(_S04_WEB_SUBTASK)
    assert outcome.verdict is Verdict.PASS
    assert "VERDICT: PASS" in outcome.checker_text
    # The DoerEnvelope passes through to the checker prompt as informational context.
    assert stub.last_prompt is not None
    assert "informational" in stub.last_prompt.lower()


def test_checker_fail_returns_fail_verdict(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    w._last_doer_envelope = DoerEnvelope(summary="ok")
    w._last_diff_text = "diff --git a/x b/x"
    w._last_witness_results = {}
    checker_out = SubtaskCheckerOutput(
        verdict="fail",
        findings=[
            SubtaskCheckerFinding(category="security", verdict="fail", rationale="missing input check")
        ],
        overall_assessment="rubric gap",
    )
    stub = _StubAgent(_StubAgentResult(structured=checker_out, rc=0))
    with (
        patch("quikode.workers.subtask_execution.make_agent", return_value=stub),
        patch("quikode.workers.subtask_execution.fsm_runtime"),
    ):
        outcome = w._check_subtask(_S04_WEB_SUBTASK)
    assert outcome.verdict is Verdict.FAIL
    assert "VERDICT: FAIL" in outcome.checker_text


def test_checker_parse_failure_synthesizes_parse_failure_outcome(tmp_path) -> None:
    """When the checker's structured output is None / parse_errors is
    populated, the worker fails closed with parse_failure layer."""
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    w._last_doer_envelope = DoerEnvelope(summary="ok")
    w._last_diff_text = "diff"
    w._last_witness_results = {}
    stub = _StubAgent(_StubAgentResult(structured=None, rc=0, parse_errors=("verdict: invalid value",)))
    with (
        patch("quikode.workers.subtask_execution.make_agent", return_value=stub),
        patch("quikode.workers.subtask_execution.fsm_runtime"),
    ):
        outcome = w._check_subtask(_S04_WEB_SUBTASK)
    assert outcome.verdict is Verdict.FAIL
    assert "parse_failure" in outcome.checker_text


# ---------- triage path ----------


def test_triage_renders_failure_layer_into_artifact(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    w._last_doer_envelope = DoerEnvelope(summary="ok")
    w._last_diff_text = "diff"
    triage_out = SubtaskTriageOutput(
        failure_layer="rubric",
        root_cause="missing input validation at apps/web/src/projects/list.tsx:42",
        file_line_cites=["apps/web/src/projects/list.tsx:42"],
        teaching_narrative="The rubric category 'security' demands input boundary checks.",
    )
    stub = _StubAgent(_StubAgentResult(structured=triage_out, rc=0))
    with patch("quikode.workers.subtask_execution.make_agent", return_value=stub):
        text = w._triage_subtask(
            _S04_WEB_SUBTASK, attempt=2, budget=10, checker_output="VERDICT: FAIL\n[FAIL]"
        )
    assert "failure_layer: rubric" in text
    assert "missing input validation" in text


def test_triage_parse_failure_returns_synthetic_text(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    w._last_doer_envelope = DoerEnvelope(summary="ok")
    w._last_diff_text = "diff"
    stub = _StubAgent(_StubAgentResult(structured=None, rc=0, parse_errors=("root_cause: required",)))
    with patch("quikode.workers.subtask_execution.make_agent", return_value=stub):
        text = w._triage_subtask(_S04_WEB_SUBTASK, attempt=2, budget=10, checker_output="VERDICT: FAIL")
    assert "TRIAGE PARSE FAILURE" in text
    assert "parse_failure" in text


# ---------- carry-forward ----------


def test_prior_doer_envelope_carry_forward_via_artifact(tmp_path) -> None:
    """Plan 22 carry-forward: the next attempt's `_fetch_prior_doer_envelope`
    re-parses the prior `subtask_doer:<id>` artifact via pydantic."""
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    prior = DoerEnvelope(
        summary="prior attempt",
        files_touched=["apps/web/src/projects/list.tsx"],
        witness_commands_run=["npm test"],
        notes="needed more work",
    )
    w.store.latest_subtask_doer_output.return_value = prior.model_dump_json()
    re_parsed = w._fetch_prior_doer_envelope(_S04_WEB_SUBTASK, attempt=2)
    assert re_parsed == prior


def test_prior_doer_envelope_returns_none_for_first_attempt(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    assert w._fetch_prior_doer_envelope(_S04_WEB_SUBTASK, attempt=1) is None


def test_prior_doer_envelope_returns_none_on_malformed_artifact(tmp_path) -> None:
    """Legacy artifact (e.g. SELF_AUDIT prose persisted before Plan 38)
    → graceful fall-through; the next attempt gets no carry-forward."""
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    w.store.latest_subtask_doer_output.return_value = "SELF_AUDIT:\n  gate_local_ci: rc=0"
    assert w._fetch_prior_doer_envelope(_S04_WEB_SUBTASK, attempt=2) is None


# ---------- diff capture ----------


def test_diff_capture_combines_status_and_unified_diff(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)

    def fake_git(args):
        if args[0] == "status":
            return 0, " M apps/web/src/projects/list.tsx\n"
        if args[0] == "diff":
            return 0, "diff --git a/x b/x\n+changed"
        return -1, ""

    with patch.object(w, "_git_in_workspace", side_effect=fake_git):
        out = w._compute_subtask_diff_excerpt()
    assert "git status --porcelain:" in out
    assert "M apps/web/src/projects/list.tsx" in out
    assert "diff --git a/x b/x" in out


def test_diff_capture_handles_empty_diff(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)

    def fake_git(args):
        if args[0] == "status":
            return 0, ""
        if args[0] == "diff":
            return 0, ""
        return -1, ""

    with patch.object(w, "_git_in_workspace", side_effect=fake_git):
        out = w._compute_subtask_diff_excerpt()
    assert out == ""


# ---------- log path stub helpers ----------


def test_run_doer_agent_records_call_with_subtask_id(tmp_path) -> None:
    """Plan 38 PR-C: per-call recording is split into a start-marker
    `record_agent_call_started` (phase / cli / model / subtask_id) and a
    finish UPDATE `record_agent_call_finished` (rc / duration_s / tokens
    / cost). The TUI's "agent in-flight" detector keys off `rc IS NULL`
    on the start-marker row, so this split has to land at the worker
    layer for the in-flight signal to be honest."""
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    envelope = DoerEnvelope(summary="x")
    stub = _StubAgent(
        _StubAgentResult(
            structured=envelope,
            rc=0,
            duration_s=12.5,
            tokens_input=1000,
            tokens_output=200,
            cost_usd=0.05,
        )
    )
    with patch("quikode.workers.subtask_execution.make_agent", return_value=stub):
        w._run_doer_agent(_S04_WEB_SUBTASK, "prompt", attempt=1)
    started = w.store.record_agent_call_started.call_args
    assert started.kwargs["phase"] == "subtask_doer"
    assert started.kwargs["subtask_id"] == "S-04-web"
    assert started.kwargs["model"] == cfg.subtask_doer_model
    finished = w.store.record_agent_call_finished.call_args
    assert finished.kwargs["rc"] == 0
    assert finished.kwargs["duration_s"] == 12.5
    assert finished.kwargs["tokens_input"] == 1000
    assert finished.kwargs["cost_usd"] == 0.05


# Suppress unused-import warning for `Path`.
_ = Path
