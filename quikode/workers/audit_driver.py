"""Plan 58: unified audit-cycle driver.

Hosts the consolidation of three pre-plan-58 worker drivers
(`_run_pre_pr_pipeline`, `_run_ci_fix_response`,
`run_changes_requested_response`) into ONE `_run_audit_cycle` method that
walks the 5-stage gauntlet (AUDIT_LOCAL_CI → AUDIT_RUBRIC → AUDIT_STANDARDS
→ AUDIT_ARCHITECTURE → AUDIT_BEHAVIOR) regardless of trigger source.

The trigger source determines:
  - the OUTER wrapping state transition (push + open PR vs. push + back
    to PENDING_CI) — handled by callers of `_run_audit_cycle`
  - the PHASE row stamping (INITIAL → PRE_PR_REVIEW at first audit start;
    PR_REVIEW cycle bump at CI_FAILURE / REVIEW_FEEDBACK entries)

Lives outside `pre_pr.py` so that module stays under the 600-line cap.
"""

from __future__ import annotations

import sys
from enum import StrEnum
from typing import Any

from quikode import fsm_runtime, runtime_shutdown
from quikode.state import State
from quikode.state_types import Phase, PrReviewTrigger
from quikode.workers.outcomes import WorkerOutcome
from quikode.workers.pre_pr_reports import (
    DEFERRED_PRE_PR_FINDINGS_ARTIFACT,
    release_valve_report,
    structural_failure_report,
)


class _TaskWorkerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.workers.task_worker"], name)


_tw = _TaskWorkerGlobals()


class AuditTriggerSource(StrEnum):
    """Plan 58: which event drove this audit cycle. Drives the OUTER
    wrapping behavior; the INNER 5-stage gauntlet is identical across all
    three sources."""

    INITIAL_AUDIT = "initial_audit"
    CI_FAILURE = "ci_failure"
    REVIEW_FEEDBACK = "review_feedback"


def audit_cycle_prologue(worker: Any, trigger_source: AuditTriggerSource) -> None:
    """Pre-loop setup for the unified audit driver.

    Drives the phase wire-up (INITIAL → PRE_PR_REVIEW on first audit
    start) and the trigger-source-aware FSM entry (CI_FIXUP_START /
    REVIEW_FIXUP_START from a post-PR state)."""
    if trigger_source is AuditTriggerSource.INITIAL_AUDIT:
        maybe_enter_pre_pr_review_phase(worker)
        return
    if trigger_source is AuditTriggerSource.CI_FAILURE:
        fsm_runtime.enter_audit_cycle_for_ci_fixup(
            worker.store,
            worker.node.id,
            note="post-PR CI failure: entering unified audit cycle",
        )
        return
    if trigger_source is AuditTriggerSource.REVIEW_FEEDBACK:
        fsm_runtime.enter_audit_cycle_for_review_fixup(
            worker.store,
            worker.node.id,
            note="post-PR CHANGES_REQUESTED: entering unified audit cycle",
        )


def maybe_enter_pre_pr_review_phase(worker: Any) -> None:
    """Plan 58: fire INITIAL → PRE_PR_REVIEW once."""
    try:
        row = worker._row()
        if str(row.get("phase") or "initial") == "initial":
            worker.store.enter_phase(
                worker.node.id,
                Phase.PRE_PR_REVIEW,
                cycle_in_phase=1,
                pr_review_trigger=PrReviewTrigger.NONE,
                note="initial subtasks done; entering PRE_PR_REVIEW phase",
            )
    except Exception as exc:
        _tw.log.warning("phase enter PRE_PR_REVIEW failed: %s; continuing", exc)


def run_audit_cycle(
    worker: Any,
    *,
    trigger_source: AuditTriggerSource,
    merge_node_mode: bool = False,
) -> WorkerOutcome | None:
    """Unified driver for the 5-stage audit gauntlet across all three
    trigger sources.

    Returns None on clean settle (caller drives the outer wrapping) or a
    BLOCKED outcome when the audit budget exhausts.
    """
    _tw.log.info("task %s: starting audit cycle (trigger=%s)", worker.node.id, trigger_source.value)
    audit_cycle_prologue(worker, trigger_source)
    resume_summary = worker._resumable_pre_pr_audit_summary()
    start_cycle = int(resume_summary["cycle"]) if resume_summary else 1
    for cycle in range(start_cycle, worker.cfg.pre_pr_audit_max_cycles + 1):
        outcome = _run_one_audit_cycle(
            worker,
            cycle=cycle,
            trigger_source=trigger_source,
            merge_node_mode=merge_node_mode,
            resume_summary=resume_summary,
        )
        if outcome is _AUDIT_CYCLE_PASSED:
            return None
        if outcome is _AUDIT_CYCLE_CONTINUE:
            resume_summary = None
            continue
        # Anything else is a WorkerOutcome (block, shutdown).
        return outcome

    note = (
        f"pre-PR audit pipeline exhausted {worker.cfg.pre_pr_audit_max_cycles} "
        "cycle(s) without a clean pass — manual review required"
    )
    fsm_runtime.block_current(worker.store, worker.node.id, note=note, last_error=note[:1000])
    return WorkerOutcome(State.BLOCKED, note)


# Sentinel objects for the inner-loop dispatch.
_AUDIT_CYCLE_PASSED = object()
_AUDIT_CYCLE_CONTINUE = object()


