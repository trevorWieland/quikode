"""Post-PR review and CI watcher mixin."""

from __future__ import annotations

import sys
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from quikode import fsm_runtime
from quikode.github_graphql import ReviewThread
from quikode.state import State, TaskRow


class _RunnerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.orchestration.runner"], name)


_rt = _RunnerGlobals()


class ReviewWatchMixin:
    def _poll_review_threads(
        self: Any,
        pool: ThreadPoolExecutor,
        futures: dict[str, Future],
        review_response_futures: set[str],
    ) -> None:
        """One review-watcher tick.

        For every post-PR task whose last poll was older than
        `cfg.review_poll_interval_s`:

        1. Check the PR state via `gh pr view`. MERGED → transition to
           MERGED. CLOSED → transition to ABORTED. Skip review-thread
           polling for either terminal state.
        2. Fetch live review threads via GraphQL. Diff against the stored
           `review_threads` table to determine which threads need
           addressing.
        3. Bump `last_review_poll_ts` so the throttle window is honored.
        4. If any threads need addressing AND the worker pool has slack,
           submit a `run_review_response` future. The task transitions to
           ADDRESSING_FEEDBACK synchronously before submit so the TUI /
           pick-next loop see the new state immediately.
        """
        now = _rt.time.time()
        cutoff = now - self.cfg.review_poll_interval_s
        candidates = self.store.tasks_needing_review_poll(cutoff=cutoff)
        for task_row in candidates:
            self._poll_review_candidate(task_row, now, pool, futures, review_response_futures)

    def _poll_review_candidate(
        self: Any,
        task_row: TaskRow,
        now: float,
        pool: ThreadPoolExecutor,
        futures: dict[str, Future],
        review_response_futures: set[str],
    ) -> None:
        task_id = task_row["id"]
        if task_id in futures:
            return
        pr_info = self._review_pr_info(task_row, now)
        if pr_info is None:
            return
        pr_number, repo = pr_info
        pr_status = _rt.github.poll_pr(self.cfg.repo_path, int(pr_number))
        self._maybe_schedule_cascade_for_push(task_row, pr_status, pool, futures, review_response_futures)
        if self._handle_pre_thread_pr_signal(
            task_row, pr_status, now, pool, futures, review_response_futures
        ):
            return
        threads = self._fetch_review_threads(repo, pr_number)
        to_address = self._classify_threads(task_id, threads)
        task_row = self._apply_post_pr_poll_state(task_row, pr_status, threads)
        self.store.set_field(task_id, last_review_poll_ts=now)
        if self._handle_review_threads(
            repo, pr_number, task_row, to_address, now, pool, futures, review_response_futures
        ):
            return
        self._post_clean_review_followups(task_row, pr_status, threads)

    def _review_pr_info(self: Any, task_row: TaskRow, now: float) -> tuple[int, str] | None:
        task_id = task_row["id"]
        pr_number = task_row.get("pr_number")
        repo = self._repo_identifier(task_row) if pr_number else None
        if pr_number and repo:
            return int(pr_number), repo
        if pr_number and not repo:
            _rt.log.warning(
                "task %s: cannot derive repo identifier from pr_url; skipping review poll", task_id
            )
        self.store.set_field(task_id, last_review_poll_ts=now)
        return None

    def _handle_pre_thread_pr_signal(
        self: Any,
        task_row: TaskRow,
        pr_status: _rt.github.PRStatus,
        now: float,
        pool: ThreadPoolExecutor,
        futures: dict[str, Future],
        review_response_futures: set[str],
    ) -> bool:
        return (
            self._handle_terminal_pr_status(task_row, pr_status, now, pool, futures, review_response_futures)
            or self._handle_post_pr_rebase_signal(
                task_row, pr_status, now, pool, futures, review_response_futures
            )
            or self._handle_post_pr_ci_failure(
                task_row, pr_status, now, pool, futures, review_response_futures
            )
        )

    def _handle_review_threads(
        self: Any,
        repo: str,
        pr_number: int,
        task_row: TaskRow,
        to_address: list[ReviewThread],
        now: float,
        pool: ThreadPoolExecutor,
        futures: dict[str, Future],
        review_response_futures: set[str],
    ) -> bool:
        if self._block_if_review_rounds_exhausted(task_row, to_address, now):
            return True
        to_address, task_row = self._triage_review_threads(repo, pr_number, task_row, to_address)
        return self._dispatch_review_response(task_row, to_address, pool, futures, review_response_futures)

    def _maybe_schedule_cascade_for_push(
        self: Any,
        task_row: TaskRow,
        pr_status: _rt.github.PRStatus,
        pool: ThreadPoolExecutor,
        futures: dict[str, Future],
        review_response_futures: set[str],
    ) -> None:
        task_id = task_row["id"]
        branch = str(task_row.get("branch") or "")
        if pr_status.state != "OPEN" or not pr_status.head_sha or not branch:
            return
        last_seen = self.store.get_last_observed_branch_tip_sha(task_id)
        if last_seen and last_seen != pr_status.head_sha:
            _rt.log.info(
                "task %s: branch %s tip advanced %s → %s; scheduling cascade rebase for descendants",
                task_id,
                branch,
                last_seen[:8],
                pr_status.head_sha[:8],
            )
            self._schedule_cascade_rebase(branch, pool, futures, review_response_futures)
        self.store.set_last_observed_branch_tip_sha(task_id, pr_status.head_sha)

    def _handle_terminal_pr_status(
        self: Any,
        task_row: TaskRow,
        pr_status: _rt.github.PRStatus,
        now: float,
        pool: ThreadPoolExecutor,
        futures: dict[str, Future],
        review_response_futures: set[str],
    ) -> bool:
        task_id = task_row["id"]
        if pr_status.state == "MERGED":
            parent_branch = str(task_row.get("branch") or "")
            fsm_runtime.mark_merged(self.store, task_id, note="merged on github")
            self.store.set_field(task_id, last_review_poll_ts=now)
            if parent_branch:
                self._schedule_rebases_for_merged_parent(
                    parent_branch, pool, futures, review_response_futures
                )
            return True
        if pr_status.state != "CLOSED":
            return False
        return self._handle_closed_pr(task_row, pr_status, now, pool, futures, review_response_futures)

    def _handle_closed_pr(
        self: Any,
        task_row: TaskRow,
        pr_status: _rt.github.PRStatus,
        now: float,
        pool: ThreadPoolExecutor,
        futures: dict[str, Future],
        review_response_futures: set[str],
    ) -> bool:
        task_id = task_row["id"]
        pr_base_ref = pr_status.base_ref_name or ""
        if self._closed_pr_needs_rebase(task_id, pr_base_ref):
            _rt.log.info(
                "task %s: PR #%s auto-closed — base %s deleted by parent merge; scheduling rebase-to-main + re-PR",
                task_id,
                task_row.get("pr_number"),
                pr_base_ref,
            )
            self.store.set_field(task_id, last_review_poll_ts=now)
            self._schedule_rebase_to_main(
                task_id, pool, futures, review_response_futures, trigger_reason="parent_merge_auto_close"
            )
            return True
        parent_branch = str(task_row.get("branch") or "")
        fsm_runtime.pr_closed(self.store, task_id, note="closed without merge")
        self.store.set_field(task_id, last_review_poll_ts=now)
        self._clear_stranded_children(task_id, parent_branch)
        return True

    def _closed_pr_needs_rebase(self: Any, task_id: str, pr_base_ref: str) -> bool:
        return bool(
            self.store.get_parent_branches(task_id)
            and pr_base_ref
            and pr_base_ref != self.cfg.base_branch
            and not self._remote_branch_exists(pr_base_ref)
        )

    def _clear_stranded_children(self: Any, task_id: str, parent_branch: str) -> None:
        if not parent_branch:
            return
        stranded = self.store.children_with_parent_branch(parent_branch)
        for child in stranded:
            self.store.clear_parent_branch(str(child["id"]))
        if stranded:
            _rt.log.info(
                "parent %s closed without merge → cleared parent_pr_branch on %d child(ren)",
                task_id,
                len(stranded),
            )

    def _handle_post_pr_rebase_signal(
        self: Any,
        task_row: TaskRow,
        pr_status: _rt.github.PRStatus,
        now: float,
        pool: ThreadPoolExecutor,
        futures: dict[str, Future],
        review_response_futures: set[str],
    ) -> bool:
        task_id = task_row["id"]
        if pr_status.mergeable != "CONFLICTING" or task_id in futures:
            return False
        _rt.log.info(
            "task %s: PR #%s is CONFLICTING — scheduling rebase to main", task_id, task_row.get("pr_number")
        )
        self.store.set_field(task_id, last_review_poll_ts=now)
        self._schedule_rebase_to_main(
            task_id, pool, futures, review_response_futures, trigger_reason="sibling_conflict"
        )
        return True

    def _handle_post_pr_ci_failure(
        self: Any,
        task_row: TaskRow,
        pr_status: _rt.github.PRStatus,
        now: float,
        pool: ThreadPoolExecutor,
        futures: dict[str, Future],
        review_response_futures: set[str],
    ) -> bool:
        review_cap = self.cfg.max_parallel + self.cfg.review_response_extra_slots
        if pr_status.checks_status != "failure" or not pr_status.failed_checks:
            return False
        if task_row["id"] in futures or len(futures) >= review_cap:
            return False
        _rt.log.info(
            "task %s: PR #%s CI failing (%d failed check(s)) — scheduling CI-fix cycle",
            task_row["id"],
            task_row.get("pr_number"),
            len(pr_status.failed_checks),
        )
        self.store.set_field(task_row["id"], last_review_poll_ts=now)
        self._schedule_ci_fix_response(task_row["id"], pr_status, pool, futures, review_response_futures)
        return True

    def _fetch_review_threads(self: Any, repo: str, pr_number: int) -> list[ReviewThread]:
        try:
            return _rt.github_graphql.get_review_threads(repo, pr_number)
        except Exception as e:
            _rt.log.warning("get_review_threads(%s, %s) raised: %s", repo, pr_number, e)
            return []

    def _apply_post_pr_poll_state(
        self: Any,
        task_row: TaskRow,
        pr_status: _rt.github.PRStatus,
        threads: list[ReviewThread],
    ) -> TaskRow:
        task_id = task_row["id"]
        target = self._classify_post_pr_target_state(task_row, pr_status, threads)
        if target is None or target.value == task_row.get("state"):
            return task_row
        if target is State.PENDING_CI:
            if task_row.get("state") in {State.AWAITING_REVIEW.value, State.MERGE_READY.value}:
                fsm_runtime.enter_triaging_feedback(
                    self.store, task_id, note=f"poll classified state -> {target.value}"
                )
                if any(not thread.is_resolved for thread in threads):
                    return _rt.cast(TaskRow, {**dict(task_row), "state": State.TRIAGING_FEEDBACK.value})
            fsm_runtime.enter_pending_ci(self.store, task_id, note=f"poll classified state -> {target.value}")
        elif target is State.AWAITING_REVIEW:
            fsm_runtime.enter_awaiting_review(
                self.store, task_id, note=f"poll classified state -> {target.value}"
            )
        elif target is State.MERGE_READY:
            fsm_runtime.enter_merge_ready(
                self.store, task_id, note=f"poll classified state -> {target.value}"
            )
        return _rt.cast(TaskRow, {**dict(task_row), "state": target.value})

    def _block_if_review_rounds_exhausted(
        self: Any,
        task_row: TaskRow,
        to_address: list[ReviewThread],
        now: float,
    ) -> bool:
        current_round = int(task_row.get("review_round") or 0)
        if not to_address or current_round < self.cfg.review_rounds_max:
            return False
        task_id = task_row["id"]
        note = (
            f"review_rounds_max ({self.cfg.review_rounds_max}) exhausted; "
            f"{len(to_address)} thread(s) still unresolved. Manual merge or close required."
        )
        _rt.log.warning(
            "task %s: review_rounds_max (%d) exhausted with %d unresolved thread(s); BLOCKING for manual merge/close",
            task_id,
            self.cfg.review_rounds_max,
            len(to_address),
        )
        fsm_runtime.enter_triaging_feedback(self.store, task_id, note=note)
        fsm_runtime.block_current(
            self.store,
            task_id,
            note=note,
            last_error=f"review_rounds_max={self.cfg.review_rounds_max} exhausted; {len(to_address)} unresolved threads remaining",
        )
        self.store.set_field(task_id, last_review_poll_ts=now)
        return True

    def _triage_review_threads(
        self: Any,
        repo: str,
        pr_number: int,
        task_row: TaskRow,
        to_address: list[ReviewThread],
    ) -> tuple[list[ReviewThread], TaskRow]:
        if not to_address:
            return to_address, task_row
        task_id = task_row["id"]
        fsm_runtime.enter_triaging_feedback(
            self.store, task_id, note=f"classifying {len(to_address)} thread(s)"
        )
        updated = _rt.cast(TaskRow, {**dict(task_row), "state": State.TRIAGING_FEEDBACK.value})
        outcome = _rt.triage.triage_review_threads(
            cfg=self.cfg,
            plan_text=str(updated.get("plan_text") or ""),
            threads=list(to_address),
        )
        self._resolve_auto_classified_threads(repo, pr_number, task_id, outcome.auto_resolved)
        if outcome.deferred:
            _rt.log.info(
                "task %s: %d thread(s) deferred to human review (needs_discussion)",
                task_id,
                len(outcome.deferred),
            )
        if outcome.classifier_errors:
            _rt.log.warning(
                "task %s: %d classifier error(s) — those thread(s) fall through to ADDRESSING_FEEDBACK",
                task_id,
                outcome.classifier_errors,
            )
        if outcome.actionable_threads:
            return outcome.actionable_threads, updated
        fsm_runtime.enter_pending_ci(
            self.store, task_id, note="triage handled all threads in-process; nothing to dispatch"
        )
        return [], _rt.cast(TaskRow, {**dict(updated), "state": State.PENDING_CI.value})

    def _resolve_auto_classified_threads(
        self: Any,
        repo: str,
        pr_number: int,
        task_id: str,
        auto_resolved: list[Any],
    ) -> None:
        for thread, verdict in auto_resolved:
            self._reply_to_auto_resolved_thread(repo, pr_number, thread, verdict)
            try:
                _rt.github_graphql.resolve_thread(thread.thread_id)
            except Exception as e:
                _rt.log.warning("auto-resolve of thread %s failed: %s", thread.thread_id, e)
            self.store.mark_thread_addressed(
                task_id,
                thread.thread_id,
                f"auto-classifier-incorrect: {verdict.rationale[:80]}",
            )

    def _reply_to_auto_resolved_thread(
        self: Any, repo: str, pr_number: int, thread: Any, verdict: Any
    ) -> None:
        if not verdict.reply or thread.last_comment_database_id is None:
            return
        try:
            _rt.github_graphql.reply_to_review_thread(
                repo=repo,
                pr_number=pr_number,
                last_comment_database_id=thread.last_comment_database_id,
                body=verdict.reply,
            )
        except Exception as e:
            _rt.log.warning("auto-reply to thread %s failed: %s", thread.thread_id, e)

    def _dispatch_review_response(
        self: Any,
        task_row: TaskRow,
        to_address: list[ReviewThread],
        pool: ThreadPoolExecutor,
        futures: dict[str, Future],
        review_response_futures: set[str],
    ) -> bool:
        if not to_address:
            return False
        task_id = task_row["id"]
        review_cap = self.cfg.max_parallel + self.cfg.review_response_extra_slots
        if len(futures) < review_cap:
            self._schedule_review_response(task_id, to_address, pool, futures, review_response_futures)
            return True
        _rt.log.info(
            "task %s has %d unresolved review threads but pool is full (%d/%d); will retry next tick",
            task_id,
            len(to_address),
            len(futures),
            review_cap,
        )
        if task_row.get("state") == State.TRIAGING_FEEDBACK.value:
            fsm_runtime.enter_pending_ci(self.store, task_id, note="pool full - re-deferring to next poll")
        return True

    def _post_clean_review_followups(
        self: Any,
        task_row: TaskRow,
        pr_status: _rt.github.PRStatus,
        threads: list[ReviewThread],
    ) -> None:
        if self.cfg.auto_merge_when_clean and task_row.get("state") == State.MERGE_READY.value:
            self._attempt_auto_merge(task_row, pr_status, threads)
        if task_row.get("state") == State.MERGE_READY.value:
            self._maybe_notify_settled(task_row, pr_status, threads)

    def _classify_threads(self: Any, task_id: str, threads: list[ReviewThread]) -> list[ReviewThread]:
        """Decide which threads warrant a response cycle and upsert all of
        them into the `review_threads` table.

        Address rules (all must be satisfied):
          - thread.is_resolved is False
          - last_comment_is_bot is False, OR cfg.respond_to_bot_reviews is True
          - thread is "new" relative to what we last addressed: either no
            stored row exists, OR the stored row was never marked addressed,
            OR the latest comment is newer than what we stored last time we
            addressed the thread.
        """
        to_address: list[ReviewThread] = []
        for t in threads:
            stored = self.store.get_review_thread(task_id, t.thread_id)
            # Upsert first so the table tracks current state regardless of action.
            self.store.upsert_review_thread(
                task_id,
                thread_id=t.thread_id,
                is_resolved=t.is_resolved,
                last_comment_ts=t.last_comment_created_at,
                last_comment_author=t.last_comment_author,
                last_comment_is_bot=t.last_comment_is_bot,
            )
            if t.is_resolved:
                continue
            if t.last_comment_is_bot and not self.cfg.respond_to_bot_reviews:
                continue
            # Thread is new (no stored row) → address.
            if stored is None:
                to_address.append(t)
                continue
            addressed_sha = stored.get("addressed_in_commit_sha")
            if not addressed_sha:
                # Never addressed; the stored row is from a prior poll-only
                # observation. Address it.
                to_address.append(t)
                continue
            # Already addressed at some point. Address again iff the latest
            # comment is newer than what we last saw at address _rt.time. We
            # approximate by comparing the incoming `last_comment_created_at`
            # to the previously-stored `last_comment_ts` — the upsert above
            # already overwrote that field, so we fall back to stored value
            # before the upsert. For a conservative approach: if the times
            # differ, treat it as a new comment.
            stored_ts = float(stored.get("last_comment_ts") or 0.0)
            if t.last_comment_created_at > stored_ts:
                to_address.append(t)
        return to_address

    def _schedule_review_response(
        self: Any,
        task_id: str,
        threads: list[ReviewThread],
        pool: ThreadPoolExecutor,
        futures: dict[str, Future],
        review_response_futures: set[str],
    ) -> None:
        """Mark the task ADDRESSING_FEEDBACK and submit a worker future."""
        fsm_runtime.enter_addressing_feedback(
            self.store,
            task_id,
            note=f"daemon scheduled response to {len(threads)} thread(s)",
        )
        _rt.log.info("scheduling review response for task %s (%d threads)", task_id, len(threads))
        fut = pool.submit(self._run_review_response_one, task_id, threads)
        futures[task_id] = fut
        review_response_futures.add(task_id)

    def _run_review_response_one(self: Any, task_id: str, threads: list[ReviewThread]):
        node = self.dag.nodes[task_id]
        worker = _rt.TaskWorker(self.cfg, self.dag, self.store, node)
        return worker.run_review_response(threads)

    def _schedule_ci_fix_response(
        self: Any,
        task_id: str,
        pr_status: _rt.github.PRStatus,
        pool: ThreadPoolExecutor,
        futures: dict[str, Future],
        review_response_futures: set[str],
    ) -> None:
        """Dispatch a CI-fix cycle when GitHub CI fails *after* the worker
        has handed off to PENDING_CI. Re-uses the review-response
        worker entry mode (which fixup-decomposes into per-slice subtasks)
        with a CI-failure trigger context."""
        fsm_runtime.enter_triaging_feedback(
            self.store,
            task_id,
            note=f"CI failed with {len(pr_status.failed_checks)} failed check(s)",
        )
        fsm_runtime.enter_addressing_feedback(
            self.store,
            task_id,
            note=f"daemon scheduled CI-fix for {len(pr_status.failed_checks)} failed check(s)",
        )
        _rt.log.info(
            "scheduling CI-fix for task %s (%d failed checks)",
            task_id,
            len(pr_status.failed_checks),
        )
        fut = pool.submit(self._run_ci_fix_response_one, task_id, pr_status)
        futures[task_id] = fut
        review_response_futures.add(task_id)

    def _run_ci_fix_response_one(self: Any, task_id: str, pr_status: _rt.github.PRStatus):
        """Worker entry for daemon-detected CI failure on a post-PR task.

        Fetches the failed-check logs, builds a synthetic ReviewThread-shaped
        payload describing the CI failure, and routes through the same
        `run_review_response` path. The worker's fixup planner sees the
        failure context and emits CI-fix subtasks.
        """
        node = self.dag.nodes[task_id]
        worker = _rt.TaskWorker(self.cfg, self.dag, self.store, node)
        return worker.run_ci_fix_response(pr_status)
