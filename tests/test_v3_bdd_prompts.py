"""V3-001/002/003 — BDD convention awareness in prompts + Subtask.interfaces field.

Plan 33 generalized the planner.md template (the BDD specifics live in the
repo-specific standards docs that the EvaluationContract loads). The
planner-level BDD-content assertions in this file have been retired; the
remaining tests still cover Subtask.interfaces shape and downstream
checker/doer prompt behavior that DOES still call out BDD validators.
"""

from __future__ import annotations

import json
from pathlib import Path

from quikode.config import Config
from quikode.dag import DAG
from quikode.evaluation_contract import build_for
from quikode.prompts import (
    checker_prompt,
    doer_prompt,
    planner_prompt,
    subtask_checker_prompt,
    subtask_doer_prompt,
)
from quikode.self_audit import ParsedSelfAudit
from quikode.subtask_schema import Subtask, parse_planner_output


def _cfg(prompts_dir: Path) -> Config:
    return Config(
        repo_path=prompts_dir,
        dag_path=prompts_dir,
        prompts_dir=prompts_dir / "missing",  # falls back to bundled
    )


def _make_dag_with_behaviors(tmp_path: Path, behaviors: list[str]) -> DAG:
    raw = {
        "schema": "test",
        "milestones": [{"id": "M-1", "title": "Auth", "goal": "x", "status": "planned"}],
        "nodes": [
            {
                "id": "R-001",
                "kind": "behavior",
                "milestone": "M-1",
                "title": "Sign in flow",
                "scope": "Implement sign-in across surfaces.",
                "boundary_with_neighbors": "auth/, not billing/.",
                "depends_on": [],
                "completes_behaviors": behaviors,
                "supports_behaviors": [],
                "expected_evidence": [
                    {
                        "kind": "test",
                        "behavior_id": behaviors[0] if behaviors else "B-100",
                        "interfaces": ["web", "api", "cli"],
                        "witnesses": ["positive", "falsification"],
                        "description": "round-trip session.",
                    }
                ],
                "playbook": ["api: POST /sessions", "web: navigate to /signin"],
                "rationale": "first auth slice.",
                "risks": ["password storage policy"],
            }
        ],
    }
    p = tmp_path / "dag.json"
    p.write_text(json.dumps(raw))
    return DAG.load(p)


# ----- V3-001: planner mentions BDD convention -----


def test_planner_renders_evaluation_contract(tmp_path):
    """Plan 33: the generalized planner.md emits the four-stage audit
    rubric verbatim. BDD specifics are in the standards docs the contract
    loads — not hard-coded into the planner prompt."""
    dag = _make_dag_with_behaviors(tmp_path, ["B-0001"])
    cfg = _cfg(tmp_path)
    contract = build_for(dag.nodes["R-001"], cfg)
    out = planner_prompt(cfg, dag, dag.nodes["R-001"], contract)
    # Contract stage cards land in the prompt
    assert "local_ci stage" in out
    assert "rubric stage" in out
    assert "standards stage" in out
    assert "behavior stage" in out
    # The Plan 33 stage-typed schema fields appear
    assert "rubric_targets" in out
    assert "standards_referenced" in out
    assert "behavior_evidence_advanced" in out


def test_planner_json_example_includes_interfaces_field(tmp_path):
    dag = _make_dag_with_behaviors(tmp_path, ["B-0001"])
    cfg = _cfg(tmp_path)
    contract = build_for(dag.nodes["R-001"], cfg)
    out = planner_prompt(cfg, dag, dag.nodes["R-001"], contract)
    assert '"interfaces"' in out


# ----- V3-002: checker prompts mention validator commands -----


def test_checker_prompt_mentions_targeted_bdd_validators(tmp_path):
    dag = _make_dag_with_behaviors(tmp_path, ["B-0001"])
    cfg = _cfg(tmp_path)
    out = checker_prompt(
        cfg, dag.nodes["R-001"], "the plan", ci_result="fail", ci_failure_excerpt="bdd tags failed"
    )
    assert "just check-bdd-tags" in out
    assert "scripts/roadmap_check.py" in out


def test_subtask_checker_renders_plan_33_targeted_block(tmp_path):
    """Plan 33 PR-B: subtask-checker prompt rewrite is rubric-first. The
    prompt no longer hardcodes BDD validator commands — they live in the
    subtask's `acceptance` and the contract's standards docs."""
    dag = _make_dag_with_behaviors(tmp_path, ["B-0001"])
    cfg = _cfg(tmp_path)
    contract = build_for(dag.nodes["R-001"], cfg)
    sub = Subtask(
        id="S-09-bdd-B-0001",
        title="Behavior proof for B-0001",
        depends_on=("S-05-api",),
        files_to_touch=("tests/bdd/features/B-0001-sign-in.feature",),
        boundary="One feature file. No production-code edits.",
        acceptance=("just check-bdd-tags passes against this feature",),
        interfaces=("web", "api"),
        notes="follow behavior-proof.md",
    )
    parsed = ParsedSelfAudit(gate_local_ci_rc=0, gate_local_ci_cmd="just check")
    out = subtask_checker_prompt(
        cfg,
        dag.nodes["R-001"],
        sub,
        contract,
        self_audit=parsed,
        diff_text="diff --git a/x b/x",
        witness_results={},
    )
    # Plan 33 PR-B: the verification matrix is present.
    assert "Verification matrix" in out
    # Subtask id flows through.
    assert "S-09-bdd-B-0001" in out


