"""Plan 33 PR-B: SELF_AUDIT parser + short-circuit unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from quikode.config import Config
from quikode.dag import DAG, Node
from quikode.evaluation_contract import build_for
from quikode.self_audit import (
    ParsedSelfAudit,
    ShortCircuit,
    parse_self_audit,
    short_circuit_decision,
)
from quikode.subtask_schema import RubricTarget, Subtask

# ---------- fixtures ----------


def _cfg(tmp_path: Path) -> Config:
    return Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        pre_pr_rubric_categories=["security", "code_quality"],
        pre_pr_rubric_min_score=7,
    )


def _node() -> Node:
    return Node(
        id="R-001",
        kind="behavior",
        milestone="M-1",
        title="Sign in flow",
        scope="Implement sign-in.",
        depends_on=(),
        completes_behaviors=("B-100",),
        supports_behaviors=(),
        boundary_with_neighbors="",
        expected_evidence=(),
        playbook=(),
        rationale="",
        risks=(),
        raw={},
    )


def _subtask(
    *,
    rubric_targets: tuple[RubricTarget, ...] = (),
    behavior: tuple[str, ...] = (),
) -> Subtask:
    return Subtask(
        id="S-01",
        title="example",
        depends_on=(),
        files_to_touch=(),
        boundary="",
        acceptance=("does the thing",),
        notes="",
        rubric_targets=rubric_targets,
        standards_referenced=(),
        behavior_evidence_advanced=behavior,
    )


# ---------- well-formed parsing ----------


def test_parse_well_formed_block() -> None:
    text = """
Some preamble narrative.

SELF_AUDIT:
  gate_local_ci: rc=0 (cmd: just check)
  gate_rubric:
    code_quality: predicted_score=8  rationale: filter goes through service  evidence: web/list.tsx:42
    security: predicted_score=9  rationale: input sanitized at boundary  evidence: api/orgs.py:18
  gate_standards:
    docs/web.md§list-views: aligned (cite paragraph 3)
  gate_behavior:
    B-001: witnessed_by=npm test:e2e  output_excerpt=PASS (1.2s)
  diff_reconcile:
    web/list.tsx: in_lane
    api/orgs.py: in_lane
"""
    parsed = parse_self_audit(text)
    assert parsed.parse_errors == ()
    assert parsed.gate_local_ci_rc == 0
    assert parsed.gate_local_ci_cmd == "just check"
    assert "code_quality" in parsed.gate_rubric
    assert parsed.gate_rubric["code_quality"].predicted_score == 8
    assert "filter goes through service" in parsed.gate_rubric["code_quality"].rationale
    assert "web/list.tsx:42" in parsed.gate_rubric["code_quality"].evidence
    assert parsed.gate_rubric["security"].predicted_score == 9
    assert "docs/web.md§list-views" in parsed.gate_standards
    assert parsed.gate_standards["docs/web.md§list-views"].aligned is True
    assert "B-001" in parsed.gate_behavior
    assert parsed.gate_behavior["B-001"].witnessed_by == "npm test:e2e"
    assert parsed.diff_reconcile["web/list.tsx"] == "in_lane"


def test_parse_tolerates_4_space_indent() -> None:
    text = """SELF_AUDIT:
    gate_local_ci: rc=0 (cmd: bash test.sh)
    gate_rubric:
        code_quality: predicted_score=7  rationale: ok  evidence: a.py:1
    gate_standards:
    gate_behavior:
    diff_reconcile:
        a.py: in_lane
"""
    parsed = parse_self_audit(text)
    assert parsed.parse_errors == ()
    assert parsed.gate_local_ci_rc == 0
    assert parsed.gate_rubric["code_quality"].predicted_score == 7


def test_parse_tolerates_trailing_whitespace() -> None:
    text = (
        "SELF_AUDIT:   \n"
        "  gate_local_ci: rc=0 (cmd: just)   \n"
        "  gate_rubric:   \n"
        "    code_quality: predicted_score=8  rationale: ok  evidence: a.py:1   \n"
        "  gate_standards:\n"
        "  gate_behavior:\n"
        "  diff_reconcile:\n"
        "    a.py: in_lane   \n"
    )
    parsed = parse_self_audit(text)
    assert parsed.parse_errors == ()
    assert parsed.gate_local_ci_rc == 0


def test_parse_empty_section_with_no_rows_is_accepted() -> None:
    text = """SELF_AUDIT:
  gate_local_ci: rc=0 (cmd: just)
  gate_rubric:
  gate_standards:
  gate_behavior:
  diff_reconcile:
