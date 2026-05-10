"""Post-PR review and CI watcher mixin (plan 28).

Two polling phases per task:

1. PENDING_CI: poll `gh pr view` for CI rollup. On `success` → AWAITING_REVIEW
   via `CI_PASSED`. On `failure` → ADDRESSING_FEEDBACK with bundled CI excerpt
   via `CI_FAILED` (no per-thread classifier in the loop).

2. AWAITING_REVIEW: poll `gh pr view` (CI flake / merge / close detection)
   plus formal Reviews via `_PR_REVIEWS_QUERY`. Trigger on the first non-bot
   review whose id ≠ `last_processed_review_id`:
   - `CHANGES_REQUESTED` → bundle every unresolved thread + every PR comment
     + recent reviews, dispatch ADDRESSING_FEEDBACK worker.
   - `APPROVED` (when `auto_merge_when_clean=True` AND CI clean) → fire
     `gh pr merge --squash --delete-branch`. Next poll observes the remote
     MERGED state and fires the FSM `MERGED` event.
   - `COMMENTED` / `DISMISSED` / bot reviews → ignored. Comments + bots are
     bundled CONTEXT only; resolved threads are excluded (= human-dismissed).

Pre-plan-28 polluted code paths now retired:
- per-thread classifier (`triage.classify_review_thread`)
- `_classify_threads`, `_triage_review_threads`, `_resolve_auto_classified_threads`
- thread-bookkeeping (`upsert_review_thread`, `mark_thread_addressed`)
- `_classify_post_pr_target_state` 3-way truth table
- `_block_if_review_rounds_exhausted` thread-batch counter
- `_maybe_notify_settled` and the entire settled-task notification surface
"""

from __future__ import annotations

