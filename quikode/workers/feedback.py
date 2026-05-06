"""Feedback worker mixin."""

from __future__ import annotations

import sys
from typing import Any

from quikode import fsm_runtime
from quikode.github_graphql import ReviewThread
from quikode.state import State
from quikode.workers.outcomes import WorkerOutcome


class _TaskWorkerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.workers.task_worker"], name)


_tw = _TaskWorkerGlobals()


class FeedbackWorkerMixin:
    def run_review_response(self: Any, threads_to_address: list[ReviewThread]) -> WorkerOutcome:
        """Alternate worker entry mode for v3 Phase B review responses.

        Submitted to the worker pool by the daemon when its review-watcher
        pass detects unresolved review threads on an PENDING_CI task.
        Skips planning + the spec subtask loop + new-worktree provisioning;
        spins up a fresh container against the existing worktree, lets the
        fixup planner decompose the threads into per-thread mini-subtasks,
        drives them through the per-subtask doer/checker/commit gate (each
        thread's fix lands as its own commit + push on the PR branch),
        resolves the threads, and returns the task to PENDING_CI.

        Lifecycle (humans drive cadence — no per-task retry budget):
          1. PROVISIONING → reuse worktree, fresh container
          2. ADDRESSING_FEEDBACK → fixup planner (kind=fixup-review)
          3. FIXUP_PLANNING → per-thread subtask plan emitted
          4. DOING_SUBTASK / CHECKING_SUBTASK loop, one slice per thread
             (each slice commits + pushes via per-subtask gate)
          5. resolve threads via GraphQL
          6. transition back to PENDING_CI
        """
        if not threads_to_address:
            _tw.log.warning("run_review_response called with empty thread list; nothing to do")
            return WorkerOutcome(State.PENDING_CI, "no threads to address")

        try:
            # 1. provision container against existing worktree
            fsm_runtime.enter_addressing_feedback(
                self.store,
                self.node.id,
                note=f"addressing {len(threads_to_address)} review thread(s)",
            )
            self._provision(provision_worktree=False)

            # Re-hydrate plan_text from the row so the fixup-planner prompt
            # has spec context. Resume plan if available — otherwise fall
            # back to "" which the prompts handle.
            row = self._row()
            self.plan_text = str(row.get("plan_text") or "")

            # Render the threads as a block the fixup planner can consume.
            # Each line = author + path:line + truncated body, so the planner
            # can scope each emitted subtask to one thread's file/line.
            thread_lines = []
            for i, t in enumerate(threads_to_address, 1):
                path_line = f"{t.path or '(no path)'}:{t.line or '?'}"
                body = (t.last_comment_body or "").strip().replace("\n", " ")
                if len(body) > 400:
                    body = body[:400] + "…"
                thread_lines.append(
                    f"{i}. [{path_line}] (by {t.last_comment_author}, "
                    f"bot={'yes' if t.last_comment_is_bot else 'no'}): {body}"
                )
            review_threads_block = "\n".join(thread_lines)

            # 2-4. v3 fixup decomposition: plan + run per-thread mini-subtasks.
            # Each lands as its own commit on the PR branch via the per-subtask
            # commit gate, replacing the old monolithic _do(attempt=300)
            # monolith that historically ran 30-60 min on a small set of
            # threads with shaky convergence.
            round_no = int(_tw.cast(Any, row.get("review_round") or 0)) + 1
            outcome = self._run_fixup_round(
                kind="fixup-review",
                round_no=round_no,
                trigger="review",
                review_threads_block=review_threads_block,
            )
            if outcome and outcome.final_state == State.BLOCKED:
                # Don't surface BLOCKED to the orchestrator — review response
                # is human-driven; let the operator see the partial progress
                # and re-trigger via a fresh thread or a manual retry.
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

            # 5. resolve threads (best-effort). Use the latest commit sha on
            # the branch as the addressed-in marker — the per-subtask commit
            # gate has already pushed each thread's slice.
            commit_sha = self._latest_commit_sha_on_branch()
            for t in threads_to_address:
                try:
                    ok = _tw.github_graphql.resolve_thread(t.thread_id)
                except Exception as e:
                    _tw.log.warning("resolve_thread %s raised: %s", t.thread_id, e)
                    ok = False
                if not ok:
                    _tw.log.warning("resolve_thread %s returned False; continuing", t.thread_id)
                if commit_sha:
                    self.store.upsert_review_thread(
                        self.node.id,
                        thread_id=t.thread_id,
                        is_resolved=t.is_resolved,
                        last_comment_ts=t.last_comment_created_at,
                        last_comment_author=t.last_comment_author,
                        last_comment_is_bot=t.last_comment_is_bot,
                    )
                    self.store.mark_thread_addressed(self.node.id, t.thread_id, commit_sha)

            # 8. counters + transition
            self.store.increment_review_round(self.node.id)
            fsm_runtime.enter_pending_ci(
                self.store,
                self.node.id,
                note=f"responded to {len(threads_to_address)} thread(s)",
            )
            return WorkerOutcome(
                State.PENDING_CI,
                f"responded to {len(threads_to_address)} threads",
            )
        except Exception as e:
            _tw.log.exception("review response for task %s crashed", self.node.id)
            # Don't FAIL the task — return it to PENDING_CI so humans
            # can intervene without losing the existing PR.
            fsm_runtime.enter_pending_ci(
                self.store,
                self.node.id,
                note=f"review response crashed: {e}",
                last_error=str(e)[:1000],
            )
            return WorkerOutcome(State.PENDING_CI, f"review response crashed: {e}")
        finally:
            # Tear down container only — keep the worktree so subsequent
            # response cycles (or merge) can reuse it.
            if self.handle is not None:
                _tw.docker_env.teardown(self._h)
                self.handle = None

    def run_ci_fix_response(self: Any, pr_status: _tw.github.PRStatus) -> WorkerOutcome:
        """Worker entry mode for daemon-detected post-merge CI failures.

        When GitHub's CI flips to FAILURE while the task is in
        PENDING_CI (typically because a review-response push landed
        and re-triggered CI which then failed), the daemon dispatches
        this worker. We re-use the fixup-decomposition path with
        kind='fixup-ci' and the failure log as the trigger context.

        Critical for unattended operation: without this path, a CI
        failure post-AWAITING-MERGE leaves the task stuck indefinitely
        until an operator notices.
        """
        try:
            fsm_runtime.enter_addressing_feedback(
                self.store,
                self.node.id,
                note=f"addressing CI failure ({len(pr_status.failed_checks)} failed check(s))",
            )
            self._provision(provision_worktree=False)
            row = self._row()
            self.plan_text = str(row.get("plan_text") or "")

            # Fetch the failed-check log excerpts for the fixup planner.
            try:
                ci_log = _tw.github.fetch_failed_check_logs(self.cfg.repo_path, int(pr_status.number))
            except Exception as e:
                _tw.log.warning("fetch_failed_check_logs raised: %s — using minimal context", e)
                ci_log = "\n".join(f"failed: {c.get('name', '<unknown>')}" for c in pr_status.failed_checks)
            ci_excerpt = _tw._last_lines(ci_log, 80)

            # ci_triage_retries is the cumulative count for this task,
            # used as the round_no so successive CI failures get distinct
            # subtask ID prefixes (F-1-1-..., F-2-1-..., etc).
            round_no = int(_tw.cast(Any, row.get("ci_triage_retries") or 0)) + 1
            self.store.increment(self.node.id, "ci_triage_retries")
            outcome = self._run_fixup_round(
                kind="fixup-ci",
                round_no=round_no,
                trigger="ci",
                ci_excerpt=ci_excerpt,
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

            # All fixup subtasks settled (per-subtask commits already
            # pushed). Return to PENDING_CI; GitHub will re-run CI
            # against the new commits and the daemon's next poll picks
            # up either CI-pass or another failure.
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
                _tw.docker_env.teardown(self._h)
                self.handle = None

    def _commit_and_push_response(self: Any) -> _tw.worktree.CommitResult:
        """Commit + push the in-flight worktree edits for a review response."""
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
