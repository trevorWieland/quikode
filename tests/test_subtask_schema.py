"""Schema + validator tests for the v2 runtime planner output.

Plan 38 PR-B.4 retired the prose-parsing path (`extract_json` /
`parse_planner_output` / `parse_fixup_planner_output`). The wire schema
(`agent_schemas.PlannerOutput` / `FixupPlannerOutput`) is validated by
the JsonAgent layer; the runtime shape (`Plan` / `FixupPlan`) is built
through `validate_and_build_plan` / direct `FixupPlan.model_validate`
plus the per-driver wire→runtime translators.

These tests pin the runtime model + Z-99 stabilization injection
behavior directly against `validate_and_build_plan` / `FixupPlan`,
bypassing the wire layer (which has its own tests in
`test_agent_schemas.py`).
"""

from __future__ import annotations

import pytest

from quikode.agent_schemas import PlannerOutput
from quikode.subtask_schema import (
    STABILIZATION_SUBTASK_ID,
    FixupPlan,
    PlanValidationError,
    validate_and_build_plan,
)
from quikode.workers.planner_driver import _wire_to_runtime_plan

# ----------------------------- validate_and_build_plan ---------------------


def _good_subtask(sid="S-1", deps=()):
    return {
        "id": sid,
        "title": f"task {sid}",
        "depends_on": list(deps),
        "files_to_touch": ["a.rs"],
        "acceptance": ["compiles"],
    }


def test_valid_minimal_plan():
    raw = {
        "node_id": "R-1",
        "subtasks": [_good_subtask()],
        "final_acceptance": ["just ci passes"],
    }
    plan = validate_and_build_plan(raw)
    assert plan.node_id == "R-1"
    assert len(plan.subtasks) == 1


def test_missing_top_keys():
    with pytest.raises(PlanValidationError, match="(?i)required|missing"):
        validate_and_build_plan({"node_id": "R-1"})


def test_subtasks_must_be_non_empty():
    with pytest.raises(PlanValidationError, match="(?i)at least 1|non-empty"):
        validate_and_build_plan(
            {
                "node_id": "R-1",
                "subtasks": [],
                "final_acceptance": ["x"],
            }
        )


def test_duplicate_subtask_id():
    with pytest.raises(PlanValidationError, match="duplicate"):
        validate_and_build_plan(
            {
                "node_id": "R-1",
                "subtasks": [_good_subtask("S-1"), _good_subtask("S-1")],
                "final_acceptance": ["x"],
            }
        )


def test_unknown_dep_caught():
    with pytest.raises(PlanValidationError, match="unknown id"):
        validate_and_build_plan(
            {
                "node_id": "R-1",
                "subtasks": [_good_subtask("S-1", deps=["S-bogus"])],
                "final_acceptance": ["x"],
            }
        )


def test_cycle_caught():
    with pytest.raises(PlanValidationError, match="cycle"):
        validate_and_build_plan(
            {
                "node_id": "R-1",
                "subtasks": [
                    _good_subtask("A", deps=["B"]),
                    _good_subtask("B", deps=["A"]),
                ],
                "final_acceptance": ["x"],
            }
        )


def test_empty_acceptance_caught():
    bad = _good_subtask("S-1")
    bad["acceptance"] = []
    with pytest.raises(PlanValidationError, match="acceptance"):
        validate_and_build_plan(
            {
                "node_id": "R-1",
                "subtasks": [bad],
                "final_acceptance": ["x"],
            }
        )


def test_node_id_mismatch_when_expected():
    raw = {
        "node_id": "R-2",
        "subtasks": [_good_subtask()],
        "final_acceptance": ["x"],
    }
    with pytest.raises(PlanValidationError, match="doesn't match expected"):
        validate_and_build_plan(raw, expected_node_id="R-1")


# ----------------------------- topo_order ----------------------------------


def test_topo_order_simple():
    raw = {
        "node_id": "R-1",
        "subtasks": [
            _good_subtask("C", deps=["A", "B"]),
            _good_subtask("A"),
            _good_subtask("B", deps=["A"]),
        ],
        "final_acceptance": ["x"],
    }
    plan = validate_and_build_plan(raw)
    order = [s.id for s in plan.topo_order()]
    assert order == ["A", "B", "C"]


# ----------------------------- FixupPlan -----------------------------


def test_fixup_plan_accepts_findings_addressed_top_level():
    """Plan 33: `addresses_findings` per-subtask is retired (D2). The
    fixup-planner emits `findings_addressed` at the top level of the
    FixupPlan; per-subtask coverage will be derived from the stage-typed
    fields (`rubric_targets`, `standards_referenced`,
    `behavior_evidence_advanced`) once PR-B rewrites the fixup-planner
    prompt and pipeline. Until then the orchestrator's completeness
    check trusts only the top-level array (see
    `pre_pr.py::_missing_finding_coverage`)."""
    raw = {
        "summary": "decompose audit findings",
        "findings_addressed": ["rubric:add-validation", "behavior:missing-witness"],
        "subtasks": [
            {
                "id": "F-1-1-add-validation",
                "title": "Add input validation",
                "depends_on": [],
                "files_to_touch": ["src/foo.rs"],
                "boundary": "validation only",
                "acceptance": ["cargo check passes"],
                "kind": "fixup-pre-pr-audit",
            },
            {
                "id": "F-1-2-add-witness",
                "title": "Add B-0030 BDD witness",
                "depends_on": [],
                "files_to_touch": ["tests/bdd/features/B-0030.feature"],
                "boundary": "test fixture only",
                "acceptance": ["scenario runs"],
                "kind": "fixup-pre-pr-audit",
            },
        ],
    }
    plan = FixupPlan.model_validate(raw)
    assert len(plan.subtasks) == 2
    assert plan.findings_addressed == (
        "rubric:add-validation",
        "behavior:missing-witness",
    )


