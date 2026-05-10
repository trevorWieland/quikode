"""Plan 35 PR-B: the architecture audit stage (5th gauntlet stage).

Lives in its own module so `pre_pr_audit.py` stays under the 600-line
architecture budget. Same shape as `pre_pr_audit.run_standards_audit`:
renders the architecture corpus TOC + cited section bodies + the diff,
invokes the `pre_pr_architecture` JsonAgent role, and bridges the
validated `PrePRArchitectureAuditOutput` into a `StageOutcome` with
the existing dict-shape findings. Severity gating uses the configured
architecture severity budgets (same shape as standards).
"""

from __future__ import annotations

from pathlib import Path

from . import pre_pr_audit as _pp
from .agent_schemas import PrePRArchitectureAuditOutput
from .agents.json_protocol import JsonAgentResult
from .config import Config
from .evaluation_contract import EvaluationContract
from .execution import ExecutionSandbox
from .pre_pr_audit_refs import (
    collect_architecture_refs_in_diff,
    render_cited_sections,
)


def run_architecture_audit(
    *,
    cfg: Config,
    handle: ExecutionSandbox,
    contract: EvaluationContract,
    diff_excerpt: str,
    cited_refs: list[tuple[str, str]],
    log_path: Path | None = None,
) -> _pp.StageOutcome:
    """Plan 35 PR-B: compare branch diff against the project's documented
    subsystem contracts. Parallel to `run_standards_audit`. Renders
    `contract.architecture.source_text` (the architecture-doc TOC) +
    cited sections inlined from the union of `architecture_referenced`
    across the plan's subtasks. Invokes the `pre_pr_architecture` role;
    schema-validation failure → `parse_failure`.
    """
    if not contract.architecture.corpus.docs:
        return _pp.StageOutcome(
            name="architecture",
            passed=False,
            summary=(
                "no architecture docs loaded — configure "
                "`architecture_docs_dir` + `architecture_doc_globs` to "
                "enable the gate"
            ),
            findings=[
                {
                    "kind": "config_error",
                    "message": (
                        "No architecture docs loaded. Set "
                        "`architecture_docs_dir` (and optionally "
                        "`architecture_doc_globs`) in quikode config (plan 35)."
                    ),
                }
            ],
        )
    architecture_corpus_text = (contract.architecture.source_text or "").strip()
    cited_sections, _ = collect_architecture_refs_in_diff(contract=contract, cited=cited_refs)
    architecture_refs_in_diff = render_cited_sections(cited_sections)
    structured, result, early = _pp._invoke_audit(
        "architecture",
        "pre_pr_architecture",
        cfg=cfg,
        handle=handle,
        log_path=log_path,
        template="pre-pr-architecture.md",
        template_ctx={
            "architecture_corpus": architecture_corpus_text,
            "architecture_refs_in_diff": architecture_refs_in_diff,
            "diff_excerpt": diff_excerpt[:30000],
        },
        expected_schema=PrePRArchitectureAuditOutput,
    )
    if early is not None:
        return early
    assert isinstance(structured, PrePRArchitectureAuditOutput)
    return _build_architecture_outcome(cfg, structured, result)


def _build_architecture_outcome(
    cfg: Config,
    audit: PrePRArchitectureAuditOutput,
    result: JsonAgentResult,
) -> _pp.StageOutcome:
    """Bridge `PrePRArchitectureAuditOutput` → `StageOutcome`. Same gating
    and findings shape as the standards audit; uses the `architecture_doc_ref`
    field name to keep the bucket distinction visible in fixup planning +
    downstream rendering.
    """
    findings_dicts: list[dict] = [
        {
            "id": f.id,
            "file": f.file,
            "line": f.line,
            "severity": f.severity,
            "architecture_doc_ref": f.architecture_doc_ref,
            "description": f.description,
            "concrete_fix": f.concrete_fix,
        }
        for f in audit.findings
    ]
    violations = _pp.severity_budget_violations(
        findings_dicts,
        medium=cfg.pre_pr_architecture_max_medium_findings,
        high=cfg.pre_pr_architecture_max_high_findings,
        critical=cfg.pre_pr_architecture_max_critical_findings,
    )
    raw_excerpt = _pp._tail(result.raw_text or "", 200 if violations else 80)
    severity_summary = _pp.severity_budget_summary(findings_dicts)
    if violations:
        return _pp.StageOutcome(
            name="architecture",
            passed=False,
            summary=(
                "architecture failed: severity budget exceeded "
                f"({_pp.format_severity_budget_violations(violations)}; {severity_summary})"
            ),
            raw_output=raw_excerpt,
            findings=findings_dicts,
        )
    return _pp.StageOutcome(
        name="architecture",
        passed=True,
        summary=f"architecture passed ({severity_summary})",
        raw_output=raw_excerpt,
        findings=findings_dicts,
    )


__all__ = ["run_architecture_audit"]