import sys
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from quikode import fsm_runtime, notify
from quikode.fsm import Event
from quikode.github_graphql import Review
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
        """One review-watcher tick (plan 28).

        Throttled per-task by `cfg.review_poll_interval_s`. For each eligible
        candidate, drives the post-PR FSM directly from CI + formal Review
        signals. Method name retained for callsite stability; behavior is
        the streamlined two-phase model.
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
        if self._handle_pre_review_pr_signal(
            task_row, pr_status, now, pool, futures, review_response_futures
        ):
            return
        # Plan 28: only AWAITING_REVIEW rows poll formal reviews. PENDING_CI
        # rows are CI-only on this tick (CI pass/fail handled in
        # _handle_pre_review_pr_signal above and in _maybe_handle_ci_pass below).
        self.store.set_field(task_id, last_review_poll_ts=now)
        if self._maybe_handle_ci_pass(task_row, pr_status):
            return
        if str(task_row.get("state")) != State.AWAITING_REVIEW.value:
            return
        # Plan 30: review-ready-settled signal fires once per settled period
        # (AWAITING_REVIEW for ≥ cfg.review_ready_settle_s, ntfy not yet fired
        # since most recent entry into AWAITING_REVIEW). Same threshold gates
        # stacked-diff dependent kickoff via scheduler.is_parent_stack_ready.
        self._maybe_notify_review_ready(task_row)
        self._handle_awaiting_review_reviews(
            repo, pr_number, task_row, pr_status, pool, futures, review_response_futures
        )

    def _maybe_notify_review_ready(self: Any, task_row: TaskRow) -> None:
        topic = (self.cfg.notify_ntfy_topic or "").strip()
        if not topic:
            return
        task_id = task_row["id"]
        entered_ts = self.store.most_recent_awaiting_review_entry_ts(task_id)
        if entered_ts is None:
            return
        now = _rt.time.time()
        if now - entered_ts < self.cfg.review_ready_settle_s:
            return
        last_notified = self.store.get_last_review_ready_notified_ts(task_id)
        if last_notified is not None and last_notified >= entered_ts:
            return  # already pinged for this settled period
        node = self.dag.nodes.get(task_id)
        title = node.title if node else task_id
        round_no = int(task_row.get("review_round") or 0)
        settled_min = int((now - entered_ts) // 60)
        round_str = f" · {round_no} review round(s)" if round_no else ""
        msg = notify.ReviewReadyMessage(
            task_id=task_id,
            title=title,
            pr_url=str(task_row.get("pr_url") or ""),
            summary=f"settled {settled_min}min{round_str} · CI green",
        )
        ok = notify.notify_review_ready(
            ntfy_url=self.cfg.notify_ntfy_url,
            ntfy_topic=topic,
            msg=msg,
        )
        if ok:
            self.store.set_last_review_ready_notified_ts(task_id, now)

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

    def _handle_pre_review_pr_signal(
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

    def _maybe_handle_ci_pass(self: Any, task_row: TaskRow, pr_status: _rt.github.PRStatus) -> bool:
        """Drive PENDING_CI → AWAITING_REVIEW on observed CI success.

        Returns True if a transition fired (caller should bail). On any other
        state (including AWAITING_REVIEW already), returns False.
        """
        if str(task_row.get("state")) != State.PENDING_CI.value:
            return False
        if pr_status.checks_status not in ("success", "none"):
            return False
        task_id = task_row["id"]
        fsm_runtime.enter_awaiting_review(self.store, task_id, note="CI green — awaiting formal review")
        return True

    def _handle_awaiting_review_reviews(
        self: Any,
        repo: str,
        pr_number: int,
        task_row: TaskRow,
        pr_status: _rt.github.PRStatus,
        pool: ThreadPoolExecutor,
        futures: dict[str, Future],
        review_response_futures: set[str],
    ) -> None:
        """Plan 28: fetch formal reviews and dispatch on the latest novel one.

        We process at most one review per tick — the most recent non-bot
        submitted review whose id ≠ `last_processed_review_id`. This naturally
        de-dupes across daemon restarts and across multiple bot reviews
        landing between ticks.
        """
        task_id = task_row["id"]
        try:
            reviews = _rt.github_graphql.get_latest_reviews(repo, pr_number)
        except Exception as e:
            _rt.log.warning("get_latest_reviews(%s, %s) raised: %s", repo, pr_number, e)
            return
        last_processed = self.store.get_last_processed_review_id(task_id)
        novel = self._novel_reviews(reviews, last_processed)
        if not novel:
            return
        # Process the most recent novel non-bot submitted review.
        target = novel[-1]
        if target.state == "CHANGES_REQUESTED":
            self._handle_changes_requested(
                repo, pr_number, task_row, target, pool, futures, review_response_futures
            )
            return
        if target.state == "APPROVED":
            self._handle_approval(task_row, pr_status, target)
            return
        # COMMENTED, DISMISSED, PENDING — context-only; advance the cursor so
        # we don't keep re-checking the same review.
        self.store.mark_review_processed(task_id, target.review_id)

    def _novel_reviews(self: Any, reviews: list[Review], last_processed: str | None) -> list[Review]:
        """Return submitted, non-bot reviews newer than `last_processed`,
        sorted oldest-first (caller takes [-1] for most recent)."""
        non_bot = [r for r in reviews if not r.is_bot and r.state != "PENDING"]
        if not last_processed:
            return non_bot
        seen = False
        out: list[Review] = []
        for r in non_bot:  # already sorted oldest-first by get_latest_reviews
            if seen:
                out.append(r)
            elif r.review_id == last_processed:
                seen = True
        return out if seen else non_bot

    def _handle_changes_requested(
        self: Any,
        repo: str,
        pr_number: int,
        task_row: TaskRow,
        review: Review,
        pool: ThreadPoolExecutor,
        futures: dict[str, Future],
        review_response_futures: set[str],
    ) -> None:
        task_id = task_row["id"]
        review_cap = self.cfg.max_parallel + self.cfg.review_response_extra_slots
        if len(futures) >= review_cap:
            _rt.log.info(
                "task %s: CHANGES_REQUESTED received but pool full (%d/%d); will retry next tick",
                task_id,
                len(futures),
                review_cap,
            )
            return
        current_round = int(task_row.get("review_round") or 0)
        if current_round >= self.cfg.review_rounds_max:
            note = (
                f"review_rounds_max ({self.cfg.review_rounds_max}) exhausted; manual merge or close required."
            )
            _rt.log.warning("task %s: review rounds exhausted — BLOCKING", task_id)
            fsm_runtime.block_current(
                self.store,
                task_id,
                note=note,
                last_error=note,
            )
            self.store.mark_review_processed(task_id, review.review_id)
            return
        try:
            bundled_context = _rt.github_graphql.bundle_pr_context(repo, pr_number)
        except Exception as e:
            _rt.log.warning(
                "bundle_pr_context(%s, %s) raised: %s — using review body only", repo, pr_number, e
            )
            bundled_context = ""
        if not bundled_context.strip():
            bundled_context = f"CHANGES_REQUESTED review by {review.author}:\n{review.body or '(no body)'}"
        # Plan 49: re-read state right before the FSM call (the dispatcher read
        # `task_row` earlier this tick — it may have drifted to BLOCKED/FAILED
        # via another path, e.g. review-rounds-exhausted just above). The FSM
        # rejects `blocked|failed → addressing_feedback`, so skip the event +
        # the worker. Do NOT mark the review processed: when the operator
        # unblocks, the next poll re-sees this review and addresses it.
        fresh_row = self.store.get(task_id)
        fresh_state = str(fresh_row.get("state")) if fresh_row else ""
        if fresh_state in (State.BLOCKED.value, State.FAILED.value):
            _rt.log.info(
                "task %s: CHANGES_REQUESTED on PR #%s but task is %s — skipping FSM event; awaiting operator unblock",
                task_id,
                pr_number,
                fresh_state.upper(),
            )
            return
        fsm_runtime.enter_addressing_feedback(
            self.store,
            task_id,
            note=f"CHANGES_REQUESTED by {review.author}",
            event=Event.CHANGES_REQUESTED_RECEIVED,
        )
        self.store.mark_review_processed(task_id, review.review_id)
        _rt.log.info(
            "scheduling changes-requested response for task %s (review by %s)",
            task_id,
            review.author,
        )
        fut = pool.submit(self._run_changes_requested_response_one, task_id, bundled_context)
        futures[task_id] = fut
        review_response_futures.add(task_id)

    def _handle_approval(
        self: Any, task_row: TaskRow, pr_status: _rt.github.PRStatus, review: Review
    ) -> None:
        task_id = task_row["id"]
        # Stamp processed so we don't re-trigger on the same approval.
        self.store.mark_review_processed(task_id, review.review_id)
        self._attempt_auto_merge(task_row, pr_status, review.review_id)

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
        # Plan 49: never auto-schedule a CI-fix cycle on a BLOCKED/FAILED task —
        # the FSM rejects `blocked|failed → addressing_feedback`, and prior to
        # this guard the unconditional `enter_addressing_feedback` raised and
        # crash-looped the orchestrator child. Skip; on the next poll the
        # operator will (we hope) have unblocked the task and normal handling
        # resumes. Returning False here keeps the main loop's other handlers
        # active for this candidate.
        current_state = str(task_row.get("state"))
        if current_state in (State.BLOCKED.value, State.FAILED.value):
            _rt.log.info(
                "task %s: PR #%s CI failing but task is %s — skipping daemon CI-fix; awaiting operator unblock",
                task_row["id"],
                task_row.get("pr_number"),
                current_state.upper(),
            )
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

    def _schedule_ci_fix_response(
        self: Any,
        task_id: str,
        pr_status: _rt.github.PRStatus,
        pool: ThreadPoolExecutor,
        futures: dict[str, Future],
        review_response_futures: set[str],
    ) -> None:
        """Plan 28: route CI failure straight to ADDRESSING_FEEDBACK via
        either CI_FAILED (from PENDING_CI) or CI_FAILED (from AWAITING_REVIEW
        — CI flake after pass). No TRIAGING_FEEDBACK intermediate.
        """
        # Use the right event depending on current state.
        row = self.store.get(task_id)
        current = State(str(row["state"])) if row else None
        if current is State.AWAITING_REVIEW:
            fsm_runtime.enter_addressing_feedback(
                self.store,
                task_id,
                note=f"daemon scheduled CI-fix for {len(pr_status.failed_checks)} failed check(s)",
                event=Event.CI_FAILED,
            )
        else:
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

    def _run_changes_requested_response_one(self: Any, task_id: str, bundled_context: str):
        """Worker entry for daemon-detected CHANGES_REQUESTED review (plan 28)."""
        node = self.dag.nodes[task_id]
        worker = _rt.TaskWorker(self.cfg, self.dag, self.store, node)
        return worker.run_changes_requested_response(bundled_context)

    def _run_ci_fix_response_one(self: Any, task_id: str, pr_status: _rt.github.PRStatus):
        node = self.dag.nodes[task_id]
        worker = _rt.TaskWorker(self.cfg, self.dag, self.store, node)
        return worker.run_ci_fix_response(pr_status)