def test_fixup_plan_accepts_subtasks_without_top_level_findings():
    """Spec subtasks + non-audit fixup kinds don't need the field. Default
    is empty tuple; absence is fine."""
    raw = {
        "summary": "fixup CI",
        "subtasks": [
            {
                "id": "F-1-1-fix-ci",
                "title": "Fix CI",
                "depends_on": [],
                "files_to_touch": ["src/lib.rs"],
                "boundary": "",
                "acceptance": ["just ci passes"],
                "kind": "fixup-ci",
            }
        ],
    }
    plan = FixupPlan.model_validate(raw)
    assert len(plan.subtasks) == 1
    assert plan.findings_addressed == ()


# ----------------------------- plan 24: stabilization subtask injection -----


def _two_subtask_plan_dict() -> dict:
    return {
        "node_id": "R-001",
        "summary": "test",
        "subtasks": [
            {
                "id": "S-01-domain",
                "title": "domain types",
                "depends_on": [],
                "files_to_touch": ["a.rs"],
                "boundary": "",
                "acceptance": ["compiles"],
            },
            {
                "id": "S-02-services",
                "title": "service layer",
                "depends_on": ["S-01-domain"],
                "files_to_touch": ["b.rs"],
                "boundary": "",
                "acceptance": ["passes tests"],
            },
        ],
        "final_acceptance": ["just ci passes"],
    }


def test_stabilization_injection_appends_when_command_given():
    raw = _two_subtask_plan_dict()
    plan = validate_and_build_plan(raw, spec_gate_command="just check")
    assert len(plan.subtasks) == 3
    last = plan.subtasks[-1]
    assert last.id == STABILIZATION_SUBTASK_ID
    assert last.depends_on == ("S-01-domain", "S-02-services")
    assert last.files_to_touch == ()
    assert any("just check" in a for a in last.acceptance)
    assert "gate-keeping cross-file fixes" in last.boundary or "scope review" in last.boundary
    assert last.kind == "spec"


def test_stabilization_injection_skipped_when_command_none():
    raw = _two_subtask_plan_dict()
    plan = validate_and_build_plan(raw, spec_gate_command=None)
    assert len(plan.subtasks) == 2
    assert all(s.id != STABILIZATION_SUBTASK_ID for s in plan.subtasks)


def test_wire_to_runtime_plan_round_trips_architecture_referenced():
    """Plan 38 PR-B.1 added `architecture_referenced` to the wire schema
    `SubtaskSpec`. The runtime `Subtask.architecture_referenced` is a
    `tuple[ArchitectureRef, ...]` (plan 35 PR-A) where the wire shape is
    a plain `list[ArchitectureRefSchema]`. The wire→runtime translator
    must preserve every doc_path/section pair across the boundary."""
    payload = {
        "node_id": "R-001",
        "summary": "x",
        "subtasks": [
            {
                "id": "S-01-domain",
                "title": "domain",
                "depends_on": [],
                "files_to_touch": ["a.rs"],
                "boundary": "x",
                "acceptance": ["compiles"],
                "interfaces": [],
                "notes": "",
                "architecture_referenced": [
                    {"doc_path": "docs/architecture/subsystems/x.md", "section": "API"},
                    {"doc_path": "docs/architecture/subsystems/y.md", "section": "Z"},
                ],
                "standards_referenced": [
                    {"doc_path": "profiles/rust/error-handling.md", "section": "Rules"},
                ],
            },
        ],
        "final_acceptance": ["just ci passes"],
    }
    planner_output = PlannerOutput.model_validate(payload)
    plan = _wire_to_runtime_plan(
        planner_output,
        expected_node_id="R-001",
        spec_gate_command=None,
        rubric_categories=None,
        rubric_min_score=None,
    )
    s = plan.subtasks[0]
    # Wire schema's plain list became the runtime's tuple, with each
    # entry preserved as the runtime ArchitectureRef shape.
    assert isinstance(s.architecture_referenced, tuple)
    assert len(s.architecture_referenced) == 2
    assert s.architecture_referenced[0].doc_path == "docs/architecture/subsystems/x.md"
    assert s.architecture_referenced[0].section == "API"
    assert s.architecture_referenced[1].doc_path == "docs/architecture/subsystems/y.md"
    assert s.architecture_referenced[1].section == "Z"
    # standards_referenced makes the same trip across the same translator.
    assert isinstance(s.standards_referenced, tuple)
    assert s.standards_referenced[0].doc_path == "profiles/rust/error-handling.md"
    assert s.standards_referenced[0].section == "Rules"


def test_stabilization_injection_idempotent_if_already_present():
    """Re-parsing the same plan_text on resume must not double-inject."""
    raw = _two_subtask_plan_dict()
    plan_v1 = validate_and_build_plan(raw, spec_gate_command="just check")
    # Simulate persisting + re-parsing by serializing back to dict shape.
    raw_v2 = {
        "node_id": plan_v1.node_id,
        "summary": plan_v1.summary,
        "subtasks": [
            {
                "id": s.id,
                "title": s.title,
                "depends_on": list(s.depends_on),
                "files_to_touch": list(s.files_to_touch),
                "boundary": s.boundary,
                "acceptance": list(s.acceptance),
                "notes": s.notes,
                "kind": s.kind,
            }
            for s in plan_v1.subtasks
        ],
        "final_acceptance": list(plan_v1.final_acceptance),
    }
    plan_v2 = validate_and_build_plan(raw_v2, spec_gate_command="just check")
    assert len(plan_v2.subtasks) == len(plan_v1.subtasks)
    assert sum(1 for s in plan_v2.subtasks if s.id == STABILIZATION_SUBTASK_ID) == 1
