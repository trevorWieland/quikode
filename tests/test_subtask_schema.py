"""Schema parsing + validation for v2 planner output."""

from __future__ import annotations

import json

import pytest

from quikode.subtask_schema import (
    STABILIZATION_SUBTASK_ID,
    PlanValidationError,
    extract_json,
    parse_fixup_planner_output,
    parse_planner_output,
    validate_and_build_plan,
)

# ----------------------------- extract_json --------------------------------


def test_extract_fenced_json():
    text = """Some narrative.

```json
{
  "node_id": "R-1",
  "subtasks": []
}
```

More narrative."""
    obj = extract_json(text)
    assert obj["node_id"] == "R-1"


def test_extract_unfenced_json():
    text = """Here you go: { "node_id": "R-1", "x": "y", "nested": { "a": 1 } } and more text."""
    obj = extract_json(text)
    assert obj["node_id"] == "R-1"
    assert obj["nested"]["a"] == 1


def test_extract_handles_strings_with_braces():
    text = """{ "id": "x", "msg": "{ literal braces }" }"""
    obj = extract_json(text)
    assert obj["msg"] == "{ literal braces }"


def test_extract_empty_raises():
    with pytest.raises(PlanValidationError, match="empty"):
        extract_json("")


def test_extract_no_json_raises():
    with pytest.raises(PlanValidationError, match="no JSON"):
        extract_json("plain text with no braces")


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


# ----------------------------- end-to-end ----------------------------------


def test_parse_full_planner_output():
    text = """The agent thought hard and produced this plan:

```json
{
  "node_id": "R-0001",
  "summary": "Add account create + sign-in",
  "subtasks": [
    {
      "id": "S-01-domain",
      "title": "Account domain types",
      "depends_on": [],
      "files_to_touch": ["crates/tanren-identity-policy/src/account.rs"],
      "boundary": "Domain only",
      "acceptance": ["compiles", "exports Account struct"]
    },
    {
      "id": "S-02-events",
      "title": "Account events",
      "depends_on": ["S-01-domain"],
      "files_to_touch": ["crates/tanren-identity-policy/src/events.rs"],
      "boundary": "Events module",
      "acceptance": ["compiles"]
    }
  ],
  "final_acceptance": [
    "just ci passes",
    "B-0043 BDD scenarios pass"
  ]
}
```

That's the plan."""
    plan = parse_planner_output(text, expected_node_id="R-0001")
    assert plan.node_id == "R-0001"
    assert plan.summary.startswith("Add account")
    assert len(plan.subtasks) == 2
    assert plan.subtasks[0].id == "S-01-domain"
    assert plan.final_acceptance == ("just ci passes", "B-0043 BDD scenarios pass")


# ----------------------------- FixupPlan -----------------------------


def test_fixup_plan_accepts_per_subtask_addresses_findings():
    """R-0020/R-0021 regression: the fixup-planner.md prompt instructs the
    planner to emit a per-subtask `addresses_findings` array for
    `kind="fixup-pre-pr-audit"` so the orchestrator can verify every audit
    finding is mapped to a slice. The Subtask model previously had
    `extra="forbid"` with no such field — every subtask carrying that key
    was rejected, the tuple emptied, and the worker BLOCKed the task."""
    text = """```json
{
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
      "addresses_findings": ["rubric:add-validation"]
    },
    {
      "id": "F-1-2-add-witness",
      "title": "Add B-0030 BDD witness",
      "depends_on": [],
      "files_to_touch": ["tests/bdd/features/B-0030.feature"],
      "boundary": "test fixture only",
      "acceptance": ["scenario runs"],
      "kind": "fixup-pre-pr-audit",
      "addresses_findings": ["behavior:missing-witness"]
    }
  ]
}
```"""
    plan = parse_fixup_planner_output(text)
    assert len(plan.subtasks) == 2
    assert plan.findings_addressed == (
        "rubric:add-validation",
        "behavior:missing-witness",
    )
    # Per-subtask traceability survives.
    assert plan.subtasks[0].addresses_findings == ("rubric:add-validation",)
    assert plan.subtasks[1].addresses_findings == ("behavior:missing-witness",)


def test_fixup_plan_accepts_subtasks_without_addresses_findings():
    """Spec subtasks + non-audit fixup kinds don't need the field. Default
    is empty tuple; absence is fine."""
    text = """```json
{
  "summary": "fixup CI",
  "subtasks": [
    {
      "id": "F-1-1-fix-ci",
      "title": "Fix CI",
      "depends_on": [],
      "files_to_touch": ["src/lib.rs"],
      "boundary": "",
      "acceptance": ["just ci passes"],
      "kind": "fixup-ci"
    }
  ]
}
```"""
    plan = parse_fixup_planner_output(text)
    assert len(plan.subtasks) == 1
    assert plan.subtasks[0].addresses_findings == ()
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


def test_parse_planner_output_threads_command_through():
    raw = _two_subtask_plan_dict()
    text = "```json\n" + json.dumps(raw) + "\n```"
    plan = parse_planner_output(text, expected_node_id="R-001", spec_gate_command="just check")
    assert any(s.id == STABILIZATION_SUBTASK_ID for s in plan.subtasks)
