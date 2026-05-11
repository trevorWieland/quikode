"""Feedback worker mixin (plan 28 / plan 58).

Two entry points, both routed via the daemon's review-watcher tick. Plan 58
collapsed the divergent fixup drivers into a single
`_run_audit_cycle(trigger_source)` method on `PrePrWorkerMixin`; these
entry points now delegate to that driver and own the OUTER wrapping
(provision → run driver → return to PENDING_CI):

- `run_changes_requested_response(bundled_context)` — fires when a formal
  GitHub Review with `state == "CHANGES_REQUESTED"` lands. The daemon has
  already assembled the bundle (every unresolved thread, every PR-level
  comment, every recent review body). Worker delegates to
  `_run_audit_cycle(trigger_source=REVIEW_FEEDBACK)` and returns the task
  to PENDING_CI when the gauntlet settles.

- `run_ci_fix_response(pr_status)` — fires when GitHub CI flips to FAILURE
  on a post-PR task (whether in PENDING_CI or AWAITING_REVIEW). Worker
  delegates to `_run_audit_cycle(trigger_source=CI_FAILURE)` and returns
  to PENDING_CI on settle.

Plan 28 deletions: per-thread tracking (`mark_thread_addressed`,
`addressed_in_commit_sha`) and the pre-dispatch sonnet classifier are gone.
Threads remain visible in the bundled context — resolved threads excluded
since "human dismissed it" = ignore.
"""

from __future__ import annotations

import sys
from typing import Any

from quikode import fsm_runtime
from quikode.state import State
from quikode.state_types import PrReviewTrigger
from quikode.workers.outcomes import WorkerOutcome
from quikode.workers.pre_pr import AuditTriggerSource


class _TaskWorkerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.workers.task_worker"], name)


_tw = _TaskWorkerGlobals()


