"""Per-subtask agent-output rendering helpers.

Pulled out of `subtask_execution.py` so that module stays under the
600-line architecture budget. Each helper renders one of the
JSON-mode agents' structured outputs (`SubtaskCheckerOutput`,
`SubtaskTriageOutput`) into a free-text artifact body; the artifacts
are read by the briefing agent, the TUI, and `qk show` for human
consumption while the structured object stays the canonical wire
contract.
"""

from __future__ import annotations

from typing import Any

from quikode.agent_schemas import (
    SubtaskCheckerFinding,
    SubtaskCheckerOutput,
    SubtaskTriageOutput,
)


def render_checker_output_for_artifact(out: SubtaskCheckerOutput) -> str:
    """Render the structured checker output to the artifact text
    shape. Includes a `VERDICT: PASS|FAIL` line so the existing
    `_parse_verdict` helper still resolves on store reads, plus a
    `ROOT_CAUSE:` block for `_extract_root_cause` (used by the progress
    agent's attempt history)."""
    lines: list[str] = []
    lines.append(f"VERDICT: {out.verdict.upper()}")
    if out.overall_assessment:
        lines.append(f"ROOT_CAUSE: {out.overall_assessment[:600]}")
    if out.findings:
        lines.append("FINDINGS:")
        for f in out.findings:
            lines.append(_render_finding(f))
    return "\n".join(lines)


def _render_finding(f: SubtaskCheckerFinding) -> str:
    rationale = f" — {f.rationale}" if f.rationale else ""
    return f"  - [{f.verdict.upper()}] {f.category}{rationale}"


def render_triage_output_for_artifact(out: SubtaskTriageOutput) -> str:
    """Render the structured triage output to a human-readable artifact
    string suitable for the next doer attempt's prompt context."""
    lines: list[str] = []
    lines.append(f"failure_layer: {out.failure_layer}")
    lines.append(f"root_cause: {out.root_cause}")
    if out.file_line_cites:
        lines.append("file_line_cites:")
        for cite in out.file_line_cites:
            lines.append(f"  - {cite}")
    if out.teaching_narrative:
        lines.append("teaching_narrative:")
        lines.append(out.teaching_narrative)
    return "\n".join(lines)


def witnesses_all_passed(results: dict[str, dict[str, Any]] | None) -> bool:
    """Plan 53: classify the scoped-witness results as green. An
    empty results dict (no witnesses configured for this subtask) is
    green by definition — the objective gate is the only signal. A
    populated dict requires every entry to have rc==0 AND a non-FAIL
    classification. The witness runner emits classification values
    `"OK"` / `"FAIL"` / `"NO_COMMAND"`; we accept anything except
    `"FAIL"` so a `NO_COMMAND` mid-run doesn't poison the no-op DONE
    path (the objective gate already verified the subtask's
    behaviorally-relevant work)."""
    if not results:
        return True
    for entry in results.values():
        if not isinstance(entry, dict):
            return False
        if int(entry.get("rc", 1) or 1) != 0:
            return False
        classification = str(entry.get("classification") or "").upper()
        if classification == "FAIL":
            return False
    return True


__all__ = [
    "render_checker_output_for_artifact",
    "render_triage_output_for_artifact",
    "witnesses_all_passed",
]
