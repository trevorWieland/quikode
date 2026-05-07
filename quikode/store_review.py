from __future__ import annotations

import time
from typing import Any, cast

from quikode import fsm
from quikode.state_types import ReviewThreadRow, State, TaskRow


class StoreReviewMixin:
    def tasks_needing_review_poll(self: Any, *, cutoff: float) -> list[TaskRow]:
        """Return tasks whose last review-poll is older than `cutoff` (or
        never polled). Used by the daemon's review-watcher pass to throttle
        GraphQL traffic.

        Plan 28: post-PR set is now {PENDING_CI, AWAITING_REVIEW} —
        MERGE_READY retired with the settle window. BLOCKED-with-PR rows
        still poll so a human-driven CHANGES_REQUESTED review can reach the
        worker after manual intervention.
        """
        post_pr_states = (
            State.PENDING_CI.value,
            State.AWAITING_REVIEW.value,
        )
        placeholders = ",".join("?" * len(post_pr_states))
        with self._tx_lock:
            rows = self.conn.execute(
                f"SELECT * FROM tasks "
                f"WHERE (state IN ({placeholders}) OR (state = ? AND pr_number IS NOT NULL)) "
                f"AND (last_review_poll_ts IS NULL OR last_review_poll_ts < ?) "
                f"ORDER BY id",
                (*post_pr_states, State.BLOCKED.value, cutoff),
            ).fetchall()
        return [cast(TaskRow, dict(r)) for r in rows]

    def get_last_processed_review_id(self: Any, task_id: str) -> str | None:
        """Plan 28: most recent GitHub Review id we've already routed to
        ADDRESSING_FEEDBACK, or None if no review has fired yet.
        """
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT last_processed_review_id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if r is None:
            return None
        v = r["last_processed_review_id"]
        return str(v) if v else None

    def mark_review_processed(self: Any, task_id: str, review_id: str) -> None:
        """Stamp the most recent CHANGES_REQUESTED review id we've routed.
        Idempotent — re-stamping the same id is a no-op."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET last_processed_review_id = ?, updated_at = ? WHERE id = ?",
                (review_id, time.time(), task_id),
            )

    def get_stored_review_threads(self: Any, task_id: str) -> list[ReviewThreadRow]:
        with self._tx_lock:
            rows = self.conn.execute(
                "SELECT * FROM review_threads WHERE task_id = ? ORDER BY first_seen_ts",
                (task_id,),
            ).fetchall()
        return [cast(ReviewThreadRow, dict(r)) for r in rows]

    def get_review_thread(self: Any, task_id: str, thread_id: str) -> ReviewThreadRow:
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT * FROM review_threads WHERE task_id = ? AND thread_id = ?",
                (task_id, thread_id),
            ).fetchone()
        return cast(ReviewThreadRow, dict(r)) if r else cast(ReviewThreadRow, None)

    def upsert_review_thread(
        self: Any,
        task_id: str,
        *,
        thread_id: str,
        is_resolved: bool | int,
        last_comment_ts: float,
        last_comment_author: str | None,
        last_comment_is_bot: bool | int,
    ) -> None:
        """Insert a new review_thread row or update an existing one. Preserves
        `addressed_in_commit_sha` from a prior row — that is set explicitly by
        `mark_thread_addressed` and must not be cleared by a poll refresh."""
        now = time.time()
        with self.tx() as c:
            existing = c.execute(
                "SELECT addressed_in_commit_sha, first_seen_ts FROM review_threads "
                "WHERE task_id = ? AND thread_id = ?",
                (task_id, thread_id),
            ).fetchone()
            if existing is None:
                c.execute(
                    "INSERT INTO review_threads "
                    "(task_id, thread_id, is_resolved, last_comment_ts, last_comment_author, "
                    " last_comment_is_bot, addressed_in_commit_sha, first_seen_ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
                    (
                        task_id,
                        thread_id,
                        1 if is_resolved else 0,
                        last_comment_ts,
                        last_comment_author,
                        1 if last_comment_is_bot else 0,
                        now,
                    ),
                )
            else:
                c.execute(
                    "UPDATE review_threads SET "
                    "is_resolved = ?, last_comment_ts = ?, last_comment_author = ?, "
                    "last_comment_is_bot = ? "
                    "WHERE task_id = ? AND thread_id = ?",
                    (
                        1 if is_resolved else 0,
                        last_comment_ts,
                        last_comment_author,
                        1 if last_comment_is_bot else 0,
                        task_id,
                        thread_id,
                    ),
                )

    def mark_thread_addressed(self: Any, task_id: str, thread_id: str, commit_sha: str) -> None:
        """Record that the given thread was addressed by a specific commit.
        Subsequent polls compare incoming `last_comment_ts` against the row's
        existing `last_comment_ts` to decide whether the thread became
        unaddressed again (new comment after addressing)."""
        with self.tx() as c:
            c.execute(
                "UPDATE review_threads SET addressed_in_commit_sha = ? WHERE task_id = ? AND thread_id = ?",
                (commit_sha, task_id, thread_id),
            )

    def children_of_parent_branch(self: Any, parent_branch: str) -> list[TaskRow]:
        """Return non-terminal child tasks whose `parent_pr_branches` JSON
        array contains `parent_branch`.

        Used by the orchestrator to find every child that needs to rebase
        when the parent's PR merges or pushes a new commit. Excludes
        terminal states (MERGED, ABORTED, FAILED, BLOCKED) — there's
        nothing left to rebase for those — and PENDING (no work has
        begun, so the next provision will pick up the new base naturally).
        """
        terminal = (
            State.MERGED.value,
            State.ABORTED.value,
            State.FAILED.value,
            State.BLOCKED.value,
            State.PENDING.value,
        )
        q = ",".join("?" * len(terminal))
        with self._tx_lock:
            rows = self.conn.execute(
                f"SELECT * FROM tasks WHERE state NOT IN ({q}) "
                f"AND parent_pr_branches IS NOT NULL "
                f"AND EXISTS (SELECT 1 FROM json_each(parent_pr_branches) "
                f"            WHERE json_each.value = ?) "
                f"ORDER BY id",
                (*terminal, parent_branch),
            ).fetchall()
        return [cast(TaskRow, dict(r)) for r in rows]

    def clear_parent_branch(self: Any, task_id: str) -> None:
        """Clear stacked-diff parent-branch metadata. Called after a child
        successfully rebases onto main, OR when the parent's PR closed
        without merging (no longer a valid stack base either way)."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET "
                "  parent_task_ids = NULL, parent_branches = NULL, parent_pr_branches = NULL, "
                "  parent_merge_base_sha = NULL, parent_merge_base_branch = NULL, "
                "  needs_parent_rebase = 0, updated_at = ? WHERE id = ?",
                (time.time(), task_id),
            )

    def mark_needs_parent_rebase(self: Any, task_id: str) -> None:
        """Set the mid-flight parent-merge flag. Worker checks at safe
        checkpoints and runs an inline rebase + retarget before proceeding."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET needs_parent_rebase = 1, updated_at = ? WHERE id = ?",
                (time.time(), task_id),
            )

    def clear_needs_parent_rebase(self: Any, task_id: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET needs_parent_rebase = 0, updated_at = ? WHERE id = ?",
                (time.time(), task_id),
            )

    def children_with_parent_branch(self: Any, parent_branch: str) -> list[TaskRow]:
        """Return ALL non-terminal tasks whose `parent_pr_branches` JSON
        array contains `parent_branch` — regardless of whether
        `_schedule_rebases_for_merged_parent` will also schedule a rebase
        future. Used to clear stale parent metadata when a parent closes
        without merging."""
        terminal = (
            State.MERGED.value,
            State.ABORTED.value,
            State.FAILED.value,
            State.BLOCKED.value,
        )
        q = ",".join("?" * len(terminal))
        with self._tx_lock:
            rows = self.conn.execute(
                f"SELECT * FROM tasks WHERE state NOT IN ({q}) "
                f"AND parent_pr_branches IS NOT NULL "
                f"AND EXISTS (SELECT 1 FROM json_each(parent_pr_branches) "
                f"            WHERE json_each.value = ?) "
                f"ORDER BY id",
                (*terminal, parent_branch),
            ).fetchall()
        return [cast(TaskRow, dict(r)) for r in rows]

    def set_pre_rebase_state(self: Any, task_id: str, state: str) -> None:
        """Stash the pre-rebase active state on the row so the rebase worker
        can restore it after a successful rebase. Idempotent."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET pre_rebase_state = ?, updated_at = ? WHERE id = ?",
                (state, time.time(), task_id),
            )

    def get_pre_rebase_state(self: Any, task_id: str) -> str | None:
        with self._tx_lock:
            r = self.conn.execute("SELECT pre_rebase_state FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if r is None:
            return None
        v = r["pre_rebase_state"]
        return str(v) if v is not None else None

    def recover_orphan_tasks(self: Any) -> list[tuple[str, str, str]]:
        """Reset tasks that were mid-flight when the orchestrator died.

        Called once at `quikode run` startup before the orchestrator is
        constructed. Each task in an active state had a worker driving it;
        after a crash/SIGTERM nothing is, so the row would otherwise sit
        forever (the picker only picks PENDING).

        Recovery rules per state — see docs/design-stacked-diffs-fix.md §3:
        * provisioning → pending (clear branch/wt/cid)
        * planning → pending (preserve plan_text via resume marker if set)
        * in-progress task states → pending + resume marker
        * committing/pushing → pending + resume marker
        * pr_opening → pending_ci (if pr_number set) else pending + resume
        * triaging_feedback → pending_ci (let daemon re-detect)
        * addressing_feedback → pending_ci (let daemon re-detect)
        * rebasing_to_main → pending_ci (if pr_number) else pending + resume

        All recovery transitions also reset retry counters so the next
        attempt has a fresh budget.

        PR-aware tasks land in PENDING_CI rather than the more specific
        AWAITING_REVIEW / MERGE_READY because the daemon's poll re-derives
        the right state from CI + review signals on its next tick.

        Returns list of (task_id, from_state, to_state) for caller logging.
        """
        retry_reset_fields = {
            "ci_triage_retries": 0,
            "conflict_resolve_retries": 0,
            "needs_intent_review": 0,
            "needs_parent_rebase": 0,
            "last_error": None,
        }

        recovered: list[tuple[str, str, str]] = []
        for row in self.all_tasks():
            try:
                cur = State(row["state"])
            except ValueError:
                continue
            if cur not in fsm.ACTIVE_STATES:
                continue

            from_state = cur.value
            extras: dict[str, Any] = dict(retry_reset_fields)
            target, recovery_fields = fsm.recover_after_crash(cur, has_pr=bool(row.get("pr_number")))
            extras.update(recovery_fields)

            self.transition(row["id"], target, note=f"orphan recovery from {from_state}", **extras)
            recovered.append((row["id"], from_state, target.value))
        return recovered

    def get_last_rebase_scheduled_ts(self: Any, task_id: str) -> float | None:
        """Read the most recent rebase-trigger timestamp for a task, or
        None when never set. Used by the orchestrator's coalescing window
        check in `_schedule_rebase_to_main`."""
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT last_rebase_scheduled_ts FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if r is None:
            return None
        v = r["last_rebase_scheduled_ts"]
        return float(v) if v is not None else None

    def set_last_rebase_scheduled(self: Any, task_id: str, ts: float) -> None:
        """Stamp the most-recent rebase-trigger timestamp on a task. Caller
        is responsible for the coalescing-window comparison; this just
        persists the value."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET last_rebase_scheduled_ts = ?, updated_at = ? WHERE id = ?",
                (ts, time.time(), task_id),
            )

    def increment_review_round(self: Any, task_id: str) -> int:
        """Bump the human-driven review→respond cycle counter for a task."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET review_round = COALESCE(review_round, 0) + 1, updated_at = ? WHERE id = ?",
                (time.time(), task_id),
            )
            r = c.execute("SELECT review_round FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return int(r["review_round"]) if r else 0
