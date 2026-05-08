"""Unit tests for `quikode.pre_pr_audit`.

Each stage is independently testable: the audit module composes
prompts + parses agent JSON envelopes, both of which are deterministic
under stub-agent harnesses. Tests cover:

- Stage parser handling of well-formed JSON, prose-wrapped JSON, garbage
- Stage outcome shape on success / failure / parse error
- merge_failed_stage_reports produces a single bundle the fixup planner
  can ingest
- collect_standards_text reads cfg-globbed docs from the repo
"""

from __future__ import annotations

from pathlib import Path

from quikode import pre_pr_audit
from quikode.architecture_docs import ArchitectureCorpus
from quikode.config import Config
from quikode.evaluation_contract import (
    ArchitectureStageRubric,
    EvaluationContract,
    StageRubric,
    StandardsStageRubric,
)

# ----- Rubric envelope parsing -----


def test_parse_rubric_envelope_well_formed():
    raw = '{"categories":[{"name":"security","score":8,"rationale":"ok"},{"name":"performance","score":6,"rationale":"slow"}]}'
    parsed = pre_pr_audit._parse_rubric_envelope(raw)
    assert parsed is not None
    assert len(parsed["categories"]) == 2


def test_parse_rubric_envelope_prose_wrapped():
    raw = 'Sure, here is the assessment.\n{"categories":[{"name":"security","score":9,"rationale":"good"}]}\nDone.'
    parsed = pre_pr_audit._parse_rubric_envelope(raw)
    assert parsed is not None
    assert parsed["categories"][0]["score"] == 9


def test_parse_rubric_envelope_garbage_returns_none():
    assert pre_pr_audit._parse_rubric_envelope("") is None
    assert pre_pr_audit._parse_rubric_envelope("just prose, no json") is None
    assert pre_pr_audit._parse_rubric_envelope('{"foo":"bar"}') is None  # missing categories


# ----- Standards envelope parsing -----


def test_parse_findings_envelope_serious_finding():
    raw = '{"findings":[{"file":"x.py","line":42,"severity":"high","description":"crosses module boundary","suggested_fix":"move to xtask"}]}'
    parsed = pre_pr_audit._parse_findings_envelope(raw)
    assert parsed is not None
    assert parsed["findings"][0]["severity"] == "high"


def test_parse_findings_envelope_empty_findings():
    raw = '{"findings":[]}'
    parsed = pre_pr_audit._parse_findings_envelope(raw)
    assert parsed is not None
    assert parsed["findings"] == []


# ----- Behavior envelope parsing -----


def test_parse_behavior_envelope_mixed_verified():
    raw = (
        '{"behaviors":[{"behavior_id":"B-1","verified":true,"evidence_seen":"test passes"},'
        '{"behavior_id":"B-2","verified":false,"gap_explanation":"endpoint not implemented"}]}'
    )
    parsed = pre_pr_audit._parse_behavior_envelope(raw)
    assert parsed is not None
    bs = parsed["behaviors"]
    assert sum(1 for b in bs if b["verified"]) == 1
    assert sum(1 for b in bs if not b["verified"]) == 1


# ----- Outcome construction -----


def test_pipeline_cycle_passed_when_all_pass():
    stages = [
        pre_pr_audit.StageOutcome("local_ci", True, "ok"),
        pre_pr_audit.StageOutcome("rubric", True, "ok"),
        pre_pr_audit.StageOutcome("standards", True, "ok"),
        pre_pr_audit.StageOutcome("behavior", True, "ok"),
    ]
    result = pre_pr_audit.PipelineCycleResult(cycle=1, stages=stages)
    assert result.passed
    assert result.failed_stages == []


def test_pipeline_cycle_failed_collects_failed_stages():
    stages = [
        pre_pr_audit.StageOutcome("local_ci", True, "ok"),
        pre_pr_audit.StageOutcome(
            "rubric",
            False,
            "score 5 in security",
            findings=[{"category": "security", "score": 5}],
        ),
        pre_pr_audit.StageOutcome("standards", True, "ok"),
        pre_pr_audit.StageOutcome(
            "behavior",
            False,
            "B-1 unverified",
            findings=[{"behavior_id": "B-1", "verified": False}],
        ),
    ]
    result = pre_pr_audit.PipelineCycleResult(cycle=1, stages=stages)
    assert not result.passed
    failed_names = [s.name for s in result.failed_stages]
    assert failed_names == ["rubric", "behavior"]