"""
    parsed = parse_self_audit(text)
    assert parsed.parse_errors == ()
    assert parsed.gate_rubric == {}
    assert parsed.gate_behavior == {}
    assert parsed.gate_standards == {}


def test_parse_with_placeholder_text_does_not_crash() -> None:
    """The doer prompt's literal `<...>` placeholders may slip into output."""
    text = """SELF_AUDIT:
  gate_local_ci: rc=0 (cmd: <command>)
  gate_rubric:
    cat: predicted_score=8  rationale: <rationale>  evidence: <file:line>
  gate_standards:
  gate_behavior:
  diff_reconcile:
    <file>: in_lane
"""
    parsed = parse_self_audit(text)
    # Should parse cleanly even with `<...>` placeholders — they're just text.
    assert parsed.parse_errors == ()
    assert parsed.gate_rubric["cat"].predicted_score == 8


# ---------- malformed parsing ----------


def test_parse_missing_anchor() -> None:
    parsed = parse_self_audit("just narrative, no SELF_AUDIT block")
    assert parsed.parse_errors
    assert "anchor line not found" in parsed.parse_errors[0]


def test_parse_missing_required_section() -> None:
    text = """SELF_AUDIT:
  gate_local_ci: rc=0 (cmd: just)
  gate_rubric:
  gate_behavior:
  diff_reconcile:
"""  # missing gate_standards
    parsed = parse_self_audit(text)
    assert parsed.parse_errors
    assert any("gate_standards" in err for err in parsed.parse_errors)


def test_parse_local_ci_missing_rc() -> None:
    text = """SELF_AUDIT:
  gate_local_ci: it ran fine
  gate_rubric:
  gate_standards:
  gate_behavior:
  diff_reconcile:
"""
    parsed = parse_self_audit(text)
    assert parsed.parse_errors
    assert any("gate_local_ci" in err for err in parsed.parse_errors)


def test_parse_rubric_row_missing_predicted_score() -> None:
    text = """SELF_AUDIT:
  gate_local_ci: rc=0 (cmd: just)
  gate_rubric:
    code_quality: rationale: missing the score  evidence: a.py:1
  gate_standards:
  gate_behavior:
  diff_reconcile:
"""
    parsed = parse_self_audit(text)
    assert parsed.parse_errors
    assert any("predicted_score" in err for err in parsed.parse_errors)


# ---------- short-circuit semantics ----------


