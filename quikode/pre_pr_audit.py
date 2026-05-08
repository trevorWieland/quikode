"""v3.6 pre-PR pipeline: 4-stage gate before opening a PR.

Stage 0  Local CI gate  — run `cfg.local_ci_command` (default `just ci`)
                          inside the dev container. The full raw output is
                          passed through to downstream consumers (triage +
                          fixup planner) without regex-based extraction —
                          structured-failure parsing was lossy on outputs
                          the patterns didn't match (e.g. R-0021's "0
                          structured failure(s) extracted").
Stage 1  Rubric audit   — codex agent rates the diff on
                          `cfg.pre_pr_rubric_categories` from 1-10. Any
                          category < `cfg.pre_pr_rubric_min_score` fails.
Stage 2  Standards audit — claude-opus reads cfg-globbed standards
                          profile docs + branch diff, outputs structured
                          findings (severity ≥ medium gates).
Stage 3  Behavior audit  — codex verifies each item in
                          `node.expected_evidence` is real (run the
                          witness, exercise the interface).

If any stage fails, all findings are merged → triage agent → fixup
planner with `kind="fixup-pre-pr-audit"` → per-subtask doer/checker
loop. After the loop completes, the pipeline re-runs from stage 0.
Cycles cap via `cfg.pre_pr_audit_max_cycles` (default 3) — anything
beyond is BLOCKED with the merged findings as the operator-actionable
context.

Each audit returns a `StageOutcome` carrying `passed: bool`, a
short `summary`, the raw (truncated) agent stdout, and the
structured `findings` JSON the triage layer consumes.

Failures from this layer are *not* the same as a checker FAIL — the
PR was *blocked from opening* because the system caught the issue
before review. The merged report is therefore an operator-readable
artifact ("here's what we caught, here's how the doer fixed it") that
can sit on the eventual PR description for transparency.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import prompts as prompts_mod
from .agents import build_agent
from .config import AgentRole, Config
from .evaluation_contract import EvaluationContract
from .execution import ExecutionSandbox, exec_in

log = logging.getLogger("quikode.pre_pr_audit")


StageName = Literal["local_ci", "rubric", "standards", "behavior"]


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
    # Pass the full raw output through to the fixup planner. Pre-plan-29 we
    # ran `triage.parse_ci_failure` to extract structured findings via regex,
    # but on outputs the patterns didn't match (custom test runners, BDD
    # scenario blocks, just-recipe wrappers) the extraction returned 0
    # findings AND we tailed the output to 200 lines — leaving the planner
    # blind. Hand over the unfiltered context and let the planner decide what
    # to fix. Cap is generous (16k chars / ~250 lines) to fit the prompt
    # budget without truncating typical multi-failure runs.
    summary = f"local CI failed: rc={rc} (full output below)"
    return StageOutcome(
        name="local_ci",
        passed=False,
        summary=summary,
        raw_output=_tail(blob, 600),
    )


# ----- Stage 1: rubric audit -----


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def run_rubric_audit(
    *,
    cfg: Config,
    handle: ExecutionSandbox,
    diff_excerpt: str,
    plan_text: str,
    role: AgentRole | None = None,
    log_path: Path | None = None,
) -> StageOutcome:
    """Score the diff on `cfg.pre_pr_rubric_categories`. Default uses the
    checker role (codex 5.5-class) — better at structural reasoning than
    the lightweight intent-reviewer model."""
    role = role or cfg.checker
    try:
        prompt = prompts_mod.render(
            cfg,
            "pre-pr-rubric.md",
            categories=list(cfg.pre_pr_rubric_categories),
            min_score=cfg.pre_pr_rubric_min_score,
            diff_excerpt=diff_excerpt[:20000],
            plan_text=plan_text[:6000],
        )
    except Exception as e:
        return StageOutcome(
            name="rubric",
            passed=False,
            summary=f"rubric prompt render failed: {e}",
            findings=[{"kind": "infra", "message": str(e)[:500]}],
        )
    agent = build_agent(role)
    result = agent.run(prompt, handle=handle, log_path=log_path, timeout=cfg.pre_pr_audit_timeout_s)
    if not result.ok:
        return StageOutcome(
            name="rubric",
            passed=False,
            summary=f"rubric agent rc={result.rc}",
            raw_output=_tail(result.stdout, 80),
            findings=[{"kind": "infra", "message": "rubric agent failed", "rc": result.rc}],
        )
    parsed = _parse_rubric_envelope(result.stdout)
    if parsed is None:
        return StageOutcome(
            name="rubric",
            passed=False,
            summary="rubric envelope unparseable — failing closed",
            raw_output=_tail(result.stdout, 200),
            findings=[{"kind": "parse_error", "message": "rubric agent produced no parseable JSON"}],
        )
    scores: list[dict] = parsed.get("categories", [])
    failing = [s for s in scores if int(s.get("score", 0)) < cfg.pre_pr_rubric_min_score]
    summary_lines = ", ".join(f"{s.get('name')}={s.get('score')}" for s in scores)
    if failing:
        return StageOutcome(
            name="rubric",
            passed=False,
            summary=f"rubric failed: {len(failing)} category(s) < {cfg.pre_pr_rubric_min_score} ({summary_lines})",
            raw_output=_tail(result.stdout, 200),
            findings=[
                {
                    "kind": "rubric_below_threshold",
                    "category": s.get("name"),
                    "score": s.get("score"),
                    "rationale": s.get("rationale", ""),
                }
                for s in failing
            ],
        )
    return StageOutcome(
        name="rubric",
        passed=True,
        summary=f"rubric passed ({summary_lines})",
        raw_output=_tail(result.stdout, 80),
    )


def _parse_rubric_envelope(text: str) -> dict | None:
    """Parse `{"categories":[{"name":"x","score":N,"rationale":"..."}]}`
    out of the agent's stdout. Tolerates leading prose."""
    if not text or not text.strip():
        return None
    candidates = [text.strip()]
    m = _JSON_OBJ_RE.search(text)
    if m:
        candidates.append(m.group(0))
    for cand in candidates:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "categories" in data and isinstance(data["categories"], list):
            return data
    return None


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


