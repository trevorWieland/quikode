"""V3-001/002/003 — BDD convention awareness in prompts + Subtask.interfaces field.

Asserts the prompts mention F-0002's mechanical contract so the doer/checker
agents have the rules in their context. The actual convergence test is
running quikode against R-0001 — these tests just gate the prompt content
so we don't accidentally drift away from the convention.
"""

from __future__ import annotations

import json
from pathlib import Path

from quikode.config import Config
from quikode.dag import DAG
from quikode.prompts import (
    checker_prompt,
    doer_prompt,
    planner_prompt,
    subtask_checker_prompt,
    subtask_doer_prompt,
)
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


def test_planner_includes_bdd_convention_section(tmp_path):
    dag = _make_dag_with_behaviors(tmp_path, ["B-0001"])
    cfg = _cfg(tmp_path)
    out = planner_prompt(cfg, dag, dag.nodes["R-001"])
    # Section header
    assert "BDD" in out
    assert "F-0002" in out
    # Mechanical rules cited verbatim — these are the things that fail builds
    assert "@B-XXXX" in out
    assert "@positive" in out
    assert "@falsification" in out
    assert "@web" in out and "@api" in out and "@mcp" in out and "@cli" in out and "@tui" in out
    assert "Scenario Outline" in out
    assert "strict" in out.lower()
    # Convention reference
    assert "behavior-proof.md" in out
    # Validator commands
    assert "just check-bdd-tags" in out
    assert "scripts/roadmap_check.py" in out


def test_planner_instructs_one_bdd_subtask_per_behavior(tmp_path):
    dag = _make_dag_with_behaviors(tmp_path, ["B-0001", "B-0002"])
    cfg = _cfg(tmp_path)
    out = planner_prompt(cfg, dag, dag.nodes["R-001"])
    # Instruction text about per-behavior subtasks
    assert "one BDD subtask per behavior" in out or "one feature file per behavior" in out
    assert "S-NN-bdd-B-XXXX" in out or "S-NN-bdd" in out


def test_planner_json_example_includes_interfaces_field(tmp_path):
    dag = _make_dag_with_behaviors(tmp_path, ["B-0001"])
    cfg = _cfg(tmp_path)
    out = planner_prompt(cfg, dag, dag.nodes["R-001"])
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


def test_subtask_checker_mentions_bdd_validator_for_bdd_subtasks(tmp_path):
    dag = _make_dag_with_behaviors(tmp_path, ["B-0001"])
    cfg = _cfg(tmp_path)
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
    out = subtask_checker_prompt(cfg, dag.nodes["R-001"], sub)
    assert "just check-bdd-tags" in out


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
        notes="",
    )
    out = subtask_doer_prompt(cfg, dag.nodes["R-001"], sub)
    # Header + the interfaces themselves
    assert "Interfaces this subtask must cover" in out
    assert "web" in out and "api" in out
    # The mechanical rules block kicks in
    assert "@positive" in out
    assert "@falsification" in out
    assert "Scenario Outline" in out
    assert "just check-bdd-tags" in out
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
    out = subtask_doer_prompt(cfg, dag.nodes["R-001"], sub)
    # The Interfaces block + BDD rules should NOT appear for non-BDD subtasks
    assert "Interfaces this subtask must cover" not in out


# ----- doer (whole-spec) BDD callout -----


def test_whole_spec_doer_mentions_bdd_for_node_with_behaviors(tmp_path):
    dag = _make_dag_with_behaviors(tmp_path, ["B-0001"])
    cfg = _cfg(tmp_path)
    out = doer_prompt(cfg, dag.nodes["R-001"], "plan body")
    # The doer prompt has a generic BDD section (since it doesn't have subtask context)
    assert "BDD" in out
    assert "@B-XXXX" in out
    assert "just check-bdd-tags" in out
