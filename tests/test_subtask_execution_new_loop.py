"""Plan 47: end-to-end smoke for the per-subtask doer→diff→checker→
triage pipeline.

The doer / checker / triage agents are stubbed via `make_agent`; the
witness runner and git helpers are mocked too. Plan 47 retired the
doer bookkeeping envelope, so the doer call returns plain text and
the checker grades the diff + witness output directly. The tests
verify wiring (doer artifact persists as plain text, diff is
captured, witnesses run, checker prompt has no self-report block)
rather than agent-CLI behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from quikode.agent_schemas import (
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


@dataclass
class _StubAgentResult:
    structured: Any = None
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
    w._last_diff_text = ""
    w._last_witness_results = {}
    return w


# ---------- doer happy path ----------


def test_doer_artifact_is_plain_text(tmp_path) -> None:
    """Plan 47: the doer call returns plain text and the worker
    persists it as the `subtask_doer:<id>` artifact (no JSON
    envelope, no parsing)."""
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    raw_doer_text = "I edited apps/web/src/projects/list.tsx and ran the tests."
    stub = _StubAgent(_StubAgentResult(structured=None, rc=0, raw_text=raw_doer_text))
    with (
        patch("quikode.workers.subtask_execution.make_agent", return_value=stub),
    ):
        w._run_doer_agent(_S04_WEB_SUBTASK, "doer prompt", attempt=1)
    artifact_calls = w.store.add_artifact.call_args_list
    kinds = [c[0][1] for c in artifact_calls]
    assert "subtask_doer:S-04-web" in kinds
    body = next(c for c in artifact_calls if c[0][1] == "subtask_doer:S-04-web")[0][2]
    # Plain-text artifact, last 20k chars.
    assert body == raw_doer_text


def test_doer_artifact_truncates_to_tail(tmp_path) -> None:
    """Plan 47: the artifact is the trailing 20000 chars of stdout —
    long doer outputs don't blow up the artifact stream."""
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    long_text = "x" * 25000 + "TAIL_MARKER"
    stub = _StubAgent(_StubAgentResult(structured=None, rc=0, raw_text=long_text))
    with patch("quikode.workers.subtask_execution.make_agent", return_value=stub):
        w._run_doer_agent(_S04_WEB_SUBTASK, "p", attempt=1)
    body = w.store.add_artifact.call_args_list[-1][0][2]
    assert body.endswith("TAIL_MARKER")
    assert len(body) == 20000


def test_cache_doer_state_runs_witnesses_with_subtask_evidence_ids(tmp_path) -> None:
    """`_cache_doer_state` runs the scoped witness runner with the
    subtask's evidence ids — no envelope-derived fallback commands."""
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
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
        w._cache_doer_state(_S04_WEB_SUBTASK)
    assert captured["evidence_ids"] == ["B-0061-test-positive"]
    assert captured["per_witness_timeout_s"] == cfg.subtask_witness_timeout_seconds
    # Plan 47: no fallback_commands plumbed through.
    assert "fallback_commands" not in captured
    assert w._last_diff_text == "diff text"
    assert w._last_witness_results["B-0061-test-positive"]["classification"] == "OK"


# ---------- checker happy + fail paths ----------


