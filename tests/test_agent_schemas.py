"""Plan 38 PR-A: round-trip + extra="forbid" + closed-enum tests for agent schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from quikode.agent_schemas import (
    BehaviorVerification,
    DoerEnvelope,
    FixupPlannerOutput,
    MergePlannerOutput,
    PlannerOutput,
    PrePRBehaviorAuditOutput,
    PrePRRubricAuditOutput,
    PrePRStandardsAuditOutput,
    ProgressVerdict,
    RubricCategoryScore,
    RubricGap,
    StandardsFinding,
    SubtaskCheckerFinding,
    SubtaskCheckerOutput,
    SubtaskSpec,
    SubtaskTriageOutput,
)


def _minimal_subtask() -> SubtaskSpec:
    return SubtaskSpec(id="S-01", title="t", acceptance=["x"])


def _round_trip(model):
    """Dump as JSON, parse back via model_validate_json, verify equality."""
    raw = model.model_dump_json()
    cls = type(model)
    reloaded = cls.model_validate_json(raw)
    assert reloaded == model
    return reloaded


# ---------- planner / fixup-planner / merge-planner ----------


def test_planner_output_round_trip() -> None:
    plan = PlannerOutput(
        node_id="N-1",
        summary="s",
        gauntlet_strategy="g" * 250,
        subtasks=[_minimal_subtask()],
        final_acceptance=["just ci passes"],
    )
    _round_trip(plan)


def test_planner_output_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        PlannerOutput.model_validate(
            {
                "node_id": "N-1",
                "subtasks": [{"id": "S-01", "acceptance": ["x"]}],
                "final_acceptance": ["x"],
                "extra_unknown_key": "boom",
            }
        )


def test_planner_subtasks_min_length() -> None:
    with pytest.raises(ValidationError):
        PlannerOutput(
            node_id="N-1",
            subtasks=[],
            final_acceptance=["x"],
        )


def test_fixup_planner_output_round_trip() -> None:
    plan = FixupPlannerOutput(
        summary="round 1",
        subtasks=[_minimal_subtask()],
        findings_addressed=["rubric:add-validation"],
    )
    _round_trip(plan)


def test_merge_planner_output_round_trip() -> None:
    plan = MergePlannerOutput(
        node_id="M-1",
        summary="integration",
        gauntlet_strategy="merge gauntlet",
        subtasks=[_minimal_subtask()],
        final_acceptance=["both parents stay green"],
        merge_context_summary="parent A renamed X; parent B added a caller",
    )
    _round_trip(plan)


# ---------- doer ----------


def test_doer_envelope_round_trip() -> None:
    env = DoerEnvelope(
        summary="touched two files",
        files_touched=["a.py", "b.py"],
        witness_commands_run=["pytest tests/"],
        notes="ok",
    )
    _round_trip(env)


def test_doer_envelope_defaults() -> None:
    """All fields optional with safe defaults."""
    env = DoerEnvelope()
    assert env.summary == ""
    assert env.files_touched == []
    assert env.witness_commands_run == []
    assert env.notes == ""


# ---------- subtask checker ----------


def test_subtask_checker_output_round_trip() -> None:
    out = SubtaskCheckerOutput(
        verdict="pass",
        findings=[
            SubtaskCheckerFinding(category="security", verdict="pass", rationale="ok"),
            SubtaskCheckerFinding(category="standards:docs/x.md§foo", verdict="unknown"),
        ],
        overall_assessment="looks good",
    )
    _round_trip(out)


def test_subtask_checker_verdict_closed_enum() -> None:
    with pytest.raises(ValidationError):
        SubtaskCheckerOutput.model_validate(
            {
                "verdict": "PASS",  # uppercase is not in the Literal lowercase set
                "findings": [],
            }
        )


def test_subtask_checker_finding_verdict_closed_enum() -> None:
    with pytest.raises(ValidationError):
        SubtaskCheckerFinding.model_validate({"category": "x", "verdict": "tbd"})


# ---------- subtask triage ----------


def test_subtask_triage_output_round_trip() -> None:
    out = SubtaskTriageOutput(
        failure_layer="rubric",
        root_cause="diff doesn't advance edge-case-handling",
        file_line_cites=["a.py:42"],
        teaching_narrative="...",
    )
    _round_trip(out)


def test_subtask_triage_failure_layer_closed_enum() -> None:
    with pytest.raises(ValidationError):
        SubtaskTriageOutput.model_validate({"failure_layer": "self_audit_mismatch", "root_cause": "x"})
    # New values accepted
    SubtaskTriageOutput.model_validate({"failure_layer": "parse_failure", "root_cause": "x"})


# ---------- pre-PR rubric ----------


def test_pre_pr_rubric_round_trip() -> None:
    out = PrePRRubricAuditOutput(
        categories=[
            RubricCategoryScore(
                name="security",
                score=8,
                rationale="ok",
                gaps_to_reach_ten=[RubricGap(id="add-x", description="add x", concrete_fix="x")],
            )
        ],
        overall_assessment="ok",
    )
    _round_trip(out)


def test_pre_pr_rubric_score_bounds() -> None:
    with pytest.raises(ValidationError):
        RubricCategoryScore(name="x", score=11)
    with pytest.raises(ValidationError):
        RubricCategoryScore(name="x", score=0)


# ---------- pre-PR standards ----------


def test_pre_pr_standards_round_trip() -> None:
    out = PrePRStandardsAuditOutput(
        findings=[
            StandardsFinding(
                id="rename-foo",
                file="src/foo.py",
                line=10,
                severity="medium",
                standards_doc_ref="docs/architecture.md §3.2",
                description="rename for clarity",
                concrete_fix="git mv ...",
            )
        ],
        overall_assessment="one medium",
    )
    _round_trip(out)


def test_pre_pr_standards_severity_closed_enum() -> None:
    with pytest.raises(ValidationError):
        StandardsFinding.model_validate(
            {
                "id": "x",
                # not in {low, medium, high, critical}
                "severity": "urgent",
                "standards_doc_ref": "docs/x.md",
                "description": "x",
            }
        )


# ---------- pre-PR behavior ----------


def test_pre_pr_behavior_round_trip() -> None:
    out = PrePRBehaviorAuditOutput(
        behaviors=[
            BehaviorVerification(
                behavior_id="B-001",
                verified=True,
                evidence_seen="pytest passes",
                completeness_gaps=[],
            )
        ],
        overall_assessment="all green",
    )
    _round_trip(out)


# ---------- progress ----------


def test_progress_verdict_round_trip() -> None:
    out = ProgressVerdict(verdict="progressing", rationale="root cause shifted")
    _round_trip(out)


def test_progress_verdict_closed_enum() -> None:
    with pytest.raises(ValidationError):
        ProgressVerdict.model_validate({"verdict": "flatlined"})  # plan 38 normalizes to flatline
    ProgressVerdict.model_validate({"verdict": "flatline"})


# ---------- subtask spec ----------


def test_subtask_spec_round_trip() -> None:
    s = SubtaskSpec(
        id="S-01",
        title="t",
        depends_on=["S-00"],
        files_to_touch=["a.py"],
        boundary="api only",
        acceptance=["x"],
        notes="n",
        interfaces=["web"],
        kind="spec",
        rubric_targets=[],
        standards_referenced=[],
        behavior_evidence_advanced=["B-1"],
    )
    _round_trip(s)


def test_subtask_spec_acceptance_required() -> None:
    with pytest.raises(ValidationError):
        SubtaskSpec(id="S-01", acceptance=[])
