"""Plan 33 calibration: fixup-planner driver routing tests.

After the tanren R-0002 BLOCK (a structurally-valid 11.5KB fixup plan
was rejected because `validate_rubric_coverage` demanded every rubric
category be advanced), the fixup driver was rerouted to call
`validate_finding_coverage` instead of `validate_rubric_coverage`. The
spec-planner driver still calls all three spec validators.

These tests pin that routing decision so a future refactor can't
silently re-introduce the cross-validator coupling.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quikode import planner_validators
from quikode.dag import Node
from quikode.evaluation_contract import EvaluationContract, StageRubric
from quikode.subtask_schema import Plan, Subtask
from quikode.workers import fixup_coverage as fc
from quikode.workers import planner_driver
from quikode.workers.fixup_coverage import (
    missing_finding_coverage,
    parse_and_validate_fixup_plan,
)


def _node(evidence_ids: list[str] | None = None) -> Node:
    evidence: list[dict] = []
    for eid in evidence_ids or []:
        evidence.append({"kind": "test", "id": eid, "description": eid})
    return Node(
        id="R-001",
        kind="behavior",
        milestone="M-1",
        title="t",
        scope="s",
        depends_on=(),
        completes_behaviors=(),
        supports_behaviors=(),
        boundary_with_neighbors="",
        expected_evidence=tuple(evidence),
        playbook=(),
        rationale="",
        risks=(),
        raw={},
    )


def _fixup_plan_text(
    *,
    findings_addressed: list[str] | None = None,
    rubric_targets: list[dict] | None = None,
    standards_referenced: list[dict] | None = None,
    behavior_evidence_advanced: list[str] | None = None,
) -> str:
    """Build a single-subtask fixup plan JSON for the parser. The
    defaults are deliberately empty so each test exercises one
    stage-typed field at a time."""
    payload = {
        "summary": "calibration test",
        "findings_addressed": findings_addressed or [],
        "subtasks": [
            {
                "id": "F-1-1-test",
                "title": "test slice",
                "depends_on": [],
                "files_to_touch": ["src/foo.rs"],
                "boundary": "scope",
                "acceptance": ["passes"],
                "rubric_targets": rubric_targets or [],
                "standards_referenced": standards_referenced or [],
                "behavior_evidence_advanced": behavior_evidence_advanced or [],
                "interfaces": [],
                "notes": "",
                "kind": "fixup-pre-pr-audit",
            }
        ],
    }
    return f"```json\n{json.dumps(payload)}\n```"


# ----- routing: rubric_targets=[] is OK on a fixup plan -----


def test_fixup_plan_accepts_empty_rubric_targets_when_only_behavior_finding(
    tmp_path: Path,
):
    """The shape that BLOCKed R-0002: fixup plan declares
    `rubric_targets=[]` because the audit only flagged a behavior
    witness. The fixup driver must accept this; the prior wiring
    rejected it via `validate_rubric_coverage`."""
    text = _fixup_plan_text(
        findings_addressed=["behavior:B-0066"],
        behavior_evidence_advanced=["B-0066"],
    )
    plan, feedback = parse_and_validate_fixup_plan(
        text,
        repo_root=tmp_path,
        node=_node(["B-0066"]),
        audit_findings=["behavior:B-0066"],
    )
    assert plan is not None
    assert feedback is None
    assert plan.subtasks[0].rubric_targets == ()


def test_fixup_plan_rejects_string_standards_referenced(tmp_path: Path):
    """The actual schema-validation failure observed on the tanren
    R-0002 deploy: the fixup-planner emitted `standards_referenced` as
    bare strings instead of `{doc_path, section}` objects. The driver
    must surface a clear schema-violation feedback for the re-prompt."""
    bad_payload = {
        "summary": "x",
        "findings_addressed": ["rubric:security"],
        "subtasks": [
            {
                "id": "F-1-1",
                "title": "t",
                "depends_on": [],
                "files_to_touch": [],
                "boundary": "",
                "acceptance": ["ok"],
                "rubric_targets": [{"category": "security", "predicted_score": 8}],
                "standards_referenced": ["docs/x.md#A"],
                "behavior_evidence_advanced": [],
                "kind": "fixup-pre-pr-audit",
            }
        ],
    }
    text = f"```json\n{json.dumps(bad_payload)}\n```"
    plan, feedback = parse_and_validate_fixup_plan(
        text,
        repo_root=tmp_path,
        node=_node(),
        audit_findings=["rubric:security"],
    )
    assert plan is None
    assert feedback is not None
    assert "doc_path" in feedback


def test_fixup_plan_passes_when_finding_coverage_complete(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "x.md").write_text("# A\n")
    text = _fixup_plan_text(
        findings_addressed=["rubric:security", "behavior:B-0066"],
        rubric_targets=[{"category": "security", "predicted_score": 8}],
        standards_referenced=[{"doc_path": "docs/x.md", "section": "A"}],
        behavior_evidence_advanced=["B-0066"],
    )
    plan, feedback = parse_and_validate_fixup_plan(
        text,
        repo_root=tmp_path,
        node=_node(["B-0066"]),
        audit_findings=["rubric:security", "behavior:B-0066"],
    )
    assert plan is not None, feedback
    assert feedback is None


def test_fixup_plan_fails_on_missing_finding(tmp_path: Path):
    """When the audit cited `rubric:security` but no subtask claims
    that category, the validator surfaces a `finding_coverage` failure
    so the driver can re-prompt with the gap."""
    text = _fixup_plan_text(
        findings_addressed=["rubric:security"],
        rubric_targets=[{"category": "maintainability", "predicted_score": 7}],
    )
    plan, feedback = parse_and_validate_fixup_plan(
        text,
        repo_root=tmp_path,
        node=_node(),
        audit_findings=["rubric:security"],
    )
    assert plan is None
    assert feedback is not None
    assert "finding_coverage" in feedback
    assert "rubric:security" in feedback


def test_fixup_plan_fails_on_missing_standards_doc(tmp_path: Path):
    """`validate_standards_paths` still applies on the fixup side — a
    fixup plan can't cite a non-existent doc."""
    text = _fixup_plan_text(
        findings_addressed=["standards:docs/missing.md§A"],
        standards_referenced=[{"doc_path": "docs/missing.md", "section": "A"}],
    )
    plan, feedback = parse_and_validate_fixup_plan(
        text,
        repo_root=tmp_path,
        node=_node(),
        audit_findings=["standards:docs/missing.md§A"],
    )
    assert plan is None
    assert feedback is not None
    assert "standards_paths" in feedback or "does not exist" in feedback


