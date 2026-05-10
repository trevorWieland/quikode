"""Plan 35 PR-B: end-to-end smoke that the 5-stage gauntlet runs the
stages in order and routes architecture findings into the architecture
namespace (NOT duplicated as standards findings).

These tests exercise the module-level audit functions and the worker's
`_execute_audit_stages` orchestrator. They verify:

1. The five stages run in declared order (`local_ci, rubric, standards,
   architecture, behavior`).
2. Each stage's findings flow into `merge_failed_stage_reports` +
   `collect_finding_ids` under the correct namespace prefix
   (`architecture:<id>` for the new stage).
3. An architecture-failing diff doesn't get its findings duplicated
   into the standards stage's output (the buckets are separate, the
   audits don't deputize for each other).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quikode import pre_pr_audit, runtime_shutdown
from quikode.agent_schemas import (
    ArchitectureFinding,
    PrePRArchitectureAuditOutput,
    PrePRBehaviorAuditOutput,
    PrePRRubricAuditOutput,
    PrePRStandardsAuditOutput,
    RubricCategoryScore,
    StandardsFinding,
)
from quikode.agents.json_protocol import JsonAgentResult
from quikode.architecture_docs import ArchitectureCorpus, ArchitectureDoc
from quikode.config import Config
from quikode.evaluation_contract import (
    ArchitectureStageRubric,
    EvaluationContract,
    StageRubric,
    StandardsStageRubric,
)
from quikode.standards_profiles import StandardsDoc, StandardsProfile
from quikode.state import State
from quikode.workers import task_worker as task_worker_mod
from quikode.workers.pre_pr import PrePrWorkerMixin


def _stub_handle() -> MagicMock:
    h = MagicMock()
    h.container_name = "qk-stub"
    return h


def _build_cfg(tmp_path: Path) -> Config:
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        prompts_dir=tmp_path / "missing-prompts",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
        local_ci_command="just ci",
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _make_contract() -> EvaluationContract:
    profile = StandardsProfile(
        name="rust-cargo",
        root=Path("/tmp/profiles/rust-cargo"),
        docs=(
            StandardsDoc(
                profile="rust-cargo",
                category="rust",
                name="error-handling",
                path=Path("/tmp/profiles/rust-cargo/rust/error-handling.md"),
                repo_relative="profiles/rust-cargo/rust/error-handling.md",
                importance="high",
                applies_to=(),
                applies_to_languages=("rust",),
                applies_to_domains=(),
                body="## Rules\n\nNo unwrap.",
                sections=("Rules",),
            ),
        ),
    )
    arch_corpus = ArchitectureCorpus(
        root=Path("/tmp/docs/architecture"),
        docs=(
            ArchitectureDoc(
                path=Path("/tmp/docs/architecture/subsystems/identity-policy.md"),
                repo_relative="docs/architecture/subsystems/identity-policy.md",
                title="Identity Policy",
                sections=("Permissions",),
                body="# Identity Policy\n\n## Permissions\n",
            ),
        ),
    )
    return EvaluationContract(
        task_id="R-FIVE",
        local_ci=StageRubric(
            name="local_ci", one_line="", threshold="rc=0", grading_template="", source_text=""
        ),
        rubric=StageRubric(name="rubric", one_line="", threshold="", grading_template="", source_text=""),
        standards=StandardsStageRubric(profiles=(profile,), source_text="catalog"),
        architecture=ArchitectureStageRubric(corpus=arch_corpus, source_text="arch toc"),
        behavior=StageRubric(name="behavior", one_line="", threshold="", grading_template="", source_text=""),
    )


def _make_json_result(*, structured) -> JsonAgentResult:
    return JsonAgentResult(
        structured=structured,
        rc=0,
        transient=False,
        duration_s=0.1,
        parse_errors=(),
        raw_text="{...}",
    )


def test_five_stage_pipeline_runs_in_order_and_namespaces_findings(tmp_path):
    """Drive the four agent-backed stages with stubbed make_agent results
    and verify (a) declared stage order, (b) finding-id namespacing per
    stage, (c) the architecture audit's findings DO appear under
    `architecture:` and DO NOT bleed into the `standards:` namespace.
    """
    cfg = _build_cfg(tmp_path)
    contract = _make_contract()

    rubric_audit = PrePRRubricAuditOutput(
        categories=[RubricCategoryScore(name="security", score=8, rationale="ok")],
    )
    standards_audit = PrePRStandardsAuditOutput(
        findings=[
            StandardsFinding(
                id="standards-finding-001",
                file="src/x.rs",
                line=1,
                severity="low",
                standards_doc_ref="profiles/rust-cargo/rust/error-handling.md§Rules",
                description="minor",
            )
        ],
    )
    architecture_audit = PrePRArchitectureAuditOutput(
        findings=[
            ArchitectureFinding(
                id="architecture-finding-001",
                file="src/identity.rs",
                line=1,
                severity="high",
                architecture_doc_ref="docs/architecture/subsystems/identity-policy.md§Permissions",
                description="cross-subsystem coupling",
                concrete_fix="route through AuthGuard",
            )
        ],
    )
    behavior_audit = PrePRBehaviorAuditOutput(behaviors=[])

    # Stub each role's agent independently — make_agent dispatches by role name.
    def fake_make_agent(role, _cfg):
        agent = MagicMock()
        if role == "pre_pr_rubric":
            agent.invoke.return_value = _make_json_result(structured=rubric_audit)
        elif role == "pre_pr_standards":
            agent.invoke.return_value = _make_json_result(structured=standards_audit)
        elif role == "pre_pr_architecture":
            agent.invoke.return_value = _make_json_result(structured=architecture_audit)
        elif role == "pre_pr_behavior":
            agent.invoke.return_value = _make_json_result(structured=behavior_audit)
        else:
            raise AssertionError(f"unexpected role: {role!r}")
        return agent

    diff = "diff --git a/src/x.rs b/src/x.rs\n@@ -1 +1 @@\n-a\n+b\n"

    with (
        patch("quikode.pre_pr_audit.make_agent", side_effect=fake_make_agent),
        patch("quikode.pre_pr_audit.prompts_mod.render", return_value="prompt"),
    ):
        # Run the four agent-backed stages individually, mimicking the
        # worker's dispatch order (local_ci is exec-based and skipped here).
        rubric_outcome = pre_pr_audit.run_rubric_audit(
            cfg=cfg, handle=_stub_handle(), diff_excerpt=diff, plan_text="plan"
        )
        standards_outcome = pre_pr_audit.run_standards_audit(
            cfg=cfg,
            handle=_stub_handle(),
            contract=contract,
            diff_excerpt=diff,
            cited_refs=[],
        )
        architecture_outcome = pre_pr_audit.run_architecture_audit(
            cfg=cfg,
            handle=_stub_handle(),
            contract=contract,
            diff_excerpt=diff,
            cited_refs=[],
        )
        behavior_outcome = pre_pr_audit.run_behavior_audit(
            cfg=cfg,
            handle=_stub_handle(),
            expected_evidence=[],
            diff_excerpt=diff,
            plan_text="plan",
        )

    # local_ci is run via the worker's dispatcher; for this audit-routing
    # smoke test we stand in a passed local_ci outcome.
    local_ci_outcome = pre_pr_audit.StageOutcome(name="local_ci", passed=True, summary="rc=0")

    stages = [
        local_ci_outcome,
        rubric_outcome,
        standards_outcome,
        architecture_outcome,
        behavior_outcome,
    ]
    assert [s.name for s in stages] == [
        "local_ci",
        "rubric",
        "standards",
        "architecture",
        "behavior",
    ]

    # Architecture failed (high severity). Stack the failed stages and
    # verify the namespace dispatch routes the finding under `architecture:`,
    # NOT under `standards:`.
    failed = [s for s in stages if not s.passed]
    assert architecture_outcome in failed
    assert standards_outcome.passed  # only low severity finding
    ids = pre_pr_audit.collect_finding_ids(failed)
    assert any(fid.startswith("architecture:") for fid in ids)
    # The architecture finding's id MUST NOT appear under the standards namespace.
    assert "standards:architecture-finding-001" not in ids
    assert "architecture:architecture-finding-001" in ids


def test_pipeline_cycle_result_with_five_stages_passed():
    """All five stages pass → cycle_result.passed is True."""
    stages = [
        pre_pr_audit.StageOutcome("local_ci", True, "ok"),
        pre_pr_audit.StageOutcome("rubric", True, "ok"),
        pre_pr_audit.StageOutcome("standards", True, "ok"),
        pre_pr_audit.StageOutcome("architecture", True, "ok"),
        pre_pr_audit.StageOutcome("behavior", True, "ok"),
    ]
    result = pre_pr_audit.PipelineCycleResult(cycle=1, stages=stages)
    assert result.passed
    assert result.failed_stages == []


def test_pipeline_cycle_result_with_architecture_failure():
    """Architecture failing while everything else passes → cycle fails
    and the failed-stage list contains exactly the architecture stage."""
    stages = [
        pre_pr_audit.StageOutcome("local_ci", True, "ok"),
        pre_pr_audit.StageOutcome("rubric", True, "ok"),
        pre_pr_audit.StageOutcome("standards", True, "ok"),
        pre_pr_audit.StageOutcome(
            "architecture",
            False,
            "high 1/0",
            findings=[
                {
                    "id": "arch-001",
                    "severity": "high",
                    "architecture_doc_ref": "docs/.../identity-policy.md§Permissions",
                }
            ],
        ),
        pre_pr_audit.StageOutcome("behavior", True, "ok"),
    ]
    result = pre_pr_audit.PipelineCycleResult(cycle=1, stages=stages)
    assert not result.passed
    failed_names = [s.name for s in result.failed_stages]
    assert failed_names == ["architecture"]


def test_collect_finding_ids_namespaces_architecture_stage():
    """Plan 35 PR-B: collect_finding_ids must namespace architecture
    findings under `architecture:<id>` so the fixup-coverage validator's
    namespace dispatch routes them into `architecture_referenced` (not
    `standards_referenced`).
    """
    architecture = pre_pr_audit.StageOutcome(
        name="architecture",
        passed=False,
        summary="",
        findings=[{"id": "arch-cross-subsystem-001", "severity": "high"}],
    )
    ids = pre_pr_audit.collect_finding_ids([architecture])
    assert "architecture:arch-cross-subsystem-001" in ids
    # Standards namespace does NOT carry the architecture finding.
    assert not any(fid.startswith("standards:") for fid in ids)


def test_execute_audit_stages_discards_stage_result_after_shutdown(tmp_path):
    """SIGTERM can interrupt an in-flight audit and leave the CLI with no
    schema output. Do not persist that partial result as a real failed stage.
    """
    cfg = _build_cfg(tmp_path)
    store = MagicMock()
    store.get.return_value = {"state": State.PRE_PR_AUDITING.value}

    class FakeWorker:
        _execute_audit_stages = PrePrWorkerMixin._execute_audit_stages
        _stage_outcome_from_summary = staticmethod(PrePrWorkerMixin._stage_outcome_from_summary)

        def __init__(self):
            self.cfg = cfg
            self.store = store
            self.node = MagicMock(id="R-SHUTDOWN", expected_evidence=[])
            self.log_path = tmp_path / "worker.log"

        @property
        def _h(self):
            return _stub_handle()

        def _evaluation_contract(self):
            return _make_contract()

        def _collect_cited_refs(self):
            return [], []

        def _merge_integration_subtasks_present(self):
            return False

        def _behavior_audit_expected_evidence(self, *, merge_node_mode: bool):
            return []

    def interrupted_behavior(**_kwargs):
        assert task_worker_mod.pre_pr_audit is pre_pr_audit
        runtime_shutdown.request_stop()
        return pre_pr_audit.StageOutcome(
            "behavior",
            False,
            "behavior audit response failed schema validation — failing closed (parse_failure)",
            findings=[{"kind": "parse_failure"}],
        )

    try:
        with (
            patch(
                "quikode.pre_pr_audit.run_local_ci_gate",
                return_value=pre_pr_audit.StageOutcome("local_ci", True, "ok"),
            ),
            patch(
                "quikode.pre_pr_audit.run_rubric_audit",
                return_value=pre_pr_audit.StageOutcome("rubric", True, "ok"),
            ),
            patch(
                "quikode.pre_pr_audit.run_standards_audit",
                return_value=pre_pr_audit.StageOutcome("standards", True, "ok"),
            ),
            patch(
                "quikode.pre_pr_audit.run_architecture_audit",
                return_value=pre_pr_audit.StageOutcome("architecture", True, "ok"),
            ),
            patch("quikode.pre_pr_audit.run_behavior_audit", side_effect=interrupted_behavior),
        ):
            with pytest.raises(runtime_shutdown.ShutdownRequested):
                FakeWorker()._execute_audit_stages(
                    cycle=1,
                    diff_excerpt="diff",
                    plan_text="plan",
                    merge_node_mode=False,
                )
    finally:
        runtime_shutdown.clear_stop()

    calls = [call.kwargs["stage_name"] for call in store.update_pre_pr_audit_stage.call_args_list]
    assert calls == ["local_ci", "rubric", "standards", "architecture"]
