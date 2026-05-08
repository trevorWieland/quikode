"""Plan 33 PR-B: end-to-end smoke for the per-subtask doer→parser→
short-circuit→checker→triage pipeline. Uses a fixture-driven version
of plan §11's R-0050 worked example — the witness runner is mocked so
no real BDD tests run; we just verify the wiring fires in the right
order and the right structured payloads flow through.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from quikode.config import Config
from quikode.dag import Node
from quikode.evaluation_contract import build_for
from quikode.self_audit import (
    ParsedSelfAudit,
    ShortCircuit,
    parse_self_audit,
    short_circuit_decision,
)
from quikode.subtask_schema import RubricTarget, Subtask
from quikode.types import Verdict
from quikode.worker import TaskWorker
from quikode.workers.outcomes import CheckerOutcome

# ---------- fixtures ----------


_R0050_NODE = Node(
    id="R-0050",
    kind="behavior",
    milestone="M-1",
    title="Project archival",
    scope="Users can mark projects as archived; archived projects are excluded from default list views.",
    depends_on=(),
    completes_behaviors=("B-0061", "B-0062"),
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
        {
            "behavior_id": "B-0061",
            "kind": "test",
            "interfaces": ["web"],
            "witnesses": ["falsification"],
            "command": "npm run test:e2e -- detail-view-still-works",
            "description": "archived project remains retrievable by id",
        },
    ),
    playbook=(),
    rationale="",
    risks=(),
    raw={},
)


_S04_WEB_SUBTASK = Subtask(
    id="S-04-web",
    title="Web list view filter + retain detail-view access",
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
    behavior_evidence_advanced=(
        "B-0061-test-positive",
        "B-0061-test-falsification",
    ),
)


_CLEAN_DOER_OUTPUT = """\
Implemented the filter. Diff cited below; ran the witnesses; rc=0 on local CI.

SELF_AUDIT:
  gate_local_ci: rc=0 (cmd: just check)
  gate_rubric:
    security: predicted_score=8  rationale: input sanitized at boundary  evidence: apps/web/src/projects/list.tsx:42
    code_quality: predicted_score=8  rationale: filter via DomainService no duplication  evidence: apps/web/src/projects/list.tsx:18
  gate_standards:
  gate_behavior:
    B-0061-test-positive: witnessed_by=npm run test:e2e -- list-excludes-archived  output_excerpt=PASS (1.2s)
    B-0061-test-falsification: witnessed_by=npm run test:e2e -- detail-view-still-works  output_excerpt=PASS (0.9s)
  diff_reconcile:
    apps/web/src/projects/list.tsx: in_lane
"""


_BAD_DOER_OUTPUT_RUBRIC_LOW = """\
SELF_AUDIT:
  gate_local_ci: rc=0 (cmd: just check)
  gate_rubric:
    security: predicted_score=4  rationale: still WIP  evidence: a.tsx:1
    code_quality: predicted_score=8  rationale: ok  evidence: a.tsx:1
  gate_standards:
  gate_behavior:
    B-0061-test-positive: witnessed_by=npm test  output_excerpt=PASS
    B-0061-test-falsification: witnessed_by=npm test  output_excerpt=PASS
  diff_reconcile:
    a.tsx: in_lane
"""


_BAD_DOER_OUTPUT_RISK_TOKEN = """\
SELF_AUDIT:
  gate_local_ci: rc=0 (cmd: just check)
  gate_rubric:
    security: predicted_score=8  rationale: STUB validation will land in S-05  evidence: a.tsx:1
    code_quality: predicted_score=8  rationale: ok  evidence: a.tsx:1
  gate_standards:
  gate_behavior:
    B-0061-test-positive: witnessed_by=npm test  output_excerpt=PASS
    B-0061-test-falsification: witnessed_by=npm test  output_excerpt=PASS
  diff_reconcile:
    a.tsx: in_lane