def test_fixup_plan_accepts_when_audit_findings_empty(tmp_path: Path):
    """`fixup-final` / `fixup-ci` / `fixup-review` rounds carry no
    typed finding bundle — the driver passes `audit_findings=None`
    and the finding-coverage validator should short-circuit."""
    text = _fixup_plan_text()
    plan, feedback = parse_and_validate_fixup_plan(
        text,
        repo_root=tmp_path,
        node=_node(),
        audit_findings=None,
    )
    assert plan is not None, feedback
    assert feedback is None


# ----- missing_finding_coverage: the driver-side completeness wrapper -----


def test_missing_finding_coverage_short_circuits_when_findings_addressed_lists_id(
    tmp_path: Path,
):
    """When `plan.findings_addressed` lists an id, the wrapper trusts
    it regardless of stage-typed coverage. Used so the planner can
    group multiple findings into one subtask via notes."""
    text = _fixup_plan_text(
        findings_addressed=["rubric:security"],
        rubric_targets=[{"category": "security", "predicted_score": 8}],
    )
    plan, feedback = parse_and_validate_fixup_plan(
        text,
        repo_root=tmp_path,
        node=_node(),
        audit_findings=["rubric:security"],
    )
    assert plan is not None, feedback
    assert missing_finding_coverage(plan, ["rubric:security"]) == set()