def test_checker_pass_returns_pass_verdict(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
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
    # Plan 47: no doer self-report block in the checker prompt.
    assert stub.last_prompt is not None
    assert "doer's self-report" not in stub.last_prompt.lower()
    assert "informational" not in stub.last_prompt.lower()


def test_checker_fail_returns_fail_verdict(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
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
    w._last_diff_text = "diff"
    triage_out = SubtaskTriageOutput(
        failure_layer="rubric",
        root_cause="missing input validation at apps/web/src/projects/list.tsx:42",
        file_line_cites=["apps/web/src/projects/list.tsx:42"],
        teaching_narrative="The rubric category 'security' demands input boundary checks.",
    )
    stub = _StubAgent(_StubAgentResult(structured=triage_out, rc=0))
    with patch("quikode.workers.subtask_execution.make_agent", return_value=stub):
        text, layer = w._triage_subtask(
            _S04_WEB_SUBTASK, attempt=2, budget=10, checker_output="VERDICT: FAIL\n[FAIL]"
        )
    assert "failure_layer: rubric" in text
    assert "missing input validation" in text
    assert layer == "rubric"


def test_triage_parse_failure_returns_synthetic_text(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    w._last_diff_text = "diff"
    stub = _StubAgent(_StubAgentResult(structured=None, rc=0, parse_errors=("root_cause: required",)))
    with patch("quikode.workers.subtask_execution.make_agent", return_value=stub):
        text, layer = w._triage_subtask(
            _S04_WEB_SUBTASK, attempt=2, budget=10, checker_output="VERDICT: FAIL"
        )
    assert "TRIAGE PARSE FAILURE" in text
    assert "parse_failure" in text
    assert layer is None


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
    """Plan 38 PR-C / Plan 47: per-call recording is split into a
    start-marker `record_agent_call_started` (phase / cli / model /
    subtask_id) and a finish UPDATE `record_agent_call_finished` (rc /
    duration_s / tokens / cost). The TUI's "agent in-flight" detector
    keys off `rc IS NULL` on the start-marker row, so this split has
    to land at the worker layer for the in-flight signal to be honest."""
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    stub = _StubAgent(
        _StubAgentResult(
            structured=None,
            rc=0,
            duration_s=12.5,
            tokens_input=1000,
            tokens_output=200,
            cost_usd=0.05,
            raw_text="apply_patch ok",
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


# ---------- plan 51 + 53: empty-diff branch dispatch ----------


def test_check_subtask_empty_diff_with_green_gates_spec_kind_is_no_op_done(tmp_path) -> None:
    """Plan 53: empty diff + green objective gate + green witnesses on
    a `kind != "fixup_ci"` subtask synthesizes a PASS outcome (no-op
    DONE path) without invoking the LLM checker. The dedicated
    `subtask_no_op_done:<id>` artifact records the verification."""
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    w._last_diff_text = ""
    w._last_witness_results = {}
    make_agent_calls: list[str] = []

    def fake_make_agent(role: str, _cfg: Config) -> Any:
        make_agent_calls.append(role)
        raise AssertionError(f"checker should be skipped on empty diff; got make_agent({role!r})")

    with (
        patch("quikode.workers.subtask_execution.make_agent", side_effect=fake_make_agent),
        patch("quikode.workers.subtask_execution.fsm_runtime"),
    ):
        outcome = w._check_subtask(_S04_WEB_SUBTASK)

    assert outcome.verdict is Verdict.PASS
    assert outcome.transient is False
    assert outcome.rc is None
    assert outcome.checker_text.startswith("VERDICT: PASS\nROOT_CAUSE: subtask no-op DONE")
    assert make_agent_calls == []
    artifact_kinds = [c[0][1] for c in w.store.add_artifact.call_args_list]
    assert f"subtask_checker:{_S04_WEB_SUBTASK.id}" in artifact_kinds
    assert f"subtask_no_op_done:{_S04_WEB_SUBTASK.id}" in artifact_kinds


def test_check_subtask_empty_diff_with_green_gates_fixup_ci_is_cannot_reproduce(tmp_path) -> None:
    """Plan 53: empty diff + green objective gate + green witnesses on
    a `kind="fixup_ci"` subtask synthesizes a FAIL outcome with the
    cannot_reproduce prefix and persists the
    `subtask_cannot_reproduce:<id>` artifact. The new K=2 stop-loss
    fires on the second occurrence of this signature; the LLM checker
    is never invoked."""
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    w._last_diff_text = ""
    w._last_witness_results = {}
    make_agent_calls: list[str] = []

    def fake_make_agent(role: str, _cfg: Config) -> Any:
        make_agent_calls.append(role)
        raise AssertionError(f"checker should be skipped on empty diff; got make_agent({role!r})")

    fixup_ci_subtask = _S04_WEB_SUBTASK.model_copy(update={"kind": "fixup-ci"})
    with (
        patch("quikode.workers.subtask_execution.make_agent", side_effect=fake_make_agent),
        patch("quikode.workers.subtask_execution.fsm_runtime"),
    ):
        outcome = w._check_subtask(fixup_ci_subtask)

    assert outcome.verdict is Verdict.FAIL
    assert outcome.transient is False
    assert outcome.rc is None
    assert outcome.checker_text.startswith("VERDICT: FAIL\nROOT_CAUSE: cannot reproduce CI failure locally")
    assert make_agent_calls == []
    artifact_kinds = [c[0][1] for c in w.store.add_artifact.call_args_list]
    assert f"subtask_checker:{fixup_ci_subtask.id}" in artifact_kinds
    assert f"subtask_cannot_reproduce:{fixup_ci_subtask.id}" in artifact_kinds


def test_check_subtask_empty_diff_with_failed_witness_is_transport(tmp_path) -> None:
    """Plan 51 preserved: when the witnesses fail (rc != 0 or
    classification == FAIL) the empty-diff path still routes through
    the plan-51 transport-class FAIL prefix so the existing transport
    stop-loss budget protects against doer-model regressions."""
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    w._last_diff_text = ""
    w._last_witness_results = {
        "B-0061-test-positive": {
            "rc": 1,
            "classification": "FAIL",
            "stdout_excerpt": "",
            "stderr_excerpt": "ouch",
            "runtime_ms": 50,
            "note": "failed",
        }
    }
    make_agent_calls: list[str] = []

    def fake_make_agent(role: str, _cfg: Config) -> Any:
        make_agent_calls.append(role)
        raise AssertionError(f"checker should be skipped on empty diff; got make_agent({role!r})")

    with (
        patch("quikode.workers.subtask_execution.make_agent", side_effect=fake_make_agent),
        patch("quikode.workers.subtask_execution.fsm_runtime"),
    ):
        outcome = w._check_subtask(_S04_WEB_SUBTASK)

    assert outcome.verdict is Verdict.FAIL
    assert outcome.checker_text.startswith("VERDICT: FAIL\nROOT_CAUSE: doer produced no diff")
    assert make_agent_calls == []


def test_check_subtask_non_empty_diff_still_invokes_llm_checker(tmp_path) -> None:
    """Plan 51 negative control: when the diff is non-empty the original
    LLM-checker path still runs."""
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    w._last_diff_text = "diff --git a/x b/x\n+changed"
    w._last_witness_results = {}
    checker_out = SubtaskCheckerOutput(
        verdict="pass",
        findings=[SubtaskCheckerFinding(category="security", verdict="pass", rationale="ok")],
        overall_assessment="ok",
    )
    stub = _StubAgent(_StubAgentResult(structured=checker_out, rc=0))
    with (
        patch("quikode.workers.subtask_execution.make_agent", return_value=stub),
        patch("quikode.workers.subtask_execution.fsm_runtime"),
    ):
        outcome = w._check_subtask(_S04_WEB_SUBTASK)
    assert outcome.verdict is Verdict.PASS
    assert stub.last_prompt is not None  # LLM checker DID run


# Suppress unused-import warning for `Path`.
_ = Path