"""


_MALFORMED_DOER_OUTPUT = """\
Did the work. Forgot the SELF_AUDIT block.
"""


# ---------- helpers ----------


def _cfg(tmp_path) -> Config:
    return Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        pre_pr_rubric_categories=["security", "code_quality"],
        pre_pr_rubric_min_score=7,
        subtask_witness_timeout_seconds=15,
    )


def _build_worker(cfg: Config) -> Any:
    w = TaskWorker.__new__(TaskWorker)
    w.cfg = cfg
    w.node = _R0050_NODE
    w.handle = MagicMock()
    w.handle.container_name = "qk-stub"
    w.log_path = None
    w.store = MagicMock()
    w.store.latest_subtask_doer_output.return_value = None
    w.last_doer_summary = ""
    w.plan = None
    w._contract = build_for(_R0050_NODE, cfg)
    w._last_parsed_self_audit = None
    w._last_self_audit_outcome = None
    w._last_diff_text = ""
    w._last_witness_results = {}
    return w


# ---------- end-to-end pipeline wiring ----------


def test_clean_self_audit_proceeds_to_llm_checker(tmp_path) -> None:
    """Doer emits a clean SELF_AUDIT → parser succeeds → short-circuit
    PROCEEDs → witnesses run → LLM checker is invoked."""
    cfg = _cfg(tmp_path)
    contract = build_for(_R0050_NODE, cfg)
    parsed = parse_self_audit(_CLEAN_DOER_OUTPUT)
    assert parsed.parse_errors == ()
    decision = short_circuit_decision(
        parsed,
        contract=contract,
        subtask=_S04_WEB_SUBTASK,
        rubric_min_score=cfg.pre_pr_rubric_min_score,
    )
    assert decision.decision is ShortCircuit.PROCEED


def test_short_circuit_skips_checker_on_low_rubric_score(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    contract = build_for(_R0050_NODE, cfg)
    parsed = parse_self_audit(_BAD_DOER_OUTPUT_RUBRIC_LOW)
    assert parsed.parse_errors == ()
    decision = short_circuit_decision(
        parsed,
        contract=contract,
        subtask=_S04_WEB_SUBTASK,
        rubric_min_score=cfg.pre_pr_rubric_min_score,
    )
    assert decision.decision is ShortCircuit.FAIL_FAST
    assert decision.failure_layer == "rubric"


def test_short_circuit_skips_checker_on_risk_token(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    contract = build_for(_R0050_NODE, cfg)
    parsed = parse_self_audit(_BAD_DOER_OUTPUT_RISK_TOKEN)
    decision = short_circuit_decision(
        parsed,
        contract=contract,
        subtask=_S04_WEB_SUBTASK,
        rubric_min_score=cfg.pre_pr_rubric_min_score,
    )
    assert decision.decision is ShortCircuit.FAIL_FAST
    assert decision.failure_layer == "self_audit_mismatch"


def test_check_subtask_uses_cached_short_circuit_outcome(tmp_path) -> None:
    """When the doer cached a short-circuit FAIL outcome, the worker's
    `_check_subtask` should return it without invoking the LLM checker."""
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    # Simulate the doer caching a fail-fast outcome (e.g. rubric below threshold).
    w._last_self_audit_outcome = CheckerOutcome(
        verdict=Verdict.FAIL,
        checker_text=(
            "VERDICT: FAIL\nROOT_CAUSE: short-circuit failure_layer=rubric.\nDETAILS:\nrubric below min"
        ),
        transient=False,
        rc=None,
        stderr="",
    )
    w._last_parsed_self_audit = parse_self_audit(_BAD_DOER_OUTPUT_RUBRIC_LOW)
    # FSM transition is infrastructure; mock it so the short-circuit
    # behavior under test is the only thing exercised.
    with (
        patch("quikode.workers.subtask_execution.fsm_runtime"),
        patch.object(w, "_run_llm_subtask_checker") as mock_llm,
    ):
        outcome = w._check_subtask(_S04_WEB_SUBTASK)
    mock_llm.assert_not_called()
    assert outcome.verdict is Verdict.FAIL
    assert "failure_layer=rubric" in outcome.checker_text


def test_witness_runner_invoked_with_subtask_evidence_ids(tmp_path) -> None:
    """When the doer's SELF_AUDIT is clean, the worker calls the witness
    runner with the subtask's `behavior_evidence_advanced` ids."""
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    parsed = parse_self_audit(_CLEAN_DOER_OUTPUT)

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
            },
            "B-0061-test-falsification": {
                "rc": 0,
                "stdout_excerpt": "PASS",
                "stderr_excerpt": "",
                "runtime_ms": 100,
                "classification": "OK",
                "note": "ok",
            },
        }

    with (
        patch("quikode.workers.subtask_execution.run_scoped_witnesses", side_effect=fake_run_scoped),
        patch.object(w, "_compute_subtask_diff_excerpt", return_value="diff"),
    ):
        # `_cache_doer_state` is the entry-point that triggers the runner
        # post-parse. Call it directly (the surrounding `_do_subtask` is
        # the doer LLM invocation we've mocked away).
        w._cache_doer_state(_S04_WEB_SUBTASK, parsed)

    assert captured["evidence_ids"] == [
        "B-0061-test-positive",
        "B-0061-test-falsification",
    ]
    assert captured["per_witness_timeout_s"] == cfg.subtask_witness_timeout_seconds


def test_malformed_self_audit_produces_parse_errors(tmp_path) -> None:
    """The parser surfaces parse errors when the SELF_AUDIT block is missing.
    The worker uses these to drive the re-prompt loop."""
    parsed = parse_self_audit(_MALFORMED_DOER_OUTPUT)
    assert parsed.parse_errors
    assert "anchor line not found" in parsed.parse_errors[0]


def test_cache_doer_state_records_self_audit_artifact(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    w = _build_worker(cfg)
    parsed = parse_self_audit(_CLEAN_DOER_OUTPUT)
    with (
        patch("quikode.workers.subtask_execution.run_scoped_witnesses", return_value={}),
        patch.object(w, "_compute_subtask_diff_excerpt", return_value=""),
    ):
        w._cache_doer_state(_S04_WEB_SUBTASK, parsed)
    # The artifact recorder should have been called for the structured
    # SELF_AUDIT carry-forward (Plan 22 → Plan 33 D14).
    artifact_kinds = [
        call_args[0][1] if len(call_args[0]) > 1 else None
        for call_args in w.store.add_artifact.call_args_list
    ]
    assert "subtask_self_audit:S-04-web" in artifact_kinds


def test_parsed_self_audit_object_carries_forward_to_next_attempt() -> None:
    """The structured `ParsedSelfAudit` is what the next doer attempt
    receives — not the loose stdout from PR-A. This lets the next
    attempt see the per-category predicted scores from the failed
    attempt and decide what to repair."""
    parsed = parse_self_audit(_BAD_DOER_OUTPUT_RUBRIC_LOW)
    assert isinstance(parsed, ParsedSelfAudit)
    assert parsed.gate_rubric["security"].predicted_score == 4
    assert parsed.gate_rubric["code_quality"].predicted_score == 8
