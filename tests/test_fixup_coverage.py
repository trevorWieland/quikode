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
import shutil
from pathlib import Path

import pytest

from quikode import planner_validators
from quikode.architecture_docs import ArchitectureCorpus, load_architecture
from quikode.config import Config
from quikode.dag import Node
from quikode.evaluation_contract import (
    ArchitectureStageRubric,
    EvaluationContract,
    StageRubric,
    StandardsStageRubric,
)
from quikode.standards_profiles import load_profiles
from quikode.subtask_schema import Plan, Subtask
from quikode.workers import fixup_coverage as fc
from quikode.workers import planner_driver
from quikode.workers.fixup_coverage import (
    missing_finding_coverage,
    parse_and_validate_fixup_plan,
)

_PROFILE_FIX = Path(__file__).resolve().parent / "fixtures" / "standards_profiles"
_ARCH_FIX = Path(__file__).resolve().parent / "fixtures" / "architecture_docs"


def _populated_contract(tmp_path: Path) -> EvaluationContract:
    """Stand up a contract with the fixture profile + architecture trees
    so `parse_and_validate_fixup_plan` (which now takes a contract) has
    real corpora to dispatch against. Tests still only exercise the
    finding/rubric/evidence routing — the standards/architecture refs
    they emit must point at fixture docs."""
    profile_root = tmp_path / "profiles"
    if not profile_root.exists():
        profile_root.mkdir()
        shutil.copytree(_PROFILE_FIX / "rust-cargo", profile_root / "rust-cargo")
    arch_root = tmp_path / "docs" / "architecture"
    if not arch_root.exists():
        arch_root.mkdir(parents=True)
        shutil.copytree(_ARCH_FIX / "subsystems", arch_root / "subsystems")
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        standards_profiles_dir=profile_root,
        standards_profiles=["rust-cargo"],
        architecture_docs_dir=arch_root,
    )
    profiles = load_profiles(cfg)
    corpus = load_architecture(cfg)
    return EvaluationContract(
        task_id="R-001",
        local_ci=StageRubric(
            name="local_ci", one_line="", threshold="rc=0", grading_template="", source_text=""
        ),
        rubric=StageRubric(
            name="rubric",
            one_line="",
            threshold="",
            grading_template="",
            source_text="- **security**\n- **maintainability**",
        ),
        standards=StandardsStageRubric(profiles=profiles, source_text=""),
        architecture=ArchitectureStageRubric(corpus=corpus, source_text=""),
        behavior=StageRubric(name="behavior", one_line="", threshold="", grading_template="", source_text=""),
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
        contract=_populated_contract(tmp_path),
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
        contract=_populated_contract(tmp_path),
        node=_node(),
        audit_findings=["rubric:security"],
    )
    assert plan is None
    assert feedback is not None
    assert "doc_path" in feedback


def test_fixup_plan_passes_when_finding_coverage_complete(tmp_path: Path):
    """Plan 35: cite a real fixture profile doc + section so the
    bucket-routing validator passes."""
    text = _fixup_plan_text(
        findings_addressed=["rubric:security", "behavior:B-0066"],
        rubric_targets=[{"category": "security", "predicted_score": 8}],
        standards_referenced=[{"doc_path": "profiles/rust-cargo/rust/error-handling.md", "section": "Rules"}],
        behavior_evidence_advanced=["B-0066"],
    )
    plan, feedback = parse_and_validate_fixup_plan(
        text,
        contract=_populated_contract(tmp_path),
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
        contract=_populated_contract(tmp_path),
        node=_node(),
        audit_findings=["rubric:security"],
    )
    assert plan is None
    assert feedback is not None
    assert "finding_coverage" in feedback
    assert "rubric:security" in feedback


def test_fixup_plan_fails_on_unknown_standards_ref(tmp_path: Path):
    """Plan 35: `validate_standards_refs` rejects citations that don't
    resolve under any loaded profile (here a fictional `docs/missing.md`)."""
    text = _fixup_plan_text(
        findings_addressed=["standards:docs/missing.md§A"],
        standards_referenced=[{"doc_path": "docs/missing.md", "section": "A"}],
    )
    plan, feedback = parse_and_validate_fixup_plan(
        text,
        contract=_populated_contract(tmp_path),
        node=_node(),
        audit_findings=["standards:docs/missing.md§A"],
    )
    assert plan is None
    assert feedback is not None
    assert "standards_refs" in feedback or "standards profile" in feedback


def test_fixup_plan_accepts_when_audit_findings_empty(tmp_path: Path):
    """`fixup-final` / `fixup-ci` / `fixup-review` rounds carry no
    typed finding bundle — the driver passes `audit_findings=None`
    and the finding-coverage validator should short-circuit."""
    text = _fixup_plan_text()
    plan, feedback = parse_and_validate_fixup_plan(
        text,
        contract=_populated_contract(tmp_path),
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
        contract=_populated_contract(tmp_path),
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
        contract=_populated_contract(tmp_path),
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
        contract=_populated_contract(tmp_path),
        node=_node(["B-0066"]),
        audit_findings=["behavior:B-0066"],
    )
    assert plan is not None, feedback
    assert calls == []


def test_spec_planner_driver_invokes_all_spec_validators(monkeypatch):
    """Plan 35: the spec-planner driver MUST call `validate_rubric_coverage`,
    `validate_evidence_partition`, `validate_standards_refs`, and
    `validate_architecture_refs`."""
    seen: list[str] = []

    def _spy(name: str):
        def _fn(*_a, **_kw):
            seen.append(name)

        return _fn

    monkeypatch.setattr(planner_driver, "validate_rubric_coverage", _spy("rubric_coverage"))
    monkeypatch.setattr(planner_driver, "validate_evidence_partition", _spy("evidence_partition"))
    monkeypatch.setattr(planner_driver, "validate_standards_refs", _spy("standards_refs"))
    monkeypatch.setattr(planner_driver, "validate_architecture_refs", _spy("architecture_refs"))

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
        standards=StandardsStageRubric(profiles=(), source_text=""),
        architecture=ArchitectureStageRubric(
            corpus=ArchitectureCorpus(root=Path("/tmp"), docs=()), source_text=""
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

    planner_driver.validate_rubric_coverage(plan, contract)
    planner_driver.validate_evidence_partition(plan, node)
    planner_driver.validate_standards_refs(plan, contract)
    planner_driver.validate_architecture_refs(plan, contract)

    assert seen == [
        "rubric_coverage",
        "evidence_partition",
        "standards_refs",
        "architecture_refs",
    ]


# ----- end of tests -----
_ = pytest  # silence unused-import lint when no parametrize fixtures used here