class FeedbackWorkerMixin:
    def run_changes_requested_response(self: Any, bundled_context: str) -> WorkerOutcome:
        """Plan 58 entry for daemon-detected `CHANGES_REQUESTED` reviews.

        `bundled_context` is the pre-rendered string from
        `github_graphql.bundle_pr_context`. Plan 58 delegates the entire
        fixup pipeline to the unified `_run_audit_cycle` driver; this
        method owns provisioning + the outer return-to-PENDING_CI
        wrapping only.
        """
        if not bundled_context.strip():
            _tw.log.warning("run_changes_requested_response called with empty context; nothing to do")
            return WorkerOutcome(State.PENDING_CI, "no context to address")

        try:
            self._provision(provision_worktree=False)
            row = self._row()
            self.plan_text = str(row.get("plan_text") or "")
            round_no = int(_tw.cast(Any, row.get("review_round") or 0)) + 1

            # Plan 58: stamp PR_REVIEW phase + bump cycle_in_phase with
            # the REVIEW_FEEDBACK trigger so the operator sees lifecycle
            # depth in the TUI.
            try:
                self.store.increment_cycle_in_phase(
                    self.node.id,
                    pr_review_trigger=PrReviewTrigger.REVIEW_FEEDBACK,
                    note=f"PR_REVIEW cycle: CHANGES_REQUESTED round {round_no}",
                )
            except Exception as exc:
                _tw.log.warning("increment_cycle_in_phase raised %s; continuing", exc)

            # Stash the bundled context for the fixup planner to read on
            # the wrapper invocation below.
            self._latest_review_threads_block = bundled_context

            outcome = self._run_audit_cycle_with_review_context(
                bundled_context=bundled_context,
                round_no=round_no,
            )
            if outcome and outcome.final_state == State.BLOCKED:
                _tw.log.warning(
                    "review response audit cycle blocked: %s — returning to PENDING_CI",
                    outcome.note,
                )
                fsm_runtime.enter_pending_ci(
                    self.store,
                    self.node.id,
                    note=f"review response blocked: {outcome.note[:200]}",
                )
                return WorkerOutcome(
                    State.PENDING_CI,
                    f"review response blocked: {outcome.note[:200]}",
                )

            self.store.increment_review_round(self.node.id)
            # The audit driver fired AUDIT_BEHAVIOR_PASSED on a clean
            # settle, advancing to PR_OPENING. `_open_pr` reuses the
            # existing PR; then `_poll_pr_loop`'s `enter_pending_ci`
            # lands us back at PENDING_CI. For this method we just
            # ensure PENDING_CI is reached:
            fsm_runtime.enter_pending_ci(
                self.store,
                self.node.id,
                note=f"responded to CHANGES_REQUESTED (round {round_no})",
            )
            return WorkerOutcome(
                State.PENDING_CI,
                f"changes-requested response round {round_no} complete",
            )
        except Exception as e:
            _tw.log.exception("changes-requested response for task %s crashed", self.node.id)
            fsm_runtime.enter_pending_ci(
                self.store,
                self.node.id,
                note=f"changes-requested response crashed: {e}",
                last_error=str(e)[:1000],
            )
            return WorkerOutcome(State.PENDING_CI, f"changes-requested response crashed: {e}")
        finally:
            if self.handle is not None:
                self.execution_backend.teardown(self._h)
                self.handle = None

    def run_ci_fix_response(self: Any, pr_status: _tw.github.PRStatus) -> WorkerOutcome:
        """Plan 58 entry for daemon-detected post-PR CI failures.

        Routed from PENDING_CI (CI failed before any review) or
        AWAITING_REVIEW (CI flaked red after passing). Delegates to
        `_run_audit_cycle(trigger_source=CI_FAILURE)`.
        """
        try:
            self._provision(provision_worktree=False)
            row = self._row()
            self.plan_text = str(row.get("plan_text") or "")

            try:
                ci_log = _tw.github.fetch_failed_check_logs(self.cfg.repo_path, int(pr_status.number))
            except Exception as e:
                _tw.log.warning("fetch_failed_check_logs raised: %s — using minimal context", e)
                ci_log = "\n".join(f"failed: {c.get('name', '<unknown>')}" for c in pr_status.failed_checks)
            ci_excerpt = _tw._last_lines(ci_log, 80)
            round_no = int(_tw.cast(Any, row.get("ci_triage_retries") or 0)) + 1
            self.store.increment(self.node.id, "ci_triage_retries")

            # Plan 58: stamp PR_REVIEW phase + bump cycle_in_phase with
            # the CI_FAILURE trigger.
            try:
                self.store.increment_cycle_in_phase(
                    self.node.id,
                    pr_review_trigger=PrReviewTrigger.CI_FAILURE,
                    note=f"PR_REVIEW cycle: CI failure round {round_no}",
                )
            except Exception as exc:
                _tw.log.warning("increment_cycle_in_phase raised %s; continuing", exc)

            local_ci_at_head = self._capture_local_ci_at_head()
            self._latest_ci_excerpt = ci_excerpt
            self._latest_local_ci_at_head = local_ci_at_head

            outcome = self._run_audit_cycle_with_ci_context(
                ci_excerpt=ci_excerpt,
                local_ci_at_head=local_ci_at_head,
                round_no=round_no,
            )
            if outcome and outcome.final_state == State.BLOCKED:
                _tw.log.warning(
                    "ci-fix audit cycle blocked: %s — returning to PENDING_CI",
                    outcome.note,
                )
                fsm_runtime.enter_pending_ci(
                    self.store,
                    self.node.id,
                    note=f"ci-fix blocked: {outcome.note[:200]}",
                )
                return WorkerOutcome(
                    State.PENDING_CI,
                    f"ci-fix blocked: {outcome.note[:200]}",
                )

            fsm_runtime.enter_pending_ci(
                self.store,
                self.node.id,
                note=f"ci-fix round {round_no} pushed {len(pr_status.failed_checks)} fix slice(s)",
            )
            return WorkerOutcome(
                State.PENDING_CI,
                f"ci-fix round {round_no} complete",
            )
        except Exception as e:
            _tw.log.exception("ci-fix for task %s crashed", self.node.id)
            fsm_runtime.enter_pending_ci(
                self.store,
                self.node.id,
                note=f"ci-fix crashed: {e}",
                last_error=str(e)[:1000],
            )
            return WorkerOutcome(State.PENDING_CI, f"ci-fix crashed: {e}")
        finally:
            if self.handle is not None:
                self.execution_backend.teardown(self._h)
                self.handle = None

    def _run_audit_cycle_with_review_context(
        self: Any, *, bundled_context: str, round_no: int
    ) -> WorkerOutcome | None:
        """Thin wrapper: invoke `_run_fixup_round` once with the review
        context so the planner sees it, then drop into the unified audit
        cycle. The fixup_round handles per-subtask doer/checker/triage
        loops; the audit cycle handles re-running the gauntlet to verify
        the fix landed cleanly.

        Plan 58: post-PR fixup cycles re-audit the result (closes the
        gap §5 of the audit identified).
        """
        # Run one fixup round with the bundled review context.
        outcome = self._run_fixup_round(
            kind="fixup-review",
            round_no=round_no,
            trigger="review",
            review_threads_block=bundled_context,
        )
        if outcome and outcome.final_state == State.BLOCKED:
            return outcome
        # Re-run the gauntlet to verify the fix; trigger source flows
        # through to the OUTER wrapping.
        return self._run_audit_cycle(trigger_source=AuditTriggerSource.REVIEW_FEEDBACK)

    def _run_audit_cycle_with_ci_context(
        self: Any,
        *,
        ci_excerpt: str,
        local_ci_at_head: tuple[bool, str] | None,
        round_no: int,
    ) -> WorkerOutcome | None:
        """Thin wrapper for the CI-failure path. Same shape as
        `_run_audit_cycle_with_review_context` but with CI-failure
        context instead of review threads."""
        outcome = self._run_fixup_round(
            kind="fixup-ci",
            round_no=round_no,
            trigger="ci",
            ci_excerpt=ci_excerpt,
            local_ci_at_head=local_ci_at_head,
        )
        if outcome and outcome.final_state == State.BLOCKED:
            return outcome
        return self._run_audit_cycle(trigger_source=AuditTriggerSource.CI_FAILURE)

    def _commit_and_push_response(self: Any) -> _tw.worktree.CommitResult:
        """Commit + push the in-flight worktree edits for a feedback response."""
        row = self._row()
        branch = str(row.get("branch") or "")
        msg = f"{self.node.id}: address review feedback"
        return _tw.worktree.commit_response(
            self._h,
            msg,
            branch=branch,
            remote=self.cfg.pr_remote,
            push=True,
            log_path=self.log_path,
        )