# ----- merge_failed_stage_reports -----


def test_merge_failed_stage_reports_combines_findings():
    stages = [
        pre_pr_audit.StageOutcome(
            "rubric",
            False,
            "security 5 (rationale: missing input validation)",
            findings=[{"category": "security", "score": 5, "rationale": "missing input validation"}],
        ),
        pre_pr_audit.StageOutcome(
            "behavior",
            False,
            "B-2 unverified",
            findings=[{"behavior_id": "B-2", "verified": False, "gap_explanation": "no test exists"}],
        ),
    ]
    bundle = pre_pr_audit.merge_failed_stage_reports(stages)
    assert "rubric" in bundle
    assert "behavior" in bundle
    assert "missing input validation" in bundle
    assert "no test exists" in bundle
    # JSON-formatted findings present.
    assert "```json" in bundle


def test_merge_failed_stage_reports_empty():
    assert pre_pr_audit.merge_failed_stage_reports([]) == ""


# ----- collect_finding_ids (completeness check support) -----


def test_collect_finding_ids_namespaces_per_stage():
    rubric = pre_pr_audit.StageOutcome(
        name="rubric",
        passed=False,
        summary="",
        findings=[
            {
                "id": "category-security",
                "gaps_to_reach_ten": [
                    {"id": "validate-org-name"},
                    {"id": "redact-pii-in-logs"},
                ],
            },
        ],
    )
    standards = pre_pr_audit.StageOutcome(
        name="standards",
        passed=False,
        summary="",
        findings=[{"id": "rename-account-orgs", "severity": "high"}],
    )
    behavior = pre_pr_audit.StageOutcome(
        name="behavior",
        passed=False,
        summary="",
        findings=[
            {
                "behavior_id": "B-1",
                "completeness_gaps": [{"id": "falsification-on-dup"}],
            }
        ],
    )
    ids = pre_pr_audit.collect_finding_ids([rubric, standards, behavior])
    assert "rubric:validate-org-name" in ids
    assert "rubric:redact-pii-in-logs" in ids
    assert "rubric:category-security" in ids
    assert "standards:rename-account-orgs" in ids
    assert "behavior:falsification-on-dup" in ids
    assert "behavior:B-1" in ids


def test_collect_finding_ids_synthesizes_when_no_id():
    stage = pre_pr_audit.StageOutcome(
        name="local_ci",
        passed=False,
        summary="",
        findings=[
            {"kind": "compile_error", "file": "src/foo.rs", "message": "boom"},
            {"kind": "compile_error", "file": None},
        ],
    )
    ids = pre_pr_audit.collect_finding_ids([stage])
    # Both findings get a stable id even without an explicit `id` field.
    assert all(fid.startswith("local_ci:") for fid in ids)
    assert len(ids) == 2


def test_collect_finding_ids_dedupes_across_stages():
    s = pre_pr_audit.StageOutcome(
        name="rubric",
        passed=False,
        summary="",
        findings=[
            {"id": "x", "gaps_to_reach_ten": [{"id": "x"}]},
        ],
    )
    ids = pre_pr_audit.collect_finding_ids([s])
    assert ids.count("rubric:x") == 1


# ----- collect_standards_text -----


def test_collect_standards_text_returns_contract_source_text(tmp_path: Path):
    """Plan 35 PR-A: collect_standards_text now reads the contract's
    pre-rendered `standards.source_text`. The legacy on-disk-glob fallback
    was retired alongside `pre_pr_standards_profile_globs`.
    """
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json")
    contract = EvaluationContract(
        task_id="R-T",
        local_ci=StageRubric(name="local_ci", one_line="", threshold="", grading_template="", source_text=""),
        rubric=StageRubric(name="rubric", one_line="", threshold="", grading_template="", source_text=""),
        standards=StandardsStageRubric(source_text="rendered profile catalog"),
        architecture=ArchitectureStageRubric(corpus=ArchitectureCorpus(root=tmp_path, docs=())),
        behavior=StageRubric(name="behavior", one_line="", threshold="", grading_template="", source_text=""),
    )
    text = pre_pr_audit.collect_standards_text(cfg, contract=contract)
    assert text == "rendered profile catalog"


def test_collect_standards_text_no_contract_returns_empty(tmp_path: Path):
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json")
    text = pre_pr_audit.collect_standards_text(cfg)
    assert text == ""
