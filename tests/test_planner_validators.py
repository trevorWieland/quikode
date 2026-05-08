"""Plan 33: tests for the planner validators.

Three validators run after the planner emits a parsed Plan:

* `validate_rubric_coverage` — every rubric category is claimed by at
  least one subtask.
* `validate_evidence_partition` — every node.expected_evidence id is
  claimed by EXACTLY ONE subtask (partition, not cover).
* `validate_standards_paths` — every cited standards doc path resolves
  to a file under the repo root.

Plus `validate_gauntlet_strategy` (length 200-2000 chars).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quikode.dag import Node
from quikode.evaluation_contract import EvaluationContract, StageRubric
from quikode.planner_validators import (
    PlannerValidationError,
    validate_evidence_partition,
    validate_finding_coverage,
    validate_gauntlet_strategy,
    validate_rubric_coverage,
    validate_standards_paths,
)
from quikode.subtask_schema import (
    STABILIZATION_SUBTASK_ID,
    FixupPlan,
    Plan,
    RubricTarget,
    StandardsRef,
    Subtask,
)

# ----- fixtures -----


def _make_contract(categories: list[str] | None = None) -> EvaluationContract:
    cats = categories if categories is not None else ["security", "maintainability"]
    rubric_text = "\n".join(f"- **{c}**" for c in cats)
    return EvaluationContract(
        task_id="R-001",
        local_ci=StageRubric(
            name="local_ci",
            one_line="ci",
            threshold="rc=0",
            grading_template="",
            source_text="Command: `just ci`",
        ),
        rubric=StageRubric(
            name="rubric",
            one_line="rubric",
            threshold="every category >= 7",
            grading_template="",
            source_text=rubric_text,
        ),
        standards=StageRubric(
            name="standards",
            one_line="std",
            threshold="no drift",
            grading_template="",
            source_text="",
        ),
        behavior=StageRubric(
            name="behavior",
            one_line="bhv",
            threshold="all witnessed",
            grading_template="",
            source_text="",
        ),
    )


def _make_node(evidence_ids: list[str] | None = None) -> Node:
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


def _subtask(
    sid: str,
    *,
    rubric_targets: tuple[RubricTarget, ...] = (),
    standards_referenced: tuple[StandardsRef, ...] = (),
    behavior_evidence_advanced: tuple[str, ...] = (),
) -> Subtask:
    return Subtask(
        id=sid,
        title=sid,
        depends_on=(),
        files_to_touch=(),
        boundary="",
        acceptance=("ok",),
        rubric_targets=rubric_targets,
        standards_referenced=standards_referenced,
        behavior_evidence_advanced=behavior_evidence_advanced,
    )


def _plan(*subtasks: Subtask, gauntlet_strategy: str = "x" * 250) -> Plan:
    return Plan(
        node_id="R-001",
        summary="x",
        subtasks=subtasks,
        final_acceptance=("ok",),
        gauntlet_strategy=gauntlet_strategy,
    )


# ----- validate_rubric_coverage -----


def test_rubric_coverage_passes_when_all_categories_claimed():
    contract = _make_contract(["security", "maintainability"])
    plan = _plan(
        _subtask("S-01", rubric_targets=(RubricTarget(category="security", predicted_score=8),)),
        _subtask("S-02", rubric_targets=(RubricTarget(category="maintainability", predicted_score=7),)),
    )
    validate_rubric_coverage(plan, contract)  # no raise


def test_rubric_coverage_fails_when_a_category_is_missing():
    contract = _make_contract(["security", "maintainability"])
    plan = _plan(
        _subtask("S-01", rubric_targets=(RubricTarget(category="security", predicted_score=8),)),
    )
    with pytest.raises(PlannerValidationError) as exc_info:
        validate_rubric_coverage(plan, contract)
    assert exc_info.value.which == "rubric_coverage"
    assert "maintainability" in exc_info.value.message
    assert "is not advanced" in exc_info.value.message


def test_rubric_coverage_fails_when_subtask_uses_unknown_category():
    contract = _make_contract(["security"])
    plan = _plan(
        _subtask("S-01", rubric_targets=(RubricTarget(category="security", predicted_score=8),)),
        _subtask("S-02", rubric_targets=(RubricTarget(category="not-a-real-category", predicted_score=8),)),
    )
    with pytest.raises(PlannerValidationError) as exc_info:
        validate_rubric_coverage(plan, contract)
    assert "not-a-real-category" in exc_info.value.message
    assert "S-02" in exc_info.value.message


def test_rubric_coverage_fails_when_contract_has_no_categories():
    contract = _make_contract(categories=[])
    plan = _plan(_subtask("S-01"))
    with pytest.raises(PlannerValidationError) as exc_info:
        validate_rubric_coverage(plan, contract)
    assert "no rubric categories" in exc_info.value.message


# ----- validate_evidence_partition -----


def test_evidence_partition_passes_when_each_id_owned_once():
    node = _make_node(["E-1", "E-2"])
    plan = _plan(
        _subtask("S-01", behavior_evidence_advanced=("E-1",)),
        _subtask("S-02", behavior_evidence_advanced=("E-2",)),
    )
    validate_evidence_partition(plan, node)  # no raise


def test_evidence_partition_fails_when_id_missing():
    node = _make_node(["E-1", "E-2"])
    plan = _plan(_subtask("S-01", behavior_evidence_advanced=("E-1",)))
    with pytest.raises(PlannerValidationError) as exc_info:
        validate_evidence_partition(plan, node)
    assert "E-2" in exc_info.value.message
    assert "missing evidence" in exc_info.value.message


def test_evidence_partition_fails_when_id_duplicated():
    node = _make_node(["E-1"])
    plan = _plan(
        _subtask("S-01", behavior_evidence_advanced=("E-1",)),
        _subtask("S-02", behavior_evidence_advanced=("E-1",)),
    )
    with pytest.raises(PlannerValidationError) as exc_info:
        validate_evidence_partition(plan, node)
    assert "duplicated evidence" in exc_info.value.message
    assert "S-01" in exc_info.value.message
    assert "S-02" in exc_info.value.message


def test_evidence_partition_fails_when_id_unknown():
    node = _make_node(["E-1"])
    plan = _plan(
        _subtask("S-01", behavior_evidence_advanced=("E-1",)),
        _subtask("S-02", behavior_evidence_advanced=("E-2-not-in-node",)),
    )
    with pytest.raises(PlannerValidationError) as exc_info:
        validate_evidence_partition(plan, node)
    assert "unknown evidence" in exc_info.value.message
    assert "E-2-not-in-node" in exc_info.value.message


def test_evidence_partition_passes_when_node_has_no_evidence():
    node = _make_node([])
    plan = _plan(_subtask("S-01"))
    validate_evidence_partition(plan, node)  # no raise


def test_evidence_partition_fails_on_unexpected_claim_with_empty_evidence():
    node = _make_node([])
    plan = _plan(_subtask("S-01", behavior_evidence_advanced=("phantom",)))
    with pytest.raises(PlannerValidationError) as exc_info:
        validate_evidence_partition(plan, node)
    assert "no expected_evidence" in exc_info.value.message
    assert "phantom" in exc_info.value.message


def test_evidence_partition_z99_exempt_when_other_subtask_owns_id():
    """Z-99 with no behavior_evidence_advanced is fine; the partition is
    held by the earlier subtask."""
    node = _make_node(["E-1"])
    z99 = Subtask(
        id=STABILIZATION_SUBTASK_ID,
        title="z99",
        depends_on=("S-01",),
        files_to_touch=(),
        boundary="",
        acceptance=("gate",),
    )
    plan = _plan(
        _subtask("S-01", behavior_evidence_advanced=("E-1",)),
        z99,
    )
    validate_evidence_partition(plan, node)  # no raise


# ----- validate_standards_paths -----


def test_standards_paths_passes_when_all_exist(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "x.md").write_text("standards body")
    plan = _plan(
        _subtask(
            "S-01",
            standards_referenced=(StandardsRef(doc_path="docs/x.md", section="intro"),),
        ),
    )
    validate_standards_paths(plan, tmp_path)  # no raise


def test_standards_paths_fails_when_path_missing(tmp_path: Path):
    plan = _plan(
        _subtask(
            "S-01",
            standards_referenced=(StandardsRef(doc_path="docs/missing.md", section="x"),),
        ),
    )
    with pytest.raises(PlannerValidationError) as exc_info:
        validate_standards_paths(plan, tmp_path)
    assert "S-01" in exc_info.value.message
    assert "docs/missing.md" in exc_info.value.message
    assert "does not exist" in exc_info.value.message


def test_standards_paths_rejects_absolute_paths(tmp_path: Path):
    plan = _plan(
        _subtask(
            "S-01",
            standards_referenced=(StandardsRef(doc_path="/etc/passwd", section="x"),),
        ),
    )
    with pytest.raises(PlannerValidationError) as exc_info:
        validate_standards_paths(plan, tmp_path)
    assert "absolute paths are forbidden" in exc_info.value.message


def test_standards_paths_fails_when_path_is_dir(tmp_path: Path):
    docs = tmp_path / "docs" / "subdir"
    docs.mkdir(parents=True)
    plan = _plan(
        _subtask(
            "S-01",
            standards_referenced=(StandardsRef(doc_path="docs/subdir", section="x"),),
        ),
    )
    with pytest.raises(PlannerValidationError) as exc_info:
        validate_standards_paths(plan, tmp_path)
    assert "non-file" in exc_info.value.message


# ----- validate_gauntlet_strategy -----


def test_gauntlet_strategy_passes_in_range():
    plan = _plan(_subtask("S-01"), gauntlet_strategy="x" * 250)
    validate_gauntlet_strategy(plan)  # no raise


def test_gauntlet_strategy_fails_when_too_short():
    plan = _plan(_subtask("S-01"), gauntlet_strategy="too short")
    with pytest.raises(PlannerValidationError) as exc_info:
        validate_gauntlet_strategy(plan)
    assert exc_info.value.which == "gauntlet_strategy"
    assert ">= 200" in exc_info.value.message


def test_gauntlet_strategy_fails_when_too_long():
    plan = _plan(_subtask("S-01"), gauntlet_strategy="x" * 2001)
    with pytest.raises(PlannerValidationError) as exc_info:
        validate_gauntlet_strategy(plan)
    assert exc_info.value.which == "gauntlet_strategy"
    assert "<= 2000" in exc_info.value.message


# ----- validate_finding_coverage (Plan 33 calibration: fixup-only) -----


def _fixup_plan(*subtasks: Subtask) -> FixupPlan:
    return FixupPlan(summary="x", subtasks=subtasks, findings_addressed=())


def test_finding_coverage_no_op_on_empty_findings():
    """fixup-final / fixup-ci / fixup-review rounds carry no typed
    finding bundle; the validator should short-circuit."""
    plan = _fixup_plan(_subtask("F-01"))
    validate_finding_coverage(plan, [])  # no raise


def test_finding_coverage_passes_when_every_finding_covered():
    plan = _fixup_plan(
        _subtask(
            "F-01",
            rubric_targets=(RubricTarget(category="security", predicted_score=8),),
        ),
        _subtask(
            "F-02",
            standards_referenced=(StandardsRef(doc_path="docs/x.md", section="A"),),
        ),
        _subtask(
            "F-03",
            behavior_evidence_advanced=("B-0066",),
        ),
    )
    validate_finding_coverage(
        plan,
        [
            "rubric:security",
            "standards:docs/x.md§A",
            "behavior:B-0066",
        ],
    )


def test_finding_coverage_fails_when_finding_missing():
    """A `rubric:security` finding with NO subtask claiming security in
    rubric_targets is the partition-failure shape that surfaced as the
    R-0002 BLOCK on the tanren deploy."""
    plan = _fixup_plan(
        _subtask(
            "F-01",
            rubric_targets=(RubricTarget(category="maintainability", predicted_score=7),),
        ),
    )
    with pytest.raises(PlannerValidationError) as exc_info:
        validate_finding_coverage(plan, ["rubric:security", "rubric:maintainability"])
    assert exc_info.value.which == "finding_coverage"
    assert "rubric:security" in exc_info.value.message
    assert "missing" in exc_info.value.message


def test_finding_coverage_fails_on_duplicate_coverage():
    """Two subtasks both claim `rubric:security` — partition discipline
    requires exactly-one ownership."""
    plan = _fixup_plan(
        _subtask(
            "F-01",
            rubric_targets=(RubricTarget(category="security", predicted_score=8),),
        ),
        _subtask(
            "F-02",
            rubric_targets=(RubricTarget(category="security", predicted_score=9),),
        ),
    )
    with pytest.raises(PlannerValidationError) as exc_info:
        validate_finding_coverage(plan, ["rubric:security"])
    assert exc_info.value.which == "finding_coverage"
    assert "duplicated" in exc_info.value.message
    assert "F-01" in exc_info.value.message
    assert "F-02" in exc_info.value.message


def test_finding_coverage_flags_unknown_rubric_claim():
    """A subtask declares `rubric_targets=[security]` but no audit finding
    references security — surfaces as `unknown` so the planner trims the
    over-claim."""
    plan = _fixup_plan(
        _subtask(
            "F-01",
            rubric_targets=(RubricTarget(category="security", predicted_score=8),),
        ),
        _subtask(
            "F-02",
            behavior_evidence_advanced=("B-0066",),
        ),
    )
    with pytest.raises(PlannerValidationError) as exc_info:
        validate_finding_coverage(plan, ["behavior:B-0066"])
    assert exc_info.value.which == "finding_coverage"
    assert "unknown" in exc_info.value.message
    assert "rubric:security" in exc_info.value.message


def test_finding_coverage_accepts_empty_rubric_targets_when_finding_is_behavior_only():
    """Plan 33 calibration: `rubric_targets=[]` is legitimate on a fixup
    that only addresses a behavior witness — the prior validator
    `validate_rubric_coverage` would have rejected this even though the
    audit only flagged a behavior id, which is exactly the pathology
    that BLOCKed R-0002 on the tanren deploy."""
    plan = _fixup_plan(
        _subtask("F-01", behavior_evidence_advanced=("B-0066",)),
    )
    validate_finding_coverage(plan, ["behavior:B-0066"])  # no raise
