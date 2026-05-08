"""Plan 35 PR-B: the standards audit stage (2nd gauntlet stage, retargeted).

Lives in its own module so `pre_pr_audit.py` stays under the 600-line
architecture budget. Plan 35 PR-B retargets the prompt context: instead
of a free-form 60k blob, the audit receives `profile_catalog` (the
contract's rendered profile catalog) + `standards_refs_in_diff` (the
per-task pinned profile sections inlined). The
`unreferenced-applicable-standard` detector runs after the LLM auditor
returns and contributes additional findings keyed off each profile
doc's `applies_to` glob.
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
    changed_files_from_diff,
    collect_standards_refs_in_diff,
    render_cited_sections,
    unreferenced_applicable_standards,
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
    cited_sections, cited_doc_paths = collect_standards_refs_in_diff(contract=contract, cited=cited_refs)
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
    changed_files = changed_files_from_diff(diff_excerpt)
    extra = unreferenced_applicable_standards(
        contract=contract,
        changed_files=changed_files,
        cited_doc_paths=cited_doc_paths,
    )
    return _build_standards_outcome(structured, result, unreferenced_findings=extra)


def _build_standards_outcome(
    audit: PrePRStandardsAuditOutput,
    result: JsonAgentResult,
    *,
    unreferenced_findings: list[dict] | None = None,
) -> _pp.StageOutcome:
    """Bridge `PrePRStandardsAuditOutput` → `StageOutcome`. Findings
    retain the existing dict shape so downstream consumers don't change.

    Plan 35 §2.10: `unreferenced_findings` (the unreferenced-applicable
    detector output) are appended into the same findings list and gated
    on the same severity threshold — same fixup-planner shape.
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
    if unreferenced_findings:
        findings_dicts.extend(unreferenced_findings)
    serious = [f for f in findings_dicts if f.get("severity") in ("medium", "high", "critical")]
    raw_excerpt = _pp._tail(result.raw_text or "", 200 if serious else 80)
    if serious:
        return _pp.StageOutcome(
            name="standards",
            passed=False,
            summary=f"standards failed: {len(serious)} medium+ severity finding(s)",
            raw_output=raw_excerpt,
            findings=findings_dicts,
        )
    return _pp.StageOutcome(
        name="standards",
        passed=True,
        summary=f"standards passed ({len(findings_dicts)} low-severity note(s))",
        raw_output=raw_excerpt,
        findings=findings_dicts,
    )


__all__ = ["run_standards_audit"]
