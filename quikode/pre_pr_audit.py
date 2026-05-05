"""v3.6 pre-PR pipeline: 4-stage gate before opening a PR.

Stage 0  Local CI gate  — run `cfg.local_ci_command` (default `just ci`)
                          inside the dev container. Output parsed via
                          `triage.parse_ci_failure` into structured
                          findings.
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
from . import triage as triage_mod
from .agents import build_agent
from .config import AgentRole, Config
from .docker_env import TaskContainer, exec_in

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
    handle: TaskContainer,
    log_path: Path | None = None,
) -> StageOutcome:
    """Run the configured local-CI command inside the dev container. Empty
    or whitespace-only `local_ci_command` skips the gate (returns passed)."""
    cmd_str = (cfg.local_ci_command or "").strip()
    if not cmd_str:
        return StageOutcome(
            name="local_ci",
            passed=True,
            summary="local_ci_command empty — gate skipped",
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
    failures = triage_mod.parse_ci_failure(blob)
    findings = [
        {
            "kind": f.kind,
            "file": f.file,
            "line": f.line,
            "message": f.message,
            "excerpt": f.excerpt[:1000],
        }
        for f in failures
    ]
    summary = f"local CI failed: rc={rc} ({len(failures)} structured failure(s) extracted)"
    return StageOutcome(
        name="local_ci",
        passed=False,
        summary=summary,
        raw_output=_tail(blob, 200),
        findings=findings,
    )


# ----- Stage 1: rubric audit -----


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def run_rubric_audit(
    *,
    cfg: Config,
    handle: TaskContainer,
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


def collect_standards_text(cfg: Config) -> str:
    """Glob the configured standards profile docs and concatenate them.
    Files outside the repo are silently dropped; missing globs are fine
    (the agent simply has less context). Truncated to 60k chars total."""
    parts: list[str] = []
    seen: set[Path] = set()
    for pat in cfg.pre_pr_standards_profile_globs:
        for path in cfg.repo_path.glob(pat):
            if not path.is_file() or path in seen:
                continue
            seen.add(path)
            try:
                rel = path.relative_to(cfg.repo_path)
            except ValueError:
                rel = path
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            parts.append(f"## {rel}\n\n{content[:8000]}")
    blob = "\n\n---\n\n".join(parts)
    return blob[:60000]


def run_standards_audit(
    *,
    cfg: Config,
    handle: TaskContainer,
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
            passed=True,
            summary="no standards profile docs found — gate skipped",
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
    handle: TaskContainer,
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


def _tail(text: str, n_lines: int) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-n_lines:]) if len(lines) > n_lines else text