def run_standards_audit(
    *,
    cfg: Config,
    handle: ExecutionSandbox,
    diff_excerpt: str,
    standards_text: str,
    role: AgentRole | None = None,
    log_path: Path | None = None,
) -> StageOutcome:
    """Compare branch diff against the configured standards profile. Uses
    `cfg.triage` (claude-opus) by default — the structural reasoning load
    is the same as the conflict-resolver / triage agent."""
    role = role or cfg.triage
    if not standards_text.strip():
        return StageOutcome(
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
    try:
        prompt = prompts_mod.render(
            cfg,
            "pre-pr-standards.md",
            standards_text=standards_text,
            diff_excerpt=diff_excerpt[:30000],
        )
    except Exception as e:
        return StageOutcome(
            name="standards",
            passed=False,
            summary=f"standards prompt render failed: {e}",
            findings=[{"kind": "infra", "message": str(e)[:500]}],
        )
    agent = build_agent(role)
    result = agent.run(prompt, handle=handle, log_path=log_path, timeout=cfg.pre_pr_audit_timeout_s)
    if not result.ok:
        return StageOutcome(
            name="standards",
            passed=False,
            summary=f"standards agent rc={result.rc}",
            raw_output=_tail(result.stdout, 80),
            findings=[{"kind": "infra", "message": "standards agent failed", "rc": result.rc}],
        )
    parsed = _parse_findings_envelope(result.stdout)
    if parsed is None:
        return StageOutcome(
            name="standards",
            passed=False,
            summary="standards envelope unparseable — failing closed",
            raw_output=_tail(result.stdout, 200),
            findings=[{"kind": "parse_error", "message": "standards agent produced no parseable JSON"}],
        )
    findings = parsed.get("findings", [])
    serious = [f for f in findings if f.get("severity") in ("high", "medium", "critical")]
    if serious:
        return StageOutcome(
            name="standards",
            passed=False,
            summary=f"standards failed: {len(serious)} medium+ severity finding(s)",
            raw_output=_tail(result.stdout, 200),
            findings=findings,
        )
    return StageOutcome(
        name="standards",
        passed=True,
        summary=f"standards passed ({len(findings)} low-severity note(s))",
        raw_output=_tail(result.stdout, 80),
        findings=findings,
    )


def _parse_findings_envelope(text: str) -> dict | None:
    """Parse `{"findings":[{"file":"x","severity":"low|medium|high|critical",...}]}`."""
    if not text or not text.strip():
        return None
    candidates = [text.strip()]
    m = _JSON_OBJ_RE.search(text)
    if m:
        candidates.append(m.group(0))
    for cand in candidates:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "findings" in data and isinstance(data["findings"], list):
            return data
    return None


# ----- Stage 3: behavior audit -----


def run_behavior_audit(
    *,
    cfg: Config,
    handle: ExecutionSandbox,
    expected_evidence: list[dict],
    diff_excerpt: str,
    plan_text: str,
    role: AgentRole | None = None,
    log_path: Path | None = None,
) -> StageOutcome:
    """Verify each `expected_evidence` item is real (run the witness or
    exercise the cited interface). Uses `cfg.checker` (codex) since it has
    `--dangerously-bypass-approvals-and-sandbox` to actually exercise
    code, unlike the safer claude-class roles."""
    role = role or cfg.checker
    if not expected_evidence:
        return StageOutcome(
            name="behavior",
            passed=True,
            summary="no expected_evidence on this node — gate skipped",
        )
    try:
        prompt = prompts_mod.render(
            cfg,
            "pre-pr-behavior.md",
            expected_evidence=expected_evidence,
            diff_excerpt=diff_excerpt[:20000],
            plan_text=plan_text[:6000],
        )
    except Exception as e:
        return StageOutcome(
            name="behavior",
            passed=False,
            summary=f"behavior prompt render failed: {e}",
            findings=[{"kind": "infra", "message": str(e)[:500]}],
        )
    agent = build_agent(role)
    result = agent.run(prompt, handle=handle, log_path=log_path, timeout=cfg.pre_pr_audit_timeout_s)
    if not result.ok:
        return StageOutcome(
            name="behavior",
            passed=False,
            summary=f"behavior agent rc={result.rc}",
            raw_output=_tail(result.stdout, 80),
            findings=[{"kind": "infra", "message": "behavior agent failed", "rc": result.rc}],
        )
    parsed = _parse_behavior_envelope(result.stdout)
    if parsed is None:
        return StageOutcome(
            name="behavior",
            passed=False,
            summary="behavior envelope unparseable — failing closed",
            raw_output=_tail(result.stdout, 200),
            findings=[{"kind": "parse_error", "message": "behavior agent produced no parseable JSON"}],
        )
    behaviors = parsed.get("behaviors", [])
    unverified = [b for b in behaviors if not b.get("verified")]
    if unverified:
        return StageOutcome(
            name="behavior",
            passed=False,
            summary=f"behavior failed: {len(unverified)} unverified behavior(s)",
            raw_output=_tail(result.stdout, 200),
            findings=[
                {
                    "kind": "behavior_unverified",
                    "behavior_id": b.get("behavior_id"),
                    "evidence_seen": b.get("evidence_seen", ""),
                    "gap_explanation": b.get("gap_explanation", ""),
                }
                for b in unverified
            ],
        )
    return StageOutcome(
        name="behavior",
        passed=True,
        summary=f"behavior passed ({len(behaviors)} verified)",
        raw_output=_tail(result.stdout, 80),
    )


def _parse_behavior_envelope(text: str) -> dict | None:
    """Parse `{"behaviors":[{"behavior_id":"x","verified":bool,...}]}`."""
    if not text or not text.strip():
        return None
    candidates = [text.strip()]
    m = _JSON_OBJ_RE.search(text)
    if m:
        candidates.append(m.group(0))
    for cand in candidates:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "behaviors" in data and isinstance(data["behaviors"], list):
            return data
    return None


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
    fixup planner mapped every finding to a subtask.

    The audit prompts emit a stable kebab-case `id` field per finding /
    gap; we namespace with the stage name (rubric / standards / behavior)
    to avoid collisions across stages and to keep the planner's
    `findings_addressed` list traceable.

    Findings without an `id` (e.g. local_ci's structured CI failures
    which have file/line but no semantic id, or rubric `gaps_to_reach_ten`
    on the older prompt format) get a synthetic id derived from stage +
    file/line so they still appear in the coverage check.
    """
    ids: list[str] = []
    seen: set[str] = set()
    for stage in failed:
        for idx, f in enumerate(stage.findings or []):
            raw_id = (
                f.get("id") or f.get("behavior_id") or f.get("category") or f.get("file") or f.get("kind")
            )
            fid = f"{stage.name}:{raw_id}" if raw_id else f"{stage.name}:auto-{idx}"
            # Walk each rubric category's gaps_to_reach_ten if present
            # (the v3.7 rubric prompt emits these inline).
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
