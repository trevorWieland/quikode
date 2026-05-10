"""Prompt rendering tests."""

from __future__ import annotations

import json
from pathlib import Path

from quikode.config import Config
from quikode.dag import DAG
from quikode.evaluation_contract import build_for
from quikode.prompts import (
    checker_prompt,
    conflict_resolver_prompt,
    doer_prompt,
    planner_prompt,
    subtask_checker_prompt,
    subtask_doer_prompt,
    subtask_triage_prompt,
    triage_prompt,
)
from quikode.subtask_schema import Subtask


def _cfg(prompts_dir: Path) -> Config:
    """Build a Config that uses bundled prompts (we don't override prompts_dir)."""
    # By default Config reads from <root>/prompts; the package falls back to bundled.
    return Config(
        repo_path=prompts_dir,
        dag_path=prompts_dir,
        prompts_dir=prompts_dir / "prompts",  # missing dir → falls back to bundled
    )


def _make_dag(tmp_path: Path) -> DAG:
    raw = {
        "schema": "test",
        "milestones": [{"id": "M-1", "title": "Auth", "goal": "x", "status": "planned"}],
        "nodes": [
            {
                "id": "R-001",
                "kind": "behavior",
                "milestone": "M-1",
                "title": "Sign in flow",
                "scope": "Implement sign-in across web and api.",
                "boundary_with_neighbors": "Touches auth/, not billing/.",
                "depends_on": [],
                "completes_behaviors": ["B-100"],
                "supports_behaviors": [],
                "expected_evidence": [
                    {
                        "kind": "test",
                        "behavior_id": "B-100",
                        "interfaces": ["web", "api"],
                        "witnesses": ["positive", "falsification"],
                        "description": "GET /auth round-trips a session.",
                    }
                ],
                "playbook": ["api: POST /sessions, assert 201", "web: navigate to /signin, assert redirect"],
                "rationale": "First auth slice.",
                "risks": ["password storage policy"],
            }
        ],
    }
    p = tmp_path / "dag.json"
    p.write_text(json.dumps(raw))
    return DAG.load(p)


def test_planner_renders_with_evidence_and_playbook(tmp_path):
    dag = _make_dag(tmp_path)
    cfg = _cfg(tmp_path)
    contract = build_for(dag.nodes["R-001"], cfg)
    out = planner_prompt(cfg, dag, dag.nodes["R-001"], contract)
    # Plan 33 planner emits JSON-structured plan; the prompt includes the
    # spec details + the four-stage audit gauntlet rubric verbatim.
    assert "R-001" in out
    assert "Sign in flow" in out
    assert "B-100" in out
    assert "GET /auth round-trips" in out
    assert "api: POST /sessions" in out
    assert "subtasks" in out  # JSON shape mentioned
    assert "final_acceptance" in out
    assert "```json" in out  # output format example
    assert "depends_on" in out


def test_doer_includes_plan(tmp_path):
    dag = _make_dag(tmp_path)
    cfg = _cfg(tmp_path)
    plan = "1. do this. 2. do that."
    out = doer_prompt(cfg, dag.nodes["R-001"], plan)
    assert plan in out
    assert "R-001" in out
    # No triage notes when none given
    assert "Triage feedback from prior attempt" not in out


def test_doer_renders_triage_notes_when_present(tmp_path):
    dag = _make_dag(tmp_path)
    cfg = _cfg(tmp_path)
    out = doer_prompt(cfg, dag.nodes["R-001"], "plan", triage_notes="Push failed: bad credentials")
    assert "Triage feedback from prior attempt" in out
    assert "Push failed: bad credentials" in out


def test_checker_includes_ci_result_and_excerpt(tmp_path):
    dag = _make_dag(tmp_path)
    cfg = _cfg(tmp_path)
    out = checker_prompt(
        cfg, dag.nodes["R-001"], "the plan", ci_result="fail", ci_failure_excerpt="ERROR: tests failed"
    )
    assert "fail" in out
    assert "ERROR: tests failed" in out
    assert "VERDICT: PASS | FAIL" in out


def test_triage_renders_review_comments(tmp_path):
    dag = _make_dag(tmp_path)
    cfg = _cfg(tmp_path)
    review = [{"author": "alice", "path": "src/a.rs", "line": 10, "body": "rename foo"}]
    out = triage_prompt(
        cfg,
        dag.nodes["R-001"],
        "the plan",
        phase="review",
        retry_count=1,
        retry_budget=3,
        review_comments=review,
    )
    assert "alice" in out
    assert "rename foo" in out
    assert "ROOT_CAUSE:" in out


