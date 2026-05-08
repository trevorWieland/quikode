"""Plan 35 PR-B: tests for the architecture audit stage.

The architecture audit grades the diff against the project's documented
subsystem contracts (`cfg.architecture_docs_dir`). It runs through the
JsonAgent layer with `PrePRArchitectureAuditOutput` as the output schema
and is parallel in shape to `run_standards_audit` — same gating
(severity ≥ medium fails), same parse-failure shape, same
unreferenced-applicable detector (this time keyed off
`cfg.architecture_path_map`).

Tests cover:
- Happy path with low-severity findings → outcome passes.
- High-severity finding → outcome fails.
- Parse failure → synthetic FAIL with `kind="parse_failure"`.
- Empty corpus → config_error.
- `unreferenced-applicable-architecture` finding fires when the diff
  touches a path matched by `architecture_path_map` but no subtask
  cited the mapped doc.
- The detector is silent when the subtask DID cite the matching doc.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from quikode import pre_pr_audit
from quikode.agent_schemas import (
    ArchitectureFinding,
    PrePRArchitectureAuditOutput,
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


def _build_cfg(tmp_path: Path, *, architecture_path_map: dict[str, str] | None = None) -> Config:
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        prompts_dir=tmp_path / "missing-prompts",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
        architecture_path_map=architecture_path_map or {},
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _stub_handle() -> MagicMock:
    h = MagicMock()
    h.container_name = "qk-stub"
    return h


def _make_arch_doc(
    *,
    repo_relative: str = "docs/architecture/subsystems/identity-policy.md",
    sections: tuple[str, ...] = ("Permissions", "Error Taxonomy"),
    body: str = "# Identity Policy\n\n## Permissions\n\nScope-tagged.\n\n## Error Taxonomy\n\nAuthError chain.\n",
    title: str = "Identity Policy",
) -> ArchitectureDoc:
    return ArchitectureDoc(
        path=Path("/tmp") / repo_relative,
        repo_relative=repo_relative,
        title=title,
        sections=sections,
        body=body,
    )


def _make_contract(
    *,
    arch_corpus: ArchitectureCorpus | None = None,
    arch_source_text: str = "",
) -> EvaluationContract:
    if arch_corpus is None:
        arch_corpus = ArchitectureCorpus(root=Path("/tmp"), docs=())
    return EvaluationContract(
        task_id="R-T",
        local_ci=StageRubric(name="local_ci", one_line="", threshold="", grading_template="", source_text=""),
        rubric=StageRubric(name="rubric", one_line="", threshold="", grading_template="", source_text=""),
        standards=StandardsStageRubric(),
        architecture=ArchitectureStageRubric(corpus=arch_corpus, source_text=arch_source_text),
        behavior=StageRubric(name="behavior", one_line="", threshold="", grading_template="", source_text=""),
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


# ----- Empty corpus / config error -----


def test_run_architecture_audit_no_corpus_returns_config_error(tmp_path):
    cfg = _build_cfg(tmp_path)
    contract = _make_contract()  # corpus has no docs
    outcome = pre_pr_audit.run_architecture_audit(
        cfg=cfg,
        handle=_stub_handle(),
        contract=contract,
        diff_excerpt="diff",
        cited_refs=[],
    )
    assert not outcome.passed
    assert "no architecture docs loaded" in outcome.summary
    assert outcome.findings[0]["kind"] == "config_error"


# ----- Happy path (low-severity finding only) -----


def test_run_architecture_audit_happy_path_low_severity(tmp_path):
    cfg = _build_cfg(tmp_path)
    arch = ArchitectureCorpus(root=Path("/tmp"), docs=(_make_arch_doc(),))
    contract = _make_contract(arch_corpus=arch, arch_source_text="arch toc")
    audit = PrePRArchitectureAuditOutput(
        findings=[
            ArchitectureFinding(
                id="x",
                file="src/identity.rs",
                line=42,
                severity="low",
                architecture_doc_ref="docs/architecture/subsystems/identity-policy.md§Permissions",
                description="minor naming drift from subsystem doc",
            )
        ]
    )
    fake_agent = MagicMock()
    fake_agent.invoke.return_value = _make_json_result(structured=audit, raw_text="{...}")
    with (
        patch("quikode.pre_pr_audit.make_agent", return_value=fake_agent),
        patch("quikode.pre_pr_audit.prompts_mod.render", return_value="prompt"),
    ):
        outcome = pre_pr_audit.run_architecture_audit(
            cfg=cfg,
            handle=_stub_handle(),
            contract=contract,
            diff_excerpt="diff --git a/src/identity.rs b/src/identity.rs\n",
            cited_refs=[
                ("docs/architecture/subsystems/identity-policy.md", "Permissions"),
            ],
        )
    assert outcome.passed
    assert outcome.findings[0]["severity"] == "low"
    assert outcome.findings[0]["architecture_doc_ref"].startswith(
        "docs/architecture/subsystems/identity-policy.md"
    )


# ----- Severity gating -----


def test_run_architecture_audit_high_severity_fails(tmp_path):
    cfg = _build_cfg(tmp_path)
    arch = ArchitectureCorpus(root=Path("/tmp"), docs=(_make_arch_doc(),))
    contract = _make_contract(arch_corpus=arch, arch_source_text="arch toc")
    audit = PrePRArchitectureAuditOutput(
        findings=[
            ArchitectureFinding(
                id="cross-subsystem-coupling-001",
                file="src/x.rs",
                line=12,
                severity="high",
                architecture_doc_ref="docs/architecture/subsystems/identity-policy.md§Permissions",
                description="undocumented cross-subsystem call",
                concrete_fix="route through AuthGuard::check",
            )
        ]
    )
    fake_agent = MagicMock()
    fake_agent.invoke.return_value = _make_json_result(structured=audit, raw_text="{...}")
    with (
        patch("quikode.pre_pr_audit.make_agent", return_value=fake_agent),
        patch("quikode.pre_pr_audit.prompts_mod.render", return_value="prompt"),
    ):
        outcome = pre_pr_audit.run_architecture_audit(
            cfg=cfg,
            handle=_stub_handle(),
            contract=contract,
            diff_excerpt="diff --git a/src/x.rs b/src/x.rs\n",
            cited_refs=[],
        )
    assert not outcome.passed
    assert "1 medium+ severity" in outcome.summary
    assert outcome.findings[0]["id"] == "cross-subsystem-coupling-001"


# ----- Parse failure -----


def test_run_architecture_audit_parse_failure_returns_synthetic_fail(tmp_path):
    cfg = _build_cfg(tmp_path)
    arch = ArchitectureCorpus(root=Path("/tmp"), docs=(_make_arch_doc(),))
    contract = _make_contract(arch_corpus=arch, arch_source_text="arch toc")
    fake_agent = MagicMock()
    fake_agent.invoke.return_value = _make_json_result(
        structured=None,
        parse_errors=("findings.0.severity: Input should be 'low', 'medium', 'high' or 'critical'",),
    )
    with (
        patch("quikode.pre_pr_audit.make_agent", return_value=fake_agent),
        patch("quikode.pre_pr_audit.prompts_mod.render", return_value="prompt"),
    ):
        outcome = pre_pr_audit.run_architecture_audit(
            cfg=cfg,
            handle=_stub_handle(),
            contract=contract,
            diff_excerpt="diff",
            cited_refs=[],
        )
    assert not outcome.passed
    assert "parse_failure" in outcome.summary
    assert outcome.findings[0]["kind"] == "parse_failure"


# ----- Unreferenced-applicable detector -----


def test_run_architecture_audit_unreferenced_applicable_fires(tmp_path):
    """Plan 35 §2.10: when `architecture_path_map` says
    `crates/identity-policy/**` → `docs/.../identity-policy.md`, the
    diff touches that path, and no subtask cited the doc, a
    `unreferenced-applicable-architecture` finding fires (severity
    medium → fails the stage)."""
    cfg = _build_cfg(
        tmp_path,
        architecture_path_map={
            "crates/identity-policy/**": "docs/architecture/subsystems/identity-policy.md",
        },
    )
    arch = ArchitectureCorpus(root=Path("/tmp"), docs=(_make_arch_doc(),))
    contract = _make_contract(arch_corpus=arch, arch_source_text="arch toc")
    audit = PrePRArchitectureAuditOutput(findings=[])
    fake_agent = MagicMock()
    fake_agent.invoke.return_value = _make_json_result(structured=audit, raw_text="{}")
    diff = (
        "diff --git a/crates/identity-policy/src/lib.rs b/crates/identity-policy/src/lib.rs\n"
        "@@ -1 +1 @@\n-x\n+y\n"
    )
    with (
        patch("quikode.pre_pr_audit.make_agent", return_value=fake_agent),
        patch("quikode.pre_pr_audit.prompts_mod.render", return_value="prompt"),
    ):
        outcome = pre_pr_audit.run_architecture_audit(
            cfg=cfg,
            handle=_stub_handle(),
            contract=contract,
            diff_excerpt=diff,
            cited_refs=[],
        )
    assert not outcome.passed
    matches = [f for f in outcome.findings if f.get("kind") == "unreferenced_applicable_architecture"]
    assert matches, f"expected unreferenced_applicable_architecture finding; got {outcome.findings!r}"
    assert matches[0]["architecture_doc_ref"] == "docs/architecture/subsystems/identity-policy.md"


def test_run_architecture_audit_unreferenced_applicable_skips_when_cited(tmp_path):
    """When the subtask DOES cite the mapped doc, the unreferenced-applicable
    detector is silent."""
    cfg = _build_cfg(
        tmp_path,
        architecture_path_map={
            "crates/identity-policy/**": "docs/architecture/subsystems/identity-policy.md",
        },
    )
    arch = ArchitectureCorpus(root=Path("/tmp"), docs=(_make_arch_doc(),))
    contract = _make_contract(arch_corpus=arch, arch_source_text="arch toc")
    audit = PrePRArchitectureAuditOutput(findings=[])
    fake_agent = MagicMock()
    fake_agent.invoke.return_value = _make_json_result(structured=audit, raw_text="{}")
    diff = (
        "diff --git a/crates/identity-policy/src/lib.rs b/crates/identity-policy/src/lib.rs\n"
        "@@ -1 +1 @@\n-x\n+y\n"
    )
    with (
        patch("quikode.pre_pr_audit.make_agent", return_value=fake_agent),
        patch("quikode.pre_pr_audit.prompts_mod.render", return_value="prompt"),
    ):
        outcome = pre_pr_audit.run_architecture_audit(
            cfg=cfg,
            handle=_stub_handle(),
            contract=contract,
            diff_excerpt=diff,
            cited_refs=[
                ("docs/architecture/subsystems/identity-policy.md", "Permissions"),
            ],
        )
    assert outcome.passed
    assert not any(f.get("kind") == "unreferenced_applicable_architecture" for f in outcome.findings)
