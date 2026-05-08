"""Plan 33 PR-B: stage-typed coverage check for the fixup planner.

Used by `quikode.workers.pre_pr.PrePrWorkerMixin._run_fixup_round` to
verify the fixup planner's `FixupPlan` covers every audit finding id.
The retired `addresses_findings` per-subtask field (Plan 33 D2) is
replaced by a union over `rubric_targets`, `standards_referenced`,
`behavior_evidence_advanced`, plus the plan-level `findings_addressed`
array, plus a `notes`-text fallback for finding ids the planner
groups into one subtask.

Lives in its own module to keep `pre_pr.py` under the 600-line budget.

Plan 33 calibration (after the tanren R-0002 BLOCK): also owns the
fixup-side validator orchestration. The fixup driver swaps
`validate_rubric_coverage` for `validate_finding_coverage` because a
fixup plan addresses specific audit gaps (per-finding), not whole
rubric categories — so empty `rubric_targets` is legitimate when a
fixup is purely transport/CI/standards. Driver-side validator routing
stays out of `pre_pr.py` to keep that file under budget.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from quikode.planner_validators import (
    PlannerValidationError,
    validate_evidence_partition,
    validate_finding_coverage,
    validate_standards_paths,
)
from quikode.subtask_schema import FixupPlan, PlanValidationError, parse_fixup_planner_output

if TYPE_CHECKING:
    from quikode.dag import Node


def collect_stage_coverage(plan: FixupPlan) -> tuple[set[str], set[str], set[str], str]:
    """Build the (rubric_cats, standards_paths, behavior_evids, notes_text)
    coverage union over the plan's subtasks. Plan 33 PR-B: the audit-
    completeness check unions across these fields instead of the retired
    per-subtask `addresses_findings`."""
    rubric_cats: set[str] = set()
    standards_paths: set[str] = set()
    behavior_evids: set[str] = set()
    notes_blob: list[str] = []
    for s in plan.subtasks:
        for tgt in s.rubric_targets:
            rubric_cats.add(tgt.category)
        for ref in s.standards_referenced:
            standards_paths.add(ref.doc_path)
        for evid in s.behavior_evidence_advanced:
            behavior_evids.add(evid)
        if s.notes:
            notes_blob.append(s.notes)
    return rubric_cats, standards_paths, behavior_evids, "\n".join(notes_blob)


def missing_finding_coverage(plan: FixupPlan, expected_finding_ids: list[str]) -> set[str]:
    """A finding id is covered when it appears in the plan-level
    `findings_addressed` OR when it can be matched to one of the plan's
    subtasks via stage-typed coverage.

    The audit emits ids namespaced by stage (`rubric:<gap-id>`,
    `standards:<finding-id>`, `behavior:<id>`). For each id we attempt:

    * Stage prefix `rubric:` → covered when ANY subtask declares any
      `rubric_targets` (the per-subtask checker verifies the diff
      substantively advances the category later) OR cites the finding
      id in its `notes`.
    * Stage prefix `standards:` → covered when ANY subtask declares any
      `standards_referenced` OR cites the finding id in `notes`.
    * Stage prefix `behavior:` → covered when the corresponding
      evidence id appears in some subtask's
      `behavior_evidence_advanced` OR the finding id is cited in `notes`.

    Anything in `plan.findings_addressed` is covered regardless.
    """
    expected = set(expected_finding_ids)
    covered: set[str] = set(plan.findings_addressed)
    rubric_cats, standards_paths, behavior_evids, notes_text = collect_stage_coverage(plan)
    for fid in expected - covered:
        if fid in notes_text:
            covered.add(fid)
            continue
        if fid.startswith("behavior:"):
            evid = fid.split(":", 1)[1]
            if evid in behavior_evids:
                covered.add(fid)
                continue
        if fid.startswith("rubric:") and rubric_cats:
            covered.add(fid)
            continue
        if fid.startswith("standards:") and standards_paths:
            covered.add(fid)
    return expected - covered


def parse_and_validate_fixup_plan(
    stdout: str,
    *,
    repo_root: Path,
    node: Node,
    audit_findings: list[str] | None,
) -> tuple[FixupPlan | None, str | None]:
    """Parse + run the fixup-side validator suite on a planner stdout.

    Returns `(plan, None)` on success, or `(None, error_message)` if the
    parse failed or any validator raised. The error_message is the
    structured feedback the caller should feed into the next planner
    re-prompt (matches the spec planner's re-prompt shape).

    Validators run, in order:

    * `validate_finding_coverage(plan, audit_findings)` — fixup-only
      partition check (replaces `validate_rubric_coverage` per Plan 33
      calibration; a fixup addresses findings, not whole categories).
    * `validate_evidence_partition(plan, node)` — same as the spec
      side; the audit's `behavior:<id>` findings need exactly-once
      coverage so this stays universal.
    * `validate_standards_paths(plan, repo_root)` — same as the spec
      side; cited standards docs must exist regardless of plan kind.

    `audit_findings` may be None or empty — both short-circuit the
    finding-coverage validator (common for `fixup-final` / `fixup-ci`
    / `fixup-review` where the trigger context replaces the typed
    finding bundle).
    """
    try:
        plan = parse_fixup_planner_output(stdout)
    except PlanValidationError as e:
        return None, (
            "Your previous fixup plan failed JSON-schema validation:\n\n"
            f"```\n{e}\n```\n\n"
            "Re-emit a single fenced ```json ... ``` block that conforms "
            "strictly to the schema. Note specifically: "
            "`standards_referenced` items MUST be objects with "
            '`{"doc_path": "...", "section": "..."}`, not strings.'
        )
    try:
        validate_finding_coverage(plan, audit_findings or [])
        validate_evidence_partition(plan, node)
        validate_standards_paths(plan, repo_root)
    except PlannerValidationError as ve:
        return None, (
            f"Your previous fixup plan failed validator `{ve.which}`. "
            f"Re-emit the COMPLETE fixup plan correcting the following:\n\n"
            f"{ve.message}"
        )
    return plan, None


def split_subtask_rows_for_planner(rows: list[dict], done_state_value: str) -> tuple[list[dict], list[dict]]:
    """Bucket existing subtask rows for the fixup-planner prompt.

    Returns `(done_spec_subtasks, prior_fixup_subtasks)` — each a list
    of view-dicts the prompt template iterates over. Lives here so
    `pre_pr.py` stays under the architecture line-budget.
    """
    done_subtasks: list[dict] = []
    prior_fixup_subtasks: list[dict] = []
    for r in rows:
        kind_val = (r.get("kind") or "spec") if isinstance(r, dict) else "spec"
        row_view = {
            "subtask_id": r["subtask_id"],
            "title": r.get("title") or "",
            "kind": kind_val,
            "state": r["state"],
        }
        if kind_val == "spec":
            if r["state"] == done_state_value:
                done_subtasks.append(row_view)
        else:
            prior_fixup_subtasks.append(row_view)
    return done_subtasks, prior_fixup_subtasks


def build_coverage_gap_addendum(missing: set[str], triage_root_cause: str | None) -> str:
    """Render the `## Coverage gap` re-prompt prefix used by the fixup
    driver after the completeness wrapper flags missing finding ids.

    Lives in this module so `pre_pr.py` stays under the architecture
    line-budget; the rendered string is concatenated with the original
    triage_root_cause and capped at 16k chars by the caller."""
    bullets = "\n".join(f"- `{fid}`" for fid in sorted(missing))
    addendum = (
        "## Coverage gap from your previous attempt\n\n"
        "Your previous plan missed the following finding ids. "
        "Include each one in `findings_addressed` AND in at least "
        "one subtask's stage-typed coverage (`rubric_targets`, "
        "`standards_referenced`, or `behavior_evidence_advanced` "
        f"matching the finding's namespace):\n\n{bullets}\n\n"
        "Re-emit the COMPLETE plan (do not emit only the deltas)."
    )
    return (addendum + "\n\n---\n\n" + (triage_root_cause or ""))[:16000]


__all__ = [
    "build_coverage_gap_addendum",
    "collect_stage_coverage",
    "missing_finding_coverage",
    "parse_and_validate_fixup_plan",
    "split_subtask_rows_for_planner",
]