# ----- v2 Phase 0: subtask prompts -----


def _subtask():
    return Subtask(
        id="S-01-domain",
        title="Add account domain types",
        depends_on=(),
        files_to_touch=("crates/foo/src/account.rs", "crates/foo/src/lib.rs"),
        boundary="Domain crate only.",
        acceptance=("cargo check passes", "Account struct exported"),
        notes="",
    )


def test_subtask_doer_prompt_includes_acceptance_and_files(tmp_path):
    dag = _make_dag(tmp_path)
    cfg = _cfg(tmp_path)
    contract = build_for(dag.nodes["R-001"], cfg)
    out = subtask_doer_prompt(cfg, dag.nodes["R-001"], _subtask(), contract)
    assert "S-01-domain" in out
    assert "Add account domain types" in out
    assert "crates/foo/src/account.rs" in out
    assert "cargo check passes" in out
    assert "Domain crate only." in out
    # Plan 47: doer has no JSON envelope; the diff is the deliverable.
    normalized = " ".join(out.lower().split())
    assert "the diff is the evidence" in normalized
    assert "no output schema" in normalized or "no json envelope" in normalized


def test_subtask_doer_renders_triage_as_context(tmp_path):
    """Plan 17 + Plan 33 + Plan 47: triage feedback is the canonical
    carry-forward across attempts."""
    dag = _make_dag(tmp_path)
    cfg = _cfg(tmp_path)
    contract = build_for(dag.nodes["R-001"], cfg)
    out = subtask_doer_prompt(
        cfg,
        dag.nodes["R-001"],
        _subtask(),
        contract,
        triage_notes="ROOT_CAUSE: missing field.",
    )
    assert "context, not a fix recipe" in out
    assert "missing field" in out


def test_subtask_checker_prompt_format(tmp_path):
    dag = _make_dag(tmp_path)
    cfg = _cfg(tmp_path)
    contract = build_for(dag.nodes["R-001"], cfg)
    out = subtask_checker_prompt(
        cfg,
        dag.nodes["R-001"],
        _subtask(),
        contract,
        diff_text="diff --git a/x b/x",
        witness_results={},
    )
    assert "S-01-domain" in out
    assert "Plan 14 preserved" in out
    # Plan 47: no doer self-report block in the checker prompt.
    assert "Doer's self-report" not in out
    assert "INFORMATIONAL" not in out


def test_conflict_resolver_prompt_renders_diffs(tmp_path):
    dag = _make_dag(tmp_path)
    cfg = _cfg(tmp_path)
    out = conflict_resolver_prompt(
        cfg,
        dag.nodes["R-001"],
        task_diff_excerpt="diff --git a/foo.rs b/foo.rs\n+pub fn bar() {}",
        main_log_excerpt="abc1234 feat: rename baz to qux",
        main_diff_excerpt="diff --git a/lib.rs b/lib.rs\n-fn baz()\n+fn qux()",
        conflicted_files=[
            {"path": "src/foo.rs", "content": "<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> main"},
        ],
    )
    assert "R-001" in out
    assert "rename baz to qux" in out
    assert "src/foo.rs" in out
    assert "<<<<<<<" in out
    assert "GIVE_UP" in out


def test_subtask_triage_prompt_senior_engineer_framing(tmp_path):
    """Plan 47: triage on the JsonAgent layer — senior-engineer-
    tutoring-junior framing preserved (Plan 14). The prompt surfaces
    the targeted contract slice, the checker's verdict, and the
    unified diff. The doer self-report block is gone."""
    dag = _make_dag(tmp_path)
    cfg = _cfg(tmp_path)
    contract = build_for(dag.nodes["R-001"], cfg)
    out = subtask_triage_prompt(
        cfg,
        dag.nodes["R-001"],
        _subtask(),
        contract,
        checker_verdict="VERDICT: FAIL\n[FAIL] foo",
        diff_text="diff --git a/x b/x",
    )
    assert "S-01-domain" in out
    assert "VERDICT: FAIL" in out
    assert "senior engineer" in out.lower()
    # Plan 38 + 47: closed enum on failure_layer drops self_audit_mismatch
    # and adds parse_failure + architecture.
    assert "parse_failure" in out
    assert "architecture" in out
    assert "self_audit_mismatch" not in out
    # Plan 47: no doer envelope / self-report block.
    assert "doer's self-report" not in out.lower()
