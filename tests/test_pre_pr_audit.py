"""Unit tests for `quikode.pre_pr_audit`.

Plan 38 PR-B.3: the three audit stages (rubric / standards / behavior)
run through the JsonAgent layer. Plan 35 PR-B added a fourth audit
stage: `architecture`, between standards and behavior; tests for it
live in `tests/test_pre_pr_architecture_audit.py`.

The legacy heuristic JSON-extract path (`json.loads(cand)` over a
regex-extracted candidate) is gone; tests that exercised the parser
are replaced with stub-`make_agent` cases that hand the audit either
a validated pydantic instance or a `parse_errors` non-empty
`JsonAgentResult` to verify the structural-failure mode.

Tests cover:
- Rubric / standards / behavior happy path → outcome reflects the
  validated pydantic instance.
- Rubric / standards / behavior gating semantics (any category below
  threshold, any severity ≥ medium, any unverified behavior).
- `parse_errors` non-empty → synthetic FAIL outcome labeled
  `parse_failure` (plan-12/14 invariant: structural failure, NOT a
  fabricated content finding).
- Agent rc != 0 → synthetic FAIL outcome labeled `infra`.
- merge_failed_stage_reports + collect_finding_ids preserved across
  the rewrite (legacy dict shape).
- collect_standards_text reads contract source_text.
- Source-level regression: heuristic regex / `json.loads(cand)` cannot
  re-appear.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from quikode import pre_pr_audit
from quikode.agent_schemas import (
    BehaviorCompletenessGap,
    BehaviorVerification,
    PrePRBehaviorAuditOutput,
    PrePRRubricAuditOutput,
    PrePRStandardsAuditOutput,
    RubricCategoryScore,
    RubricGap,
    StandardsFinding,
)
from quikode.agents.json_protocol import JsonAgentResult
from quikode.architecture_docs import ArchitectureCorpus
from quikode.config import Config
from quikode.evaluation_contract import (
    ArchitectureStageRubric,
    EvaluationContract,
    StageRubric,
    StandardsStageRubric,
)
from quikode.standards_profiles import StandardsDoc, StandardsProfile

# ----- helpers -----


def _build_cfg(tmp_path: Path) -> Config:
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        prompts_dir=tmp_path / "missing-prompts",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _stub_handle() -> MagicMock:
    h = MagicMock()
    h.container_name = "qk-stub"
    return h


def _make_contract(
    *,
    profiles: tuple[StandardsProfile, ...] = (),
    arch_corpus: ArchitectureCorpus | None = None,
    standards_source_text: str = "",
) -> EvaluationContract:
    if arch_corpus is None:
        arch_corpus = ArchitectureCorpus(root=Path("/tmp"), docs=())
    return EvaluationContract(
        task_id="R-T",
        local_ci=StageRubric(name="local_ci", one_line="", threshold="", grading_template="", source_text=""),
        rubric=StageRubric(name="rubric", one_line="", threshold="", grading_template="", source_text=""),
        standards=StandardsStageRubric(profiles=profiles, source_text=standards_source_text),
        architecture=ArchitectureStageRubric(corpus=arch_corpus),
        behavior=StageRubric(name="behavior", one_line="", threshold="", grading_template="", source_text=""),
    )


def _make_profile(
    name: str,
    *,
    docs: tuple[StandardsDoc, ...] = (),
) -> StandardsProfile:
    return StandardsProfile(name=name, root=Path("/tmp/profiles") / name, docs=docs)


def _make_standards_doc(
    *,
    profile: str = "rust-cargo",
    repo_relative: str = "profiles/rust-cargo/rust/error-handling.md",
    sections: tuple[str, ...] = ("Rules",),
    body: str = "## Rules\n\nNo unwrap.\n",
    applies_to: tuple[str, ...] = (),
    name: str = "error-handling",
    category: str = "rust",
) -> StandardsDoc:
    return StandardsDoc(
        profile=profile,
        category=category,
        name=name,
        path=Path("/tmp") / repo_relative,
        repo_relative=repo_relative,
        importance="high",
        applies_to=applies_to,
        applies_to_languages=("rust",),
        applies_to_domains=(),
        body=body,
        sections=sections,
    )


def _make_json_result(
    *,
    structured=None,
    rc: int = 0,
    parse_errors: tuple[str, ...] = (),
    raw_text: str | None = None,
) -> JsonAgentResult:
    return JsonAgentResult(
        structured=structured,
        rc=rc,
        transient=False,
        duration_s=0.1,
        parse_errors=parse_errors,
        raw_text=raw_text,
    )


# ----- Rubric audit -----


def test_run_rubric_audit_happy_path_all_pass(tmp_path):
    cfg = _build_cfg(tmp_path)
    audit = PrePRRubricAuditOutput(
        categories=[
            RubricCategoryScore(name="security", score=8, rationale="ok"),
            RubricCategoryScore(name="performance", score=9, rationale="great"),
        ],
        overall_assessment="solid",
    )
    fake_agent = MagicMock()
    fake_agent.invoke.return_value = _make_json_result(structured=audit, raw_text="{...}")
    with (
        patch("quikode.pre_pr_audit.make_agent", return_value=fake_agent),
        patch("quikode.pre_pr_audit.prompts_mod.render", return_value="prompt"),
    ):
        outcome = pre_pr_audit.run_rubric_audit(
            cfg=cfg,
            handle=_stub_handle(),
            diff_excerpt="diff",
            plan_text="plan",
        )
    assert outcome.passed
    assert outcome.name == "rubric"
    assert "security=8" in outcome.summary
    assert "performance=9" in outcome.summary


def test_run_rubric_audit_failing_category_below_threshold(tmp_path):
    cfg = _build_cfg(tmp_path)
    audit = PrePRRubricAuditOutput(
        categories=[
            RubricCategoryScore(
                name="security",
                score=4,
                rationale="missing input validation",
                gaps_to_reach_ten=[
                    RubricGap(
                        id="validate-org-name",
                        description="Org name length unchecked",
                        concrete_fix="Add len<=64 guard in create_organization_atomic",
                        files=["src/org.rs"],
                    )
                ],
            ),
            RubricCategoryScore(
                name="performance",
                score=8,
                rationale="passes threshold but can be tighter",
                gaps_to_reach_ten=[
                    RubricGap(
                        id="batch-project-list",
                        description="Project list still does one query per row",
                        concrete_fix="Batch-load owner data before rendering the list",
                        files=["src/projects.rs"],
                    )
                ],
            ),
        ]
    )
    fake_agent = MagicMock()
    fake_agent.invoke.return_value = _make_json_result(structured=audit, raw_text="{...}")
    with (
        patch("quikode.pre_pr_audit.make_agent", return_value=fake_agent),
        patch("quikode.pre_pr_audit.prompts_mod.render", return_value="prompt"),
    ):
        outcome = pre_pr_audit.run_rubric_audit(
            cfg=cfg,
            handle=_stub_handle(),
            diff_excerpt="diff",
            plan_text="plan",
        )
    assert not outcome.passed
    assert "rubric failed" in outcome.summary
    # Legacy dict shape preserved (collect_finding_ids reads `id` /
    # `category` / `gaps_to_reach_ten`).
    assert len(outcome.findings) == 2
    f = outcome.findings[0]
    assert f["kind"] == "rubric_below_threshold"
    assert f["category"] == "security"
    assert f["score"] == 4
    assert f["id"] == "category-security"
    assert f["gaps_to_reach_ten"][0]["id"] == "validate-org-name"
    improvement = outcome.findings[1]
    assert improvement["kind"] == "rubric_reach_ten_gap"
    assert improvement["category"] == "performance"
    assert improvement["score"] == 8
    assert improvement["id"] == "reach-ten-performance"
    assert improvement["gaps_to_reach_ten"][0]["id"] == "batch-project-list"


def test_run_rubric_audit_all_pass_does_not_forward_reach_ten_gaps(tmp_path):
    cfg = _build_cfg(tmp_path)
    audit = PrePRRubricAuditOutput(
        categories=[
            RubricCategoryScore(
                name="security",
                score=8,
                rationale="above threshold",
                gaps_to_reach_ten=[
                    RubricGap(
                        id="add-rate-limit",
                        description="Endpoint lacks a rate-limit guard",
                        concrete_fix="Add per-user request throttling",
                        files=["src/api.rs"],
                    )
                ],
            ),
            RubricCategoryScore(name="performance", score=10, rationale="complete"),
        ]
    )
    fake_agent = MagicMock()
    fake_agent.invoke.return_value = _make_json_result(structured=audit, raw_text="{...}")
    with (
        patch("quikode.pre_pr_audit.make_agent", return_value=fake_agent),
        patch("quikode.pre_pr_audit.prompts_mod.render", return_value="prompt"),
    ):
        outcome = pre_pr_audit.run_rubric_audit(
            cfg=cfg,
            handle=_stub_handle(),
            diff_excerpt="diff",
            plan_text="plan",
        )
    assert outcome.passed
    assert outcome.findings == []


def test_run_rubric_audit_parse_failure_returns_synthetic_fail(tmp_path):
    cfg = _build_cfg(tmp_path)
    fake_agent = MagicMock()
    fake_agent.invoke.return_value = _make_json_result(
        structured=None,
        parse_errors=("categories.0.score: Input should be a valid integer",),
        raw_text="garbage prose",
    )
    with (
        patch("quikode.pre_pr_audit.make_agent", return_value=fake_agent),
        patch("quikode.pre_pr_audit.prompts_mod.render", return_value="prompt"),
    ):
        outcome = pre_pr_audit.run_rubric_audit(
            cfg=cfg,
            handle=_stub_handle(),
            diff_excerpt="diff",
            plan_text="plan",
        )
    assert not outcome.passed
    assert "parse_failure" in outcome.summary
    # Plan-12/14 invariant: parse_failure is a STRUCTURAL signal, not a
    # fabricated content finding masquerading as a real grade.
    assert len(outcome.findings) == 1
    assert outcome.findings[0]["kind"] == "parse_failure"
    assert "Input should be a valid integer" in outcome.findings[0]["rationale"]


def test_run_rubric_audit_agent_rc_nonzero_returns_infra_fail(tmp_path):
    cfg = _build_cfg(tmp_path)
    fake_agent = MagicMock()
    fake_agent.invoke.return_value = _make_json_result(structured=None, rc=124)
    with (
        patch("quikode.pre_pr_audit.make_agent", return_value=fake_agent),
        patch("quikode.pre_pr_audit.prompts_mod.render", return_value="prompt"),
    ):
        outcome = pre_pr_audit.run_rubric_audit(
            cfg=cfg,
            handle=_stub_handle(),
            diff_excerpt="diff",
            plan_text="plan",
        )
    assert not outcome.passed
    assert "rc=124" in outcome.summary
    assert outcome.findings[0]["kind"] == "infra"


# ----- Standards audit -----


def test_run_standards_audit_no_profiles_returns_config_error(tmp_path):
    cfg = _build_cfg(tmp_path)
    contract = _make_contract(profiles=(), standards_source_text="")
    outcome = pre_pr_audit.run_standards_audit(
        cfg=cfg,
        handle=_stub_handle(),
        contract=contract,
        diff_excerpt="diff",
        cited_refs=[],
    )
    assert not outcome.passed
    assert "no standards profile docs loaded" in outcome.summary
    assert outcome.findings[0]["kind"] == "config_error"


def test_run_standards_audit_happy_path_low_severity(tmp_path):
    cfg = _build_cfg(tmp_path)
    profile = _make_profile("rust-cargo", docs=(_make_standards_doc(),))
    contract = _make_contract(profiles=(profile,), standards_source_text="profile catalog")
    audit = PrePRStandardsAuditOutput(
        findings=[
            StandardsFinding(
                id="x",
                file="a.py",
                line=12,
                severity="low",
                standards_doc_ref="profiles/rust-cargo/rust/error-handling.md§Rules",
                description="minor naming nit",
            )
        ]
    )
    fake_agent = MagicMock()
    fake_agent.invoke.return_value = _make_json_result(structured=audit, raw_text="{...}")
    with (
        patch("quikode.pre_pr_audit.make_agent", return_value=fake_agent),
        patch("quikode.pre_pr_audit.prompts_mod.render", return_value="prompt"),
    ):
        outcome = pre_pr_audit.run_standards_audit(
            cfg=cfg,
            handle=_stub_handle(),
            contract=contract,
            diff_excerpt="diff",
            cited_refs=[("profiles/rust-cargo/rust/error-handling.md", "Rules")],
        )
    assert outcome.passed
    assert "low-severity note(s)" in outcome.summary
    assert outcome.findings[0]["severity"] == "low"
    # Plan 35 PR-B: the rename to `profile_doc_ref` carries through.
    assert outcome.findings[0]["profile_doc_ref"].startswith("profiles/rust-cargo")


def test_run_standards_audit_serious_finding_fails(tmp_path):
    cfg = _build_cfg(tmp_path)
    profile = _make_profile("rust-cargo", docs=(_make_standards_doc(),))
    contract = _make_contract(profiles=(profile,), standards_source_text="profile catalog")
    audit = PrePRStandardsAuditOutput(
        findings=[
            StandardsFinding(
                id="rename-account-orgs",
                file="src/x.rs",
                line=42,
                severity="high",
                standards_doc_ref="profiles/rust-cargo/rust/error-handling.md§Rules",
                description="crosses module boundary",
                concrete_fix="move to xtask",
            )
        ]
    )
    fake_agent = MagicMock()
    fake_agent.invoke.return_value = _make_json_result(structured=audit, raw_text="{...}")
    with (
        patch("quikode.pre_pr_audit.make_agent", return_value=fake_agent),
        patch("quikode.pre_pr_audit.prompts_mod.render", return_value="prompt"),
    ):
        outcome = pre_pr_audit.run_standards_audit(
            cfg=cfg,
            handle=_stub_handle(),
            contract=contract,
            diff_excerpt="diff",
            cited_refs=[],
        )
    assert not outcome.passed
    assert "1 medium+ severity" in outcome.summary
    # Legacy dict shape preserved.
    assert outcome.findings[0]["id"] == "rename-account-orgs"
    assert outcome.findings[0]["severity"] == "high"


def test_run_standards_audit_parse_failure_returns_synthetic_fail(tmp_path):
    cfg = _build_cfg(tmp_path)
    profile = _make_profile("rust-cargo", docs=(_make_standards_doc(),))
    contract = _make_contract(profiles=(profile,), standards_source_text="profile catalog")
    fake_agent = MagicMock()
    fake_agent.invoke.return_value = _make_json_result(
        structured=None,
        parse_errors=("findings.0.severity: Input should be 'low', 'medium', 'high' or 'critical'",),
    )
    with (
        patch("quikode.pre_pr_audit.make_agent", return_value=fake_agent),
        patch("quikode.pre_pr_audit.prompts_mod.render", return_value="prompt"),
    ):
        outcome = pre_pr_audit.run_standards_audit(
            cfg=cfg,
            handle=_stub_handle(),
            contract=contract,
            diff_excerpt="diff",
            cited_refs=[],
        )
    assert not outcome.passed
    assert "parse_failure" in outcome.summary
    assert outcome.findings[0]["kind"] == "parse_failure"


def test_run_standards_audit_does_not_synthesize_uncited_profile_findings(tmp_path):
    """The standards gate reports only auditor findings. Missing
    `standards_referenced` coverage is planner quality, not a synthetic
    standards violation.
    """
    cfg = _build_cfg(tmp_path)
    doc = _make_standards_doc(
        repo_relative="profiles/rust-cargo/rust/no-any.md",
        applies_to=("**/*.ts",),
        sections=("Rules",),
        name="no-any",
    )
    profile = _make_profile("rust-cargo", docs=(doc,))
    contract = _make_contract(profiles=(profile,), standards_source_text="profile catalog")
    audit = PrePRStandardsAuditOutput(findings=[])
    fake_agent = MagicMock()
    fake_agent.invoke.return_value = _make_json_result(structured=audit, raw_text="{}")
    diff = "diff --git a/src/app.ts b/src/app.ts\n@@ -1 +1 @@\n-x\n+y\n"
    with (
        patch("quikode.pre_pr_audit.make_agent", return_value=fake_agent),
        patch("quikode.pre_pr_audit.prompts_mod.render", return_value="prompt"),
    ):
        outcome = pre_pr_audit.run_standards_audit(
            cfg=cfg,
            handle=_stub_handle(),
            contract=contract,
            diff_excerpt=diff,
            cited_refs=[],  # nothing cited
        )
    assert outcome.passed
    assert outcome.findings == []


# ----- Behavior audit -----


def test_run_behavior_audit_no_evidence_passes_skipped(tmp_path):
    cfg = _build_cfg(tmp_path)
    outcome = pre_pr_audit.run_behavior_audit(
        cfg=cfg,
        handle=_stub_handle(),
        expected_evidence=[],
        diff_excerpt="diff",
        plan_text="plan",
    )
    assert outcome.passed
    assert "gate skipped" in outcome.summary


def test_run_behavior_audit_all_verified_passes(tmp_path):
    cfg = _build_cfg(tmp_path)
    audit = PrePRBehaviorAuditOutput(
        behaviors=[
            BehaviorVerification(
                behavior_id="B-1",
                verified=True,
                evidence_seen="test passes",
            )
        ]
    )
    fake_agent = MagicMock()
    fake_agent.invoke.return_value = _make_json_result(structured=audit, raw_text="{...}")
    with (
        patch("quikode.pre_pr_audit.make_agent", return_value=fake_agent),
        patch("quikode.pre_pr_audit.prompts_mod.render", return_value="prompt"),
    ):
        outcome = pre_pr_audit.run_behavior_audit(
            cfg=cfg,
            handle=_stub_handle(),
            expected_evidence=[{"behavior_id": "B-1"}],
            diff_excerpt="diff",
            plan_text="plan",
        )
    assert outcome.passed
    assert "1 verified" in outcome.summary


def test_run_behavior_audit_unverified_behavior_fails(tmp_path):
    cfg = _build_cfg(tmp_path)
    audit = PrePRBehaviorAuditOutput(
        behaviors=[
            BehaviorVerification(
                behavior_id="B-1",
                verified=True,
                evidence_seen="test passes",
            ),
            BehaviorVerification(
                behavior_id="B-2",
                verified=False,
                gap_explanation="endpoint not implemented",
                concrete_fix="add /v1/foo",
                completeness_gaps=[
                    BehaviorCompletenessGap(
                        id="falsification-on-dup",
                        description="dup-key path untested",
                    )
                ],
            ),
        ]
    )
    fake_agent = MagicMock()
    fake_agent.invoke.return_value = _make_json_result(structured=audit, raw_text="{...}")
    with (
        patch("quikode.pre_pr_audit.make_agent", return_value=fake_agent),
        patch("quikode.pre_pr_audit.prompts_mod.render", return_value="prompt"),
    ):
        outcome = pre_pr_audit.run_behavior_audit(
            cfg=cfg,
            handle=_stub_handle(),
            expected_evidence=[{"behavior_id": "B-1"}, {"behavior_id": "B-2"}],
            diff_excerpt="diff",
            plan_text="plan",
        )
    assert not outcome.passed
    assert "1 unverified" in outcome.summary
    assert outcome.findings[0]["behavior_id"] == "B-2"
    # Bridge preserves completeness_gaps so collect_finding_ids picks them up.
    assert outcome.findings[0]["completeness_gaps"][0]["id"] == "falsification-on-dup"


def test_run_behavior_audit_parse_failure_returns_synthetic_fail(tmp_path):
    cfg = _build_cfg(tmp_path)
    fake_agent = MagicMock()
    fake_agent.invoke.return_value = _make_json_result(
        structured=None,
        parse_errors=("behaviors.0.verified: Input should be a valid boolean",),
    )
    with (
        patch("quikode.pre_pr_audit.make_agent", return_value=fake_agent),
        patch("quikode.pre_pr_audit.prompts_mod.render", return_value="prompt"),
    ):
        outcome = pre_pr_audit.run_behavior_audit(
            cfg=cfg,
            handle=_stub_handle(),
            expected_evidence=[{"behavior_id": "B-1"}],
            diff_excerpt="diff",
            plan_text="plan",
        )
    assert not outcome.passed
    assert "parse_failure" in outcome.summary
    assert outcome.findings[0]["kind"] == "parse_failure"


# ----- Outcome construction (preserved across rewrite) -----


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


# ----- Source-level guards (Plan 38 PR-B.3 regression) -----


def test_pre_pr_audit_source_has_no_heuristic_json_extract():
    """Plan 38 PR-B.3 regression: pre_pr_audit.py must not re-introduce
    the heuristic JSON-extract path. The JsonAgent layer owns parsing for
    all three audit stages."""
    src = Path("quikode/pre_pr_audit.py").read_text()
    # No regex-based JSON extraction.
    assert "_JSON_OBJ_RE" not in src, "regex extraction must be gone"
    assert "_JSON_OBJECT_RE" not in src, "regex extraction must be gone"
    assert "import re" not in src, "re module no longer needed"
    # No cand-based json.loads heuristic.
    assert "json.loads(cand)" not in src, "json.loads heuristic must be gone"
    # The legacy parse helpers are deleted.
    assert "_parse_rubric_envelope" not in src, "rubric parser helper must be gone"
    assert "_parse_findings_envelope" not in src, "standards parser helper must be gone"
    assert "_parse_behavior_envelope" not in src, "behavior parser helper must be gone"