def _run_one_audit_cycle(
    worker: Any,
    *,
    cycle: int,
    trigger_source: AuditTriggerSource,
    merge_node_mode: bool,
    resume_summary: dict[str, Any] | None,
) -> Any:
    """Run a single audit cycle. Returns one of the sentinel objects
    (`_AUDIT_CYCLE_PASSED` / `_AUDIT_CYCLE_CONTINUE`) or a `WorkerOutcome`.
    Extracted from `run_audit_cycle` to keep both functions under the
    branch-count cap."""
    _tw.log.info(
        "task %s: audit cycle %d/%d (trigger=%s)",
        worker.node.id,
        cycle,
        worker.cfg.pre_pr_audit_max_cycles,
        trigger_source.value,
    )
    cycle_resume_summary, bootstrap_outcome = worker._enter_audit_cycle(cycle, resume_summary)
    if bootstrap_outcome is not None:
        return bootstrap_outcome
    if trigger_source is AuditTriggerSource.INITIAL_AUDIT and not worker._pre_pr_stage_passed(
        cycle_resume_summary, "local_ci"
    ):
        fsm_runtime.enter_local_ci_checking(
            worker.store,
            worker.node.id,
            note=f"audit cycle {cycle}: local-ci ({worker.cfg.local_ci_command})",
        )

    diff_excerpt = worker._compute_branch_diff_excerpt()
    plan_text = str(worker._row().get("plan_text") or "")

    try:
        stages = worker._execute_audit_stages(
            cycle=cycle,
            diff_excerpt=diff_excerpt,
            plan_text=plan_text,
            merge_node_mode=merge_node_mode,
            resume_summary=cycle_resume_summary,
        )
    except runtime_shutdown.ShutdownRequested:
        note = "shutdown requested during pre-pr audit; discarding partial audit result"
        _tw.log.info("task %s: %s", worker.node.id, note)
        return WorkerOutcome(fsm_runtime.current_state(worker.store, worker.node.id), note)
    cycle_result = _tw.pre_pr_audit.PipelineCycleResult(cycle=cycle, stages=stages)
    for s in cycle_result.stages:
        _tw.log.info(
            "task %s pre-pr cycle %d stage `%s`: %s",
            worker.node.id,
            cycle,
            s.name,
            "PASS" if s.passed else "FAIL",
        )

    settled = worker._settle_pre_pr_cycle(cycle, cycle_result)
    if settled is not None:
        # Plan 58: clean settle exits at AUDIT_BEHAVIOR. Spec tasks advance
        # to PR_OPENING via audit_behavior_passed. Merge-nodes skip this
        # — the merge_node_worker fires MERGE_NODE_BUILT directly.
        if settled[0] is None and not merge_node_mode:
            fsm_runtime.audit_behavior_passed(
                worker.store, worker.node.id, note=f"audit cycle {cycle} passed cleanly"
            )
        return settled[0] if settled[0] is not None else _AUDIT_CYCLE_PASSED

    fixup_outcome = _drive_fixup_round_for_cycle(worker, cycle=cycle, cycle_result=cycle_result)
    if fixup_outcome is not None:
        return fixup_outcome
    return _AUDIT_CYCLE_CONTINUE


def _drive_fixup_round_for_cycle(worker: Any, *, cycle: int, cycle_result: Any) -> WorkerOutcome | None:
    """Plan 58: failure-path fixup driving. Renders the findings block,
    augments with the required-coverage instruction, invokes the fixup
    planner round."""
    fsm_runtime.enter_fixup_planning(
        worker.store,
        worker.node.id,
        note=f"audit cycle {cycle} failed: " + ", ".join(s.name for s in cycle_result.failed_stages),
    )
    findings_block = _tw.pre_pr_audit.merge_failed_stage_reports(cycle_result.failed_stages)
    expected_finding_ids = _tw.pre_pr_audit.collect_finding_ids(cycle_result.failed_stages)
    worker.store.add_artifact(worker.node.id, f"pre_pr_audit:cycle_{cycle}", findings_block)
    if expected_finding_ids:
        augmented = (
            "## Required finding coverage\n\n"
            "Every id below MUST appear in your output's "
            "`findings_addressed` array AND be addressed by at "
            "least one subtask's stage-typed coverage "
            "(`rubric_targets`, `standards_referenced`, "
            "`architecture_referenced`, or `behavior_evidence_advanced` "
            "matching the finding's namespace). The per-subtask `addresses_findings` "
            "field is gone (Plan 33 D2). Dropping ids is forbidden.\n\n"
            + "\n".join(f"- `{fid}`" for fid in expected_finding_ids)
            + "\n\n---\n\n"
            + findings_block
        )
    else:
        augmented = findings_block
    outcome = worker._run_fixup_round(
        kind="fixup-pre-pr-audit",
        round_no=cycle,
        trigger="pre_pr_audit",
        triage_root_cause=augmented[:16000],
        expected_finding_ids=expected_finding_ids,
    )
    if outcome and outcome.final_state == State.BLOCKED:
        return outcome
    return None


__all__ = [
    "DEFERRED_PRE_PR_FINDINGS_ARTIFACT",
    "AuditTriggerSource",
    "audit_cycle_prologue",
    "maybe_enter_pre_pr_review_phase",
    "release_valve_report",
    "run_audit_cycle",
    "structural_failure_report",
]
