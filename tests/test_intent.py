"""Phase B intent-gap parsing + prompt rendering."""

from __future__ import annotations

import json

from quikode.config import Config
from quikode.dag import DAG
from quikode.prompts import intent_reviewer_prompt
from quikode.worker import _parse_intent_verdict


def test_parse_no_drift():
    text = """VERDICT: NO_DRIFT

AFFECTED_AREAS: none

EXPLANATION: Main only changed README; no overlap with this task."""
    out = _parse_intent_verdict(text)
    assert out.verdict.value == "NO_DRIFT"
    assert out.affected_areas == "none"
    assert "README" in out.explanation


def test_parse_minor_drift():
    text = """VERDICT: MINOR_DRIFT

AFFECTED_AREAS: src/lib.rs

EXPLANATION: Main renamed a function this task calls."""
    out = _parse_intent_verdict(text)
    assert out.verdict.value == "MINOR_DRIFT"
    assert out.affected_areas == "src/lib.rs"


def test_parse_intent_conflict():
    text = """VERDICT: INTENT_CONFLICT

AFFECTED_AREAS: foo/bar.py, foo/qux.py

EXPLANATION: Main added a new instance of the pattern this task was supposed to apply universally."""
    out = _parse_intent_verdict(text)
    assert out.verdict.value == "INTENT_CONFLICT"
    assert "pattern" in out.explanation


def test_parse_unknown_defaults_safe():
    """Bad output → NO_DRIFT (the safe-no-op default), not BLOCKED."""
    out = _parse_intent_verdict("agent emitted gibberish")
    assert out.verdict.value == "NO_DRIFT"


def _make_dag(tmp_path):
    raw = {
        "schema": "test",
        "milestones": [{"id": "M-1", "title": "x", "goal": "x", "status": "planned"}],
        "nodes": [
            {
                "id": "R-001",
                "kind": "behavior",
                "milestone": "M-1",
                "title": "test node",
                "scope": "do something",
                "depends_on": [],
                "completes_behaviors": ["B-100"],
                "supports_behaviors": [],
                "boundary_with_neighbors": "",
                "expected_evidence": [],
                "playbook": [],
                "rationale": "",
                "risks": [],
            }
        ],
    }
    p = tmp_path / "dag.json"
    p.write_text(json.dumps(raw))
    return DAG.load(p)


def test_intent_reviewer_prompt_renders(tmp_path):
    dag = _make_dag(tmp_path)
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path, prompts_dir=tmp_path / "missing")
    out = intent_reviewer_prompt(
        cfg,
        dag.nodes["R-001"],
        task_diff_excerpt="diff --git a/x.rs b/x.rs\n+pub fn new_thing() {}",
        main_log_excerpt="abc1234 feat: rename foo to bar",
        main_diff_excerpt="diff --git a/x.rs b/x.rs\n-fn foo()\n+fn bar()",
    )
    assert "R-001" in out
    assert "VERDICT: NO_DRIFT | MINOR_DRIFT | INTENT_CONFLICT" in out
    assert "rename foo to bar" in out
    assert "new_thing" in out
