"""v3.6 pre-PR pipeline: 5-stage gate before opening a PR.

Stage 0 (local_ci) runs `cfg.local_ci_command` and passes raw output
through to triage. Stages 1-4 (rubric / standards / architecture /
behavior) invoke JsonAgent roles `pre_pr_rubric` / `pre_pr_standards`
/ `pre_pr_architecture` / `pre_pr_behavior`, each with a closed
pydantic schema (`PrePR*AuditOutput`). Plan 35 PR-B added the
architecture stage between standards and behavior.

Failures merge into a `audit_findings` bundle → triage → fixup planner
(kind="fixup-pre-pr-audit") → per-subtask doer/checker loop, then the
pipeline re-runs. Cycles cap at `cfg.pre_pr_audit_max_cycles` (default
3); beyond that the node is BLOCKED.

Plan 38 PR-B.3: prose parsing (heuristic JSON extraction) is gone for
all three audit stages. Each audit's outcome is built from the validated
pydantic instance the JsonAgent layer hands back. A schema-validation
failure (`parse_errors` non-empty) is retried in-place before it collapses
to a synthetic FAIL labeled `parse_failure` — the plan-12/14 invariant
(no fabrication) means downstream FIXUP planning sees this as "the audit
couldn't run cleanly", NOT a real grading failure.

Failures from this layer are *not* the same as a checker FAIL — the PR
was *blocked from opening* because the system caught the issue before
review. The merged report is an operator-readable artifact ("here's what
we caught, here's how the doer fixed it") that sits on the eventual PR
description for transparency.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from . import prompts as prompts_mod
from .agent_registry import make_agent
from .agent_schemas import (
    PrePRBehaviorAuditOutput,
    PrePRRubricAuditOutput,
)
from .agents.json_protocol import JsonAgentResult
from .config import Config
from .evaluation_contract import EvaluationContract
from .execution import ExecutionSandbox, exec_in

# Two child modules carry the standards / architecture audit bodies
# (split out to keep this file under the 600-line architecture budget).
# Cycle-tolerant: the children import `from . import pre_pr_audit as
# _pp` at the top of THEIR files, getting the partial module. They
# only access `_pp.X` inside function bodies, called at runtime — by
# then this module is fully constructed. We place these imports here
# so workers / tests can call `pre_pr_audit.run_standards_audit` etc.
# directly (stable import path).
from .pre_pr_audit_architecture import run_architecture_audit
from .pre_pr_audit_standards import run_standards_audit

log = logging.getLogger("quikode.pre_pr_audit")


StageName = Literal["local_ci", "rubric", "standards", "architecture", "behavior"]


@dataclass
class StageOutcome:
    """One stage's result. All four stages produce one of these per cycle."""

    name: StageName
    passed: bool
    summary: str
    raw_output: str = ""
    findings: list[dict] = field(default_factory=list)


@dataclass
class PipelineCycleResult:
    """One full pipeline pass: all four stages run, regardless of failure
    along the way (we want the merged report to surface every issue at
    once, not just the first)."""

    cycle: int
    stages: list[StageOutcome]

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.stages)

    @property
    def failed_stages(self) -> list[StageOutcome]:
        return [s for s in self.stages if not s.passed]


def severity_counts(findings: list[dict]) -> dict[str, int]:
    counts = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    for finding in findings:
        severity = str(finding.get("severity") or "").lower()
        if severity in counts:
            counts[severity] += 1
    return counts


def severity_budget_violations(
    findings: list[dict],
    *,
    medium: int,
    high: int,
    critical: int,
) -> dict[str, tuple[int, int]]:
    counts = severity_counts(findings)
    budgets = {
        "medium": medium,
        "high": high,
        "critical": critical,
    }
    return {
        severity: (counts[severity], budget)
        for severity, budget in budgets.items()
        if counts[severity] > budget
    }


def severity_budget_summary(findings: list[dict]) -> str:
    counts = severity_counts(findings)
    return (
        f"{counts['critical']} critical, {counts['high']} high, "
        f"{counts['medium']} medium, {counts['low']} low"
    )


