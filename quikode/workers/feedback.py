"""Feedback worker mixin (plan 28).

Two entry points, both routed via the daemon's review-watcher tick:

- `run_changes_requested_response(bundled_context)` — fires when a formal
  GitHub Review with `state == "CHANGES_REQUESTED"` lands. The daemon has
  already assembled the bundle (every unresolved thread, every PR-level
  comment, every recent review body). Worker renders the bundle as fixup-
  planner context, runs one fixup-decomposition round, and returns the task
  to PENDING_CI on push.

- `run_ci_fix_response(pr_status)` — fires when GitHub CI flips to FAILURE on
  a post-PR task (whether in PENDING_CI or AWAITING_REVIEW). Worker fetches
  failed-check logs, renders an excerpt, runs one fixup-decomposition round
  scoped to the failures, returns to PENDING_CI on push.

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
from quikode.workers.outcomes import WorkerOutcome


class _TaskWorkerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.workers.task_worker"], name)


_tw = _TaskWorkerGlobals()


class FeedbackWorkerMixin:
    def run_changes_requested_response(self: Any, bundled_context: str) -> WorkerOutcome:
        """Worker entry for daemon-detected `CHANGES_REQUESTED` reviews.

        `bundled_context` is the pre-rendered string from
        `github_graphql.bundle_pr_context`: every unresolved inline thread +
        every PR-level comment + recent reviews (with bodies). Resolved
        threads are excluded by design — a resolved thread is the human's
        "ignore this" signal.

        Lifecycle:
          1. ADDRESSING_FEEDBACK (already entered by the daemon's transition)
          2. provision: reuse worktree, fresh container
          3. fixup-decomposition round (kind=fixup-review) with bundled
             context fed via `review_threads_block`
          4. per-subtask doer/checker/commit gate — each slice commits + pushes
          5. transition back to PENDING_CI
        """
        if not bundled_context.strip():
            _tw.log.warning("run_changes_requested_response called with empty context; nothing to do")
            return WorkerOutcome(State.PENDING_CI, "no context to address")

        try:
            self._provision(provision_worktree=False)
            row = self._row()
            self.plan_text = str(row.get("plan_text") or "")

            round_no = int(_tw.cast(Any, row.get("review_round") or 0)) + 1
            outcome = self._run_fixup_round(
                kind="fixup-review",
                round_no=round_no,
                trigger="review",
                review_threads_block=bundled_context,
            )
            if outcome and outcome.final_state == State.BLOCKED:
                _tw.log.warning(
                    "review response fixup round blocked: %s — returning to PENDING_CI",
                    outcome.note,
                )
                fsm_runtime.enter_pending_ci(
                    self.store,
                    self.node.id,
                    note=f"review response fixup blocked: {outcome.note[:200]}",
                )
                return WorkerOutcome(
                    State.PENDING_CI,
                    f"review response fixup blocked: {outcome.note[:200]}",
                )

            self.store.increment_review_round(self.node.id)
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
        """Worker entry mode for daemon-detected post-PR CI failures.

        Routed from PENDING_CI (CI failed before any review) or
        AWAITING_REVIEW (CI flaked red after passing). Re-uses the fixup-
        decomposition path with kind='fixup-ci' and the failure log as the
        trigger context.
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
            # Plan 53: capture local-CI-at-head before invoking the
            # fixup planner so the planner sees the local-vs-CI signal.
            local_ci_at_head = self._capture_local_ci_at_head()
            outcome = self._run_fixup_round(
                kind="fixup-ci",
                round_no=round_no,
                trigger="ci",
                ci_excerpt=ci_excerpt,
                local_ci_at_head=local_ci_at_head,
            )
            if outcome and outcome.final_state == State.BLOCKED:
                _tw.log.warning(
                    "ci-fix fixup round blocked: %s — returning to PENDING_CI",
                    outcome.note,
                )
                fsm_runtime.enter_pending_ci(
                    self.store,
                    self.node.id,
                    note=f"ci-fix fixup blocked: {outcome.note[:200]}",
                )
                return WorkerOutcome(
                    State.PENDING_CI,
                    f"ci-fix fixup blocked: {outcome.note[:200]}",
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