# ----- V3-003: Subtask.interfaces -----


def test_subtask_interfaces_optional_default_empty():
    s = Subtask(
        id="S-01-domain",
        title="Domain types",
        depends_on=(),
        files_to_touch=("crates/foo/src/account.rs",),
        boundary="Domain crate only.",
        acceptance=("cargo check passes",),
    )
    assert s.interfaces == ()


def test_subtask_interfaces_accepts_list_from_json():
    """Planner emits JSON; tuple field must coerce list."""
    s = Subtask(
        id="S-09-bdd-B-0001",
        title="BDD",
        depends_on=(),
        files_to_touch=("tests/bdd/features/B-0001-sign-in.feature",),
        boundary="One feature file.",
        acceptance=("just check-bdd-tags passes",),
        interfaces=["web", "api", "cli"],
    )
    assert s.interfaces == ("web", "api", "cli")


def test_planner_json_round_trips_with_interfaces():
    raw = """```json
{
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
      "notes": ""
    },
    {
      "id": "S-09-bdd-B-0001",
      "title": "BDD B-0001",
      "depends_on": ["S-01-domain"],
      "files_to_touch": ["tests/bdd/features/B-0001-sign-in.feature"],
      "boundary": "feature only",
      "acceptance": ["just check-bdd-tags passes"],
      "interfaces": ["web", "api"],
      "notes": ""
    }
  ],
  "final_acceptance": ["just ci passes"]
}
```"""
    plan = parse_planner_output(raw, expected_node_id="R-001")
    assert plan.subtasks[0].interfaces == ()
    assert plan.subtasks[1].interfaces == ("web", "api")


def test_subtask_doer_renders_interfaces_block_when_set(tmp_path):
    """Plan 17 compressed the BDD slice rules to a single paragraph that
    cites `just check-bdd-tags` as the authoritative validator and points
    at `docs/architecture/subsystems/behavior-proof.md` for the convention
    reference. The full mechanical rule list lives in the convention doc,
    not in the prompt — the prompt only needs to surface the interfaces
    plus the validator + ref doc."""
    dag = _make_dag_with_behaviors(tmp_path, ["B-0001"])
    cfg = _cfg(tmp_path)
    sub = Subtask(
        id="S-09-bdd-B-0001",
        title="BDD",
        depends_on=("S-05",),
        files_to_touch=("tests/bdd/features/B-0001-sign-in.feature",),
        boundary="feature only",
        acceptance=("just check-bdd-tags passes",),
        interfaces=("web", "api"),
        notes="follow docs/architecture/subsystems/behavior-proof.md",
    )
    contract = build_for(dag.nodes["R-001"], cfg)
    out = subtask_doer_prompt(cfg, dag.nodes["R-001"], sub, contract)
    # BDD slice section is rendered with the interfaces in the header.
    assert "BDD slice" in out
    assert "web" in out and "api" in out
    # The acceptance criterion (which cites the validator) flows through.
    assert "just check-bdd-tags" in out
    # The notes-cited convention doc flows through too.
    assert "behavior-proof.md" in out


def test_subtask_doer_omits_interfaces_block_when_empty(tmp_path):
    dag = _make_dag_with_behaviors(tmp_path, ["B-0001"])
    cfg = _cfg(tmp_path)
    sub = Subtask(
        id="S-01-domain",
        title="Domain",
        depends_on=(),
        files_to_touch=("crates/foo/src/account.rs",),
        boundary="Domain crate only.",
        acceptance=("cargo check passes",),
        interfaces=(),
        notes="",
    )
    contract = build_for(dag.nodes["R-001"], cfg)
    out = subtask_doer_prompt(cfg, dag.nodes["R-001"], sub, contract)
    # The BDD slice block should NOT appear for non-BDD subtasks
    assert "BDD slice" not in out


# ----- doer (whole-spec) BDD callout -----


def test_whole_spec_doer_mentions_bdd_for_node_with_behaviors(tmp_path):
    dag = _make_dag_with_behaviors(tmp_path, ["B-0001"])
    cfg = _cfg(tmp_path)
    out = doer_prompt(cfg, dag.nodes["R-001"], "plan body")
    # The doer prompt has a generic BDD section (since it doesn't have subtask context)
    assert "BDD" in out
    assert "@B-XXXX" in out
    assert "just check-bdd-tags" in out