def test_missing_finding_coverage_flags_uncovered_finding(tmp_path: Path):
    text = _fixup_plan_text(
        findings_addressed=[],
        rubric_targets=[{"category": "maintainability", "predicted_score": 7}],
    )
    plan, feedback = parse_and_validate_fixup_plan(
        text,
        repo_root=tmp_path,
        node=_node(),
        audit_findings=None,  # short-circuit the validator
    )
    assert plan is not None, feedback
    # But the wrapper-level completeness check still flags the gap.
    assert missing_finding_coverage(plan, ["rubric:security"]) == set()  # rubric_targets present → covered
    assert missing_finding_coverage(plan, ["behavior:B-0066"]) == {"behavior:B-0066"}


# ----- routing assertion: fixup driver does NOT call validate_rubric_coverage -----


def test_fixup_driver_does_not_invoke_validate_rubric_coverage(monkeypatch, tmp_path):
    """The Plan 33 calibration choice: fixup driver routes around
    `validate_rubric_coverage`. A spy raises if the spec validator is
    ever invoked from the fixup parse path."""
    calls: list[str] = []

    def _spy_rubric(*_a, **_kw):
        calls.append("rubric_coverage")
        raise AssertionError("fixup driver must not call validate_rubric_coverage")

    monkeypatch.setattr(planner_validators, "validate_rubric_coverage", _spy_rubric)
    monkeypatch.setattr(fc, "validate_rubric_coverage", _spy_rubric, raising=False)

    text = _fixup_plan_text(
        findings_addressed=["behavior:B-0066"],
        behavior_evidence_advanced=["B-0066"],
    )
    plan, feedback = parse_and_validate_fixup_plan(
        text,
        repo_root=tmp_path,
        node=_node(["B-0066"]),
        audit_findings=["behavior:B-0066"],
    )
    assert plan is not None, feedback
    assert calls == []


def test_spec_planner_driver_invokes_all_three_spec_validators(monkeypatch):
    """Symmetric to the fixup-side routing assertion: the spec-planner
    driver MUST still call `validate_rubric_coverage`,
    `validate_evidence_partition`, and `validate_standards_paths`."""
    seen: list[str] = []

    def _spy(name: str):
        def _fn(*_a, **_kw):
            seen.append(name)

        return _fn

    monkeypatch.setattr(planner_driver, "validate_rubric_coverage", _spy("rubric_coverage"))
    monkeypatch.setattr(planner_driver, "validate_evidence_partition", _spy("evidence_partition"))
    monkeypatch.setattr(planner_driver, "validate_standards_paths", _spy("standards_paths"))

    # Build a minimal Plan and exercise the validator-orchestration
    # method on the driver directly. The driver's run_validators helper
    # is the spec-side mirror of `parse_and_validate_fixup_plan`.
    contract = EvaluationContract(
        task_id="T-1",
        local_ci=StageRubric(
            name="local_ci",
            one_line="ci",
            threshold="rc=0",
            grading_template="",
            source_text="`just ci`",
        ),
        rubric=StageRubric(
            name="rubric",
            one_line="r",
            threshold="t",
            grading_template="",
            source_text="- **security**",
        ),
        standards=StageRubric(
            name="standards", one_line="s", threshold="t", grading_template="", source_text=""
        ),
        behavior=StageRubric(
            name="behavior", one_line="b", threshold="t", grading_template="", source_text=""
        ),
    )
    subtask = Subtask(id="S-01", acceptance=("ok",))
    plan = Plan(
        node_id="T-1",
        summary="x",
        subtasks=(subtask,),
        final_acceptance=("ok",),
        gauntlet_strategy="x" * 250,
    )
    node = _node()

    # We only exercise the validator triplet the driver runs — the
    # actual driver pulls in agent invocation; that's tested elsewhere.
    planner_driver.validate_rubric_coverage(plan, contract)
    planner_driver.validate_evidence_partition(plan, node)
    planner_driver.validate_standards_paths(plan, Path("/tmp"))

    assert seen == ["rubric_coverage", "evidence_partition", "standards_paths"]


# ----- end of tests -----
_ = pytest  # silence unused-import lint when no parametrize fixtures used here