def test_short_circuit_proceed_on_clean_audit(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    contract = build_for(_node(), cfg)
    parsed = parse_self_audit(
        """SELF_AUDIT:
  gate_local_ci: rc=0 (cmd: just)
  gate_rubric:
    code_quality: predicted_score=8  rationale: ok  evidence: a.py:1
  gate_standards:
  gate_behavior:
  diff_reconcile:
    a.py: in_lane
"""
    )
    assert parsed.parse_errors == ()
    decision = short_circuit_decision(
        parsed,
        contract=contract,
        subtask=_subtask(rubric_targets=(RubricTarget(category="code_quality", predicted_score=8),)),
        rubric_min_score=7,
    )
    assert decision.decision is ShortCircuit.PROCEED


def test_short_circuit_local_ci_rc_nonzero(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    contract = build_for(_node(), cfg)
    parsed = parse_self_audit(
        """SELF_AUDIT:
  gate_local_ci: rc=2 (cmd: just check)
  gate_rubric:
    code_quality: predicted_score=8  rationale: ok  evidence: a.py:1
  gate_standards:
  gate_behavior:
  diff_reconcile:
"""
    )
    decision = short_circuit_decision(parsed, contract=contract, subtask=_subtask(), rubric_min_score=7)
    assert decision.decision is ShortCircuit.FAIL_FAST
    assert decision.failure_layer == "local_ci"


def test_short_circuit_rubric_below_threshold(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    contract = build_for(_node(), cfg)
    parsed = parse_self_audit(
        """SELF_AUDIT:
  gate_local_ci: rc=0 (cmd: just)
  gate_rubric:
    code_quality: predicted_score=5  rationale: weak  evidence: a.py:1
  gate_standards:
  gate_behavior:
  diff_reconcile:
"""
    )
    decision = short_circuit_decision(parsed, contract=contract, subtask=_subtask(), rubric_min_score=7)
    assert decision.decision is ShortCircuit.FAIL_FAST
    assert decision.failure_layer == "rubric"
    assert "code_quality=5" in decision.reason


@pytest.mark.parametrize("token", ["RISK", "STUB", "TODO", "FIXME", "XXX", "risk", "stub"])
def test_short_circuit_risk_token_in_rationale(tmp_path: Path, token: str) -> None:
    cfg = _cfg(tmp_path)
    contract = build_for(_node(), cfg)
    parsed = parse_self_audit(
        f"""SELF_AUDIT:
  gate_local_ci: rc=0 (cmd: just)
  gate_rubric:
    code_quality: predicted_score=8  rationale: {token} this is incomplete  evidence: a.py:1
  gate_standards:
  gate_behavior:
  diff_reconcile:
"""
    )
    decision = short_circuit_decision(parsed, contract=contract, subtask=_subtask(), rubric_min_score=7)
    assert decision.decision is ShortCircuit.FAIL_FAST
    assert decision.failure_layer == "self_audit_mismatch"


def test_short_circuit_risk_token_in_behavior_excerpt(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    contract = build_for(_node(), cfg)
    parsed = parse_self_audit(
        """SELF_AUDIT:
  gate_local_ci: rc=0 (cmd: just)
  gate_rubric:
    code_quality: predicted_score=8  rationale: ok  evidence: a.py:1
  gate_standards:
  gate_behavior:
    B-001: witnessed_by=npm test  output_excerpt=TODO add real assertion
  diff_reconcile:
"""
    )
    decision = short_circuit_decision(parsed, contract=contract, subtask=_subtask(), rubric_min_score=7)
    assert decision.decision is ShortCircuit.FAIL_FAST
    assert decision.failure_layer == "self_audit_mismatch"


def test_short_circuit_word_boundary_does_not_match_substring(tmp_path: Path) -> None:
    """`STUB` inside `STUBBORN` is not a STUB token (word boundary)."""
    cfg = _cfg(tmp_path)
    contract = build_for(_node(), cfg)
    parsed = parse_self_audit(
        """SELF_AUDIT:
  gate_local_ci: rc=0 (cmd: just)
  gate_rubric:
    code_quality: predicted_score=8  rationale: stubbornly testing  evidence: a.py:1
  gate_standards:
  gate_behavior:
  diff_reconcile:
"""
    )
    decision = short_circuit_decision(parsed, contract=contract, subtask=_subtask(), rubric_min_score=7)
    assert decision.decision is ShortCircuit.PROCEED


def test_short_circuit_unparseable_rc_is_self_audit_mismatch(tmp_path: Path) -> None:
    """When the parser couldn't recover an int rc, the decision is mismatch
    (not local_ci) — the doer needs to re-emit, not fix CI."""
    cfg = _cfg(tmp_path)
    contract = build_for(_node(), cfg)
    parsed = ParsedSelfAudit(gate_local_ci_rc=None, gate_local_ci_cmd="")
    decision = short_circuit_decision(parsed, contract=contract, subtask=_subtask(), rubric_min_score=7)
    assert decision.decision is ShortCircuit.FAIL_FAST
    assert decision.failure_layer == "self_audit_mismatch"


# ---------- re-prompt-loop edge cases ----------


def test_parsed_self_audit_partial_recovery_still_returns_object() -> None:
    """Even a heavily-malformed input returns a ParsedSelfAudit (with
    parse_errors). The worker reads parse_errors first and can re-prompt."""
    parsed = parse_self_audit("SELF_AUDIT:\n  gate_local_ci: garbage\n")
    assert isinstance(parsed, ParsedSelfAudit)
    assert parsed.parse_errors


def test_dag_loader_compatibility(tmp_path: Path) -> None:
    """Sanity: the parser is independent of DAG / Config plumbing."""
    raw = (
        '{"schema":"test","milestones":[{"id":"M","title":"x","goal":"y","status":"planned"}],'
        '"nodes":[{"id":"R-001","kind":"behavior","milestone":"M","title":"x",'
        '"scope":"x","depends_on":[],"completes_behaviors":[],"supports_behaviors":[],'
        '"expected_evidence":[]}]}'
    )
    p = tmp_path / "dag.json"
    p.write_text(raw)
    dag = DAG.load(p)
    assert "R-001" in dag.nodes
