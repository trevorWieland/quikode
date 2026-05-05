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
from quikode.config import Config

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


# ----- collect_standards_text -----


def test_collect_standards_text_reads_globbed_docs(tmp_path: Path):
    (tmp_path / "docs" / "standards").mkdir(parents=True)
    (tmp_path / "docs" / "standards" / "rust.md").write_text("# Rust standards\n\nUse `?` not `unwrap`.\n")
    (tmp_path / "docs" / "standards" / "tests.md").write_text("# Test standards\n\nNo `assert_eq!(x, x)`.\n")
    (tmp_path / "AGENTS.md").write_text("# Agents\n\nFollow the spec.\n")
    (tmp_path / "README.md").write_text("Top-level README — should NOT be picked up by default globs.\n")

    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        pre_pr_standards_profile_globs=[
            "docs/standards/**/*.md",
            "AGENTS.md",
        ],
    )
    text = pre_pr_audit.collect_standards_text(cfg)
    assert "Rust standards" in text
    assert "Test standards" in text
    assert "Agents" in text
    assert "Top-level README" not in text


def test_collect_standards_text_missing_dir_returns_empty(tmp_path: Path):
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        pre_pr_standards_profile_globs=["docs/standards/**/*.md"],
    )
    text = pre_pr_audit.collect_standards_text(cfg)
    assert text == ""