def format_severity_budget_violations(violations: dict[str, tuple[int, int]]) -> str:
    return ", ".join(f"{severity} {actual}/{budget}" for severity, (actual, budget) in violations.items())


# ----- Stage 0: local CI gate -----


def run_local_ci_gate(
    *,
    cfg: Config,
    handle: ExecutionSandbox,
    log_path: Path | None = None,
) -> StageOutcome:
    """Run the configured local-CI command inside the dev container. Empty
    or whitespace-only `local_ci_command` skips the gate (returns passed)."""
    cmd_str = (cfg.local_ci_command or "").strip()
    if not cmd_str:
        return StageOutcome(
            name="local_ci",
            passed=False,
            summary="cfg.local_ci_command is empty — pipeline cannot validate",
            findings=[
                {
                    "kind": "config_error",
                    "message": "Set cfg.local_ci_command (e.g. 'just ci') to enable the gate.",
                }
            ],
        )
    try:
        rc, stdout, stderr = exec_in(
            handle,
            ["bash", "-lc", cmd_str],
            log_path=log_path,
            timeout=cfg.local_ci_timeout_s,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return StageOutcome(
            name="local_ci",
            passed=False,
            summary=f"local CI raised: {e}",
            raw_output=str(e)[:2000],
            findings=[{"kind": "infra", "message": str(e)[:500]}],
        )
    blob = (stdout or "") + "\n" + (stderr or "")
    if rc == 0:
        return StageOutcome(
            name="local_ci",
            passed=True,
            summary=f"local CI passed: `{cmd_str}` rc=0",
            raw_output=_tail(blob, 80),
        )
    # Pass the full raw output through to the fixup planner. Plan-29
    # retired `triage.parse_ci_failure`'s regex-based extraction because
    # custom runners / BDD blocks / just-recipe wrappers blew up the
    # patterns and left the planner blind. Cap is generous (~250 lines).
    return StageOutcome(
        name="local_ci",
        passed=False,
        summary=f"local CI failed: rc={rc} (full output below)",
        raw_output=_tail(blob, 600),
    )


# ----- shared dispatch for the JsonAgent-backed audit stages -----


def _invoke_audit(
    stage: StageName,
    role: str,
    *,
    cfg: Config,
    handle: ExecutionSandbox,
    log_path: Path | None,
    template: str,
    template_ctx: dict,
    expected_schema: type[BaseModel],
) -> tuple[BaseModel | None, JsonAgentResult, StageOutcome | None]:
    """Render the prompt, dispatch through `make_agent`, and pre-validate.

    Returns `(structured, result, early_outcome)`. When `early_outcome`
    is non-None the caller surfaces it verbatim (render failure, agent
    rc != 0, parse failure, registry/schema mismatch). Otherwise
    `structured` is the validated pydantic instance.
    """
    try:
        prompt = prompts_mod.render(cfg, template, **template_ctx)
    except Exception as e:
        empty = JsonAgentResult(structured=None, rc=0, transient=False, duration_s=0.0)
        return None, empty, _render_failure_outcome(stage, e)
    agent = make_agent(role, cfg)
    attempts = cfg.pre_pr_audit_output_retries + 1
    result: JsonAgentResult | None = None
    for attempt_no in range(1, attempts + 1):
        result = agent.invoke(prompt, handle=handle, log_path=log_path, timeout=cfg.pre_pr_audit_timeout_s)
        if result.rc != 0:
            return None, result, _agent_failure_outcome(stage, result)
        if result.parse_errors or result.structured is None:
            if attempt_no < attempts:
                log.warning(
                    "%s audit output failed schema validation on attempt %d/%d; retrying",
                    stage,
                    attempt_no,
                    attempts,
                )
                continue
            return None, result, _parse_failure_outcome(stage, result)
        if not isinstance(result.structured, expected_schema):
            # Defensive: registry binds the role to its schema; only fires
            # on a registry misconfiguration.
            mismatch = StageOutcome(
                name=stage,
                passed=False,
                summary=(
                    f"{stage} agent returned unexpected schema "
                    f"{type(result.structured).__name__}; expected {expected_schema.__name__}"
                ),
                findings=[{"kind": "infra", "message": "registry/schema mismatch"}],
            )
            return None, result, mismatch
        return result.structured, result, None
    assert result is not None
    return None, result, _parse_failure_outcome(stage, result)


def _parse_failure_outcome(stage: StageName, result: JsonAgentResult) -> StageOutcome:
    """Synthetic FAIL for a schema-validation failure. The
    `kind="parse_failure"` label preserves the plan-12/14 no-fabrication
    invariant — this is "the audit couldn't run cleanly", NOT a content
    finding the FIXUP planner should treat as a real grading failure."""
    parse_errors = list(result.parse_errors)
    rationale = "; ".join(parse_errors)[:500] if parse_errors else "no structured output"
    return StageOutcome(
        name=stage,
        passed=False,
        summary=f"{stage} audit response failed schema validation — failing closed (parse_failure)",
        raw_output=_tail(result.raw_text or "", 200),
        findings=[
            {
                "kind": "parse_failure",
                "message": f"{stage} audit response failed schema validation",
                "rationale": rationale,
                "parse_errors": parse_errors,
            }
        ],
    )


def _agent_failure_outcome(stage: StageName, result: JsonAgentResult) -> StageOutcome:
    """FAIL outcome for transport-level agent failure (rc != 0)."""
    return StageOutcome(
        name=stage,
        passed=False,
        summary=f"{stage} agent rc={result.rc}",
        raw_output=_tail(result.raw_text or "", 80),
        findings=[{"kind": "infra", "message": f"{stage} agent failed", "rc": result.rc}],
    )


def _render_failure_outcome(stage: StageName, exc: Exception) -> StageOutcome:
    """FAIL outcome for a prompt-render exception."""
    return StageOutcome(
        name=stage,
        passed=False,
        summary=f"{stage} prompt render failed: {exc}",
        findings=[{"kind": "infra", "message": str(exc)[:500]}],
    )


# ----- Stage 1: rubric audit -----


def run_rubric_audit(
    *,
    cfg: Config,
    handle: ExecutionSandbox,
    diff_excerpt: str,
    plan_text: str,
    log_path: Path | None = None,
) -> StageOutcome:
    """Score the diff on `cfg.pre_pr_rubric_categories`. Invokes the
    `pre_pr_rubric` role; schema-validation failure → `parse_failure`."""
    structured, result, early = _invoke_audit(
        "rubric",
        "pre_pr_rubric",
        cfg=cfg,
        handle=handle,
        log_path=log_path,
        template="pre-pr-rubric.md",
        template_ctx={
            "categories": list(cfg.pre_pr_rubric_categories),
            "min_score": cfg.pre_pr_rubric_min_score,
            "diff_excerpt": diff_excerpt[:20000],
            "plan_text": plan_text[:6000],
        },
        expected_schema=PrePRRubricAuditOutput,
    )
    if early is not None:
        return early
    assert isinstance(structured, PrePRRubricAuditOutput)
    return _build_rubric_outcome(cfg, structured, result)


def _build_rubric_outcome(
    cfg: Config,
    audit: PrePRRubricAuditOutput,
    result: JsonAgentResult,
) -> StageOutcome:
    """Bridge `PrePRRubricAuditOutput` → `StageOutcome`. Findings retain
    the existing dict shape so `collect_finding_ids` and the audit-bundle
    renderer keep working without change."""
    scores = audit.categories
    failing = [s for s in scores if s.score < cfg.pre_pr_rubric_min_score]
    summary_lines = ", ".join(f"{s.name}={s.score}" for s in scores)
    raw_excerpt = _tail(result.raw_text or "", 200 if failing else 80)
    if failing:
        failing_names = {s.name for s in failing}
        findings: list[dict] = []
        for s in scores:
            if s.name in failing_names:
                kind = "rubric_below_threshold"
                finding_id = f"category-{s.name}"
            elif s.score < 10 and s.gaps_to_reach_ten:
                kind = "rubric_reach_ten_gap"
                finding_id = f"reach-ten-{s.name}"
            else:
                continue
            findings.append(
                {
                    "kind": kind,
                    "id": finding_id,
                    "category": s.name,
                    "score": s.score,
                    "rationale": s.rationale,
                    "gaps_to_reach_ten": [
                        {
                            "id": gap.id,
                            "description": gap.description,
                            "concrete_fix": gap.concrete_fix,
                            "files": list(gap.files),
                        }
                        for gap in s.gaps_to_reach_ten
                    ],
                }
            )
        return StageOutcome(
            name="rubric",
            passed=False,
            summary=(
                f"rubric failed: {len(failing)} category(s) < {cfg.pre_pr_rubric_min_score} ({summary_lines})"
            ),
            raw_output=raw_excerpt,
            findings=findings,
        )
    return StageOutcome(
        name="rubric",
        passed=True,
        summary=f"rubric passed ({summary_lines})",
        raw_output=raw_excerpt,
    )


# ----- Stage 2: standards audit -----


def collect_standards_text(cfg: Config, *, contract: EvaluationContract | None = None) -> str:
    """Plan 33 PR-B / Plan 35 PR-A: returns the contract's already-built
    `standards.source_text` (the rendered profile catalog). Returns an
    empty string when no contract is supplied (the prior on-disk glob
    fallback path was retired in plan 35 along with
    `pre_pr_standards_profile_globs`). Truncated to 60k chars."""
    if contract is None:
        return ""
    text = contract.standards.source_text or ""
    return text[:60000]


# `run_standards_audit` is re-exported from `pre_pr_audit_standards`
# (see the bottom of this file). The body lives in the sibling module
# to keep this file under the 600-line architecture budget after Plan
# 35 PR-B added the architecture stage. The sibling imports
# `_invoke_audit` + `StageOutcome` from here using
# `from . import pre_pr_audit as _pp`, which Python's import machinery
# resolves to the partial module at child-import time (the children
# only access `_pp.X` inside function bodies at call time, never at
# their own import time, so the partial-module reference is safe).


# ----- Stage 3: architecture audit (Plan 35 PR-B) -----
#
# The architecture audit's `run_architecture_audit` + outcome bridge
# live in `pre_pr_audit_architecture.py` to keep this file under the
# 600-line architecture budget. The public function is re-exported
# below for stable import paths (workers + tests use
# `pre_pr_audit.run_architecture_audit`).


# `run_architecture_audit` is bound at module import time (bottom of
# file) from `pre_pr_audit_architecture.run_architecture_audit`. Same
# split rationale as `run_standards_audit` above.


# ----- Stage 4: behavior audit -----


def run_behavior_audit(
    *,
    cfg: Config,
    handle: ExecutionSandbox,
    expected_evidence: list[dict],
    diff_excerpt: str,
    plan_text: str,
    log_path: Path | None = None,
) -> StageOutcome:
    """Verify each `expected_evidence` item is real. Invokes the
    `pre_pr_behavior` role; schema-validation failure → `parse_failure`."""
    if not expected_evidence:
        return StageOutcome(
            name="behavior",
            passed=True,
            summary="no expected_evidence on this node — gate skipped",
        )
    structured, result, early = _invoke_audit(
        "behavior",
        "pre_pr_behavior",
        cfg=cfg,
        handle=handle,
        log_path=log_path,
        template="pre-pr-behavior.md",
        template_ctx={
            "expected_evidence": expected_evidence,
            "diff_excerpt": diff_excerpt[:20000],
            "plan_text": plan_text[:6000],
        },
        expected_schema=PrePRBehaviorAuditOutput,
    )
    if early is not None:
        return early
    assert isinstance(structured, PrePRBehaviorAuditOutput)
    return _build_behavior_outcome(structured, result)


def _build_behavior_outcome(
    audit: PrePRBehaviorAuditOutput,
    result: JsonAgentResult,
) -> StageOutcome:
    """Bridge `PrePRBehaviorAuditOutput` → `StageOutcome`. Findings
    retain the existing dict shape so `collect_finding_ids` (walks
    `behavior_id` + `completeness_gaps`) keeps working unchanged."""
    behaviors = audit.behaviors
    unverified = [b for b in behaviors if not b.verified]
    raw_excerpt = _tail(result.raw_text or "", 200 if unverified else 80)
    if unverified:
        findings: list[dict] = [
            {
                "kind": "behavior_unverified",
                "behavior_id": b.behavior_id,
                "evidence_seen": b.evidence_seen,
                "gap_explanation": b.gap_explanation,
                "concrete_fix": b.concrete_fix,
                "completeness_gaps": [
                    {
                        "id": gap.id,
                        "description": gap.description,
                        "concrete_fix": gap.concrete_fix,
                    }
                    for gap in b.completeness_gaps
                ],
            }
            for b in unverified
        ]
        return StageOutcome(
            name="behavior",
            passed=False,
            summary=f"behavior failed: {len(unverified)} unverified behavior(s)",
            raw_output=raw_excerpt,
            findings=findings,
        )
    return StageOutcome(
        name="behavior",
        passed=True,
        summary=f"behavior passed ({len(behaviors)} verified)",
        raw_output=raw_excerpt,
    )


# ----- merge findings into a triage-ready bundle -----


def merge_failed_stage_reports(failed: list[StageOutcome]) -> str:
    """Build a single human-readable + agent-ingestible bundle of every
    failure across the four stages. The fixup planner consumes this as
    the `audit_findings` block."""
    if not failed:
        return ""
    sections: list[str] = []
    for s in failed:
        header = f"## Stage `{s.name}` — {s.summary}"
        body_lines = [header]
        if s.findings:
            body_lines.append("\n### Structured findings\n")
            body_lines.append("```json")
            body_lines.append(json.dumps(s.findings, indent=2)[:6000])
            body_lines.append("```")
        if s.raw_output:
            body_lines.append("\n### Agent output (tail)\n")
            body_lines.append("```")
            body_lines.append(s.raw_output[:4000])
            body_lines.append("```")
        sections.append("\n".join(body_lines))
    return "\n\n---\n\n".join(sections)


def collect_finding_ids(failed: list[StageOutcome]) -> list[str]:
    """Extract every finding `id` (namespaced by stage) across the failed
    stages. Used by the orchestrator's completeness check to verify the
    fixup planner mapped every finding to a subtask. Findings without an
    explicit `id` get a synthetic id derived from stage + file/kind so
    they still appear in the coverage check.
    """
    ids: list[str] = []
    seen: set[str] = set()
    for stage in failed:
        for idx, f in enumerate(stage.findings or []):
            raw_id = (
                f.get("id") or f.get("behavior_id") or f.get("category") or f.get("file") or f.get("kind")
            )
            fid = f"{stage.name}:{raw_id}" if raw_id else f"{stage.name}:auto-{idx}"
            # Walk each rubric category's gaps_to_reach_ten if present.
            gaps = f.get("gaps_to_reach_ten") or []
            if isinstance(gaps, list):
                for gap in gaps:
                    if isinstance(gap, dict) and gap.get("id"):
                        gap_fid = f"{stage.name}:{gap['id']}"
                        if gap_fid not in seen:
                            seen.add(gap_fid)
                            ids.append(gap_fid)
            # Walk behavior `completeness_gaps` similarly.
            cgaps = f.get("completeness_gaps") or []
            if isinstance(cgaps, list):
                for cgap in cgaps:
                    if isinstance(cgap, dict) and cgap.get("id"):
                        cgap_fid = f"{stage.name}:{cgap['id']}"
                        if cgap_fid not in seen:
                            seen.add(cgap_fid)
                            ids.append(cgap_fid)
            if fid not in seen:
                seen.add(fid)
                ids.append(fid)
    return ids


def _tail(text: str, n_lines: int) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-n_lines:]) if len(lines) > n_lines else text


__all__ = [
    "PipelineCycleResult",
    "StageName",
    "StageOutcome",
    "collect_finding_ids",
    "collect_standards_text",
    "format_severity_budget_violations",
    "merge_failed_stage_reports",
    "run_architecture_audit",
    "run_behavior_audit",
    "run_local_ci_gate",
    "run_rubric_audit",
    "run_standards_audit",
    "severity_budget_summary",
    "severity_budget_violations",
    "severity_counts",
]
