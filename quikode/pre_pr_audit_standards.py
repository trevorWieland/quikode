"""Plan 35 PR-B: the standards audit stage (2nd gauntlet stage, retargeted).

Lives in its own module so `pre_pr_audit.py` stays under the 600-line
architecture budget. Plan 35 PR-B retargets the prompt context: instead
of a free-form 60k blob, the audit receives `profile_catalog` (the
contract's rendered profile catalog) + `standards_refs_in_diff` (the
per-task pinned profile sections inlined).
"""

from __future__ import annotations

from pathlib import Path

from . import pre_pr_audit as _pp
from .agent_schemas import PrePRStandardsAuditOutput
from .agents.json_protocol import JsonAgentResult
from .config import Config
from .evaluation_contract import EvaluationContract
from .execution import ExecutionSandbox
from .pre_pr_audit_refs import (
    collect_standards_refs_in_diff,
    render_cited_sections,
)


def run_standards_audit(
    *,
    cfg: Config,
    handle: ExecutionSandbox,
    contract: EvaluationContract,
    diff_excerpt: str,
    cited_refs: list[tuple[str, str]],
    log_path: Path | None = None,
) -> _pp.StageOutcome:
    """Compare branch diff against the configured standards profile.
    Plan 35 PR-B retargets: receives the contract + the per-task
    `cited_refs` (the union of `standards_referenced` from the plan's
    subtasks). Renders the profile catalog (`contract.standards.source_text`)
    + the cited sections inlined; never receives a free-form blob.
    Invokes the `pre_pr_standards` role; schema-validation failure →
    `parse_failure`."""
    profile_catalog = (contract.standards.source_text or "").strip()
    if not profile_catalog or not contract.standards.profiles:
        return _pp.StageOutcome(
            name="standards",
            passed=False,
            summary=(
                "no standards profile docs loaded — configure "
                "`standards_profiles_dir` + `standards_profiles` to enable the gate"
            ),
            findings=[
                {
                    "kind": "config_error",
                    "message": (
                        "No standards profile docs loaded. Set "
                        "`standards_profiles_dir` and `standards_profiles` "
                        "in quikode config (plan 35)."
                    ),
                }
            ],
        )
    cited_sections, _ = collect_standards_refs_in_diff(contract=contract, cited=cited_refs)
    standards_refs_in_diff = render_cited_sections(cited_sections)
    structured, result, early = _pp._invoke_audit(
        "standards",
        "pre_pr_standards",
        cfg=cfg,
        handle=handle,
        log_path=log_path,
        template="pre-pr-standards.md",
        template_ctx={
            "profile_catalog": profile_catalog,
            "standards_refs_in_diff": standards_refs_in_diff,
            "diff_excerpt": diff_excerpt[:30000],
        },
        expected_schema=PrePRStandardsAuditOutput,
    )
    if early is not None:
        return early
    assert isinstance(structured, PrePRStandardsAuditOutput)
    return _build_standards_outcome(cfg, structured, result)


def _build_standards_outcome(
    cfg: Config,
    audit: PrePRStandardsAuditOutput,
    result: JsonAgentResult,
) -> _pp.StageOutcome:
    """Bridge `PrePRStandardsAuditOutput` → `StageOutcome`. Findings
    retain the existing dict shape so downstream consumers don't change.
    """
    findings_dicts: list[dict] = [
        {
            "id": f.id,
            "file": f.file,
            "line": f.line,
            "severity": f.severity,
            "profile_doc_ref": f.standards_doc_ref,
            "description": f.description,
            "concrete_fix": f.concrete_fix,
        }
        for f in audit.findings
    ]
    violations = _pp.severity_budget_violations(
        findings_dicts,
        medium=cfg.pre_pr_standards_max_medium_findings,
        high=cfg.pre_pr_standards_max_high_findings,
        critical=cfg.pre_pr_standards_max_critical_findings,
    )
    raw_excerpt = _pp._tail(result.raw_text or "", 200 if violations else 80)
    severity_summary = _pp.severity_budget_summary(findings_dicts)
    if violations:
        return _pp.StageOutcome(
            name="standards",
            passed=False,
            summary=(
                "standards failed: severity budget exceeded "
                f"({_pp.format_severity_budget_violations(violations)}; {severity_summary})"
            ),
            raw_output=raw_excerpt,
            findings=findings_dicts,
        )
    return _pp.StageOutcome(
        name="standards",
        passed=True,
        summary=f"standards passed ({severity_summary})",
        raw_output=raw_excerpt,
        findings=findings_dicts,
    )


__all__ = ["run_standards_audit"]
