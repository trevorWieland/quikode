"""Plan 33 PR-B: stage-typed coverage check for the fixup planner.

Used by `quikode.workers.pre_pr.PrePrWorkerMixin._run_fixup_round` to
verify the fixup planner's `FixupPlan` covers every audit finding id.
The retired `addresses_findings` per-subtask field (Plan 33 D2) is
replaced by a union over `rubric_targets`, `standards_referenced`,
`behavior_evidence_advanced`, plus the plan-level `findings_addressed`
array, plus a `notes`-text fallback for finding ids the planner
groups into one subtask.

Lives in its own module to keep `pre_pr.py` under the 600-line budget.
"""

from __future__ import annotations

from quikode.subtask_schema import FixupPlan


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


__all__ = ["collect_stage_coverage", "missing_finding_coverage"]
