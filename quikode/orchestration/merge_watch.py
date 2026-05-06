"""Merge-readiness, auto-merge, and settled notification mixin."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any

from quikode.github_graphql import ReviewThread
from quikode.state import State


class _RunnerGlobals:
    def __getattr__(self, name: str) -> Any:
        return getattr(sys.modules["quikode.orchestration.runner"], name)


_rt = _RunnerGlobals()


class MergeWatchMixin:
    def _attempt_auto_merge(
        self: Any,
        task_row: Mapping[str, Any],
        pr_status: _rt.github.PRStatus,
        threads: list[ReviewThread],
    ) -> None:
        """Squash-merge `task_row`'s PR if it's safe to do so unattended.

        Preconditions (all must hold):
          - cfg.auto_merge_when_clean is True (caller checked, defensive recheck)
          - PR state == OPEN
          - PR mergeable == MERGEABLE
          - All checks SUCCESS (or none)
          - No unresolved review threads (regardless of bot status)
          - The task has been in MERGE_READY for at least
            cfg.auto_merge_min_age_s

        On success: sets `auto_merged=1` and lets the next poll tick
        catch the actual MERGED transition through the existing path.
        Failures are logged but never raised — a transient `gh pr merge`
        error gets retried on the next watcher tick.
        """
        if not self._auto_merge_preconditions(task_row, pr_status, threads):
            return
        task_id = str(task_row["id"])
        pr_number = int(task_row.get("pr_number") or 0)
        if not pr_number:
            return
        _rt.log.info(
            "task %s: auto-merge preconditions met → gh pr merge --squash --delete-branch #%d",
            task_id,
            pr_number,
        )
        try:
            r = _rt.subprocess.run(
                [
                    "gh",
                    "pr",
                    "merge",
                    str(pr_number),
                    "--squash",
                    "--delete-branch",
                ],
                cwd=self.cfg.repo_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except (_rt.subprocess.TimeoutExpired, OSError) as e:
            _rt.log.warning("auto-merge for task %s raised %s; will retry on next tick", task_id, e)
            return
        if r.returncode != 0:
            _rt.log.warning(
                "auto-merge for task %s PR #%d failed (rc=%d): %s",
                task_id,
                pr_number,
                r.returncode,
                (r.stderr or r.stdout)[:300],
            )
            return
        self.store.set_field(task_id, auto_merged=1)
        _rt.log.info("task %s: PR #%d auto-merged successfully", task_id, pr_number)

    def _auto_merge_preconditions(
        self: Any,
        task_row: Mapping[str, Any],
        pr_status: _rt.github.PRStatus,
        threads: list[ReviewThread],
    ) -> bool:
        checks_ok = pr_status.checks_status in ("success", "none")
        clean = not any(not thread.is_resolved for thread in threads)
        last_change = self._last_state_change_ts(str(task_row["id"]))
        age_ok = last_change is None or (_rt.time.time() - last_change) >= self.cfg.auto_merge_min_age_s
        return (
            self.cfg.auto_merge_when_clean
            and pr_status.state == "OPEN"
            and pr_status.mergeable == "MERGEABLE"
            and checks_ok
            and clean
            and age_ok
        )

    def _last_state_change_ts(self: Any, task_id: str) -> float | None:
        """Most-recent `state_log` ts for a task, or None when missing."""
        r = self.store.conn.execute(
            "SELECT MAX(ts) AS ts FROM state_log WHERE task_id = ?", (task_id,)
        ).fetchone()
        if r is None:
            return None
        v = r["ts"]
        return float(v) if v is not None else None

    def _classify_post_pr_target_state(
        self: Any,
        task_row: Mapping[str, Any],
        pr_status: _rt.github.PRStatus,
        threads: list[ReviewThread],
    ) -> State | None:
        """Decide which post-PR state a task should be in given live signals.

        Returns the target state, or None if no transition is appropriate
        (current state isn't a post-PR state, or signals are ambiguous).
        Does not write — caller transitions if returned state differs from
        current.

        Truth table:
          - CI failure OR unresolved threads OR CI pending  → PENDING_CI
          - CI green, all threads resolved, recently changed → AWAITING_REVIEW
          - CI green, all threads resolved, settled past quiet window → MERGE_READY
        """
        try:
            current = State(task_row["state"])
        except (ValueError, KeyError):
            return None
        if current not in {State.PENDING_CI, State.AWAITING_REVIEW, State.MERGE_READY}:
            return None

        # CI failure or unresolved threads: not in any "ready" state. The
        # daemon's CI-fix / review-response branches will dispatch the worker
        # separately; here we just make sure the row reflects "not done yet."
        ci_failed = pr_status.checks_status == "failure"
        has_unresolved = any(not t.is_resolved for t in threads)
        if ci_failed or has_unresolved:
            return State.PENDING_CI

        # CI not yet green: stay PENDING_CI.
        if pr_status.checks_status not in ("success", "none"):
            return State.PENDING_CI

        # Settle window: how long since we entered a "clean" post-PR state?
        # Once CI is green and threads are resolved, the row sits at either
        # AWAITING_REVIEW or MERGE_READY; the entry into AWAITING_REVIEW (or
        # directly into MERGE_READY) is the start of the settle window. A
        # task that just had a fixup-response push will see its AWAITING_REVIEW
        # entry timestamp reset; that's the right behavior — the new commit
        # hasn't had time to be re-reviewed.
        quiet_s = self.cfg.stack_settle_quiet_s
        task_id = str(task_row["id"])
        last_clean_entry = self._last_clean_post_pr_entry_ts(task_id)
        if last_clean_entry is None or (_rt.time.time() - last_clean_entry) < quiet_s:
            return State.AWAITING_REVIEW
        return State.MERGE_READY

    def _last_clean_post_pr_entry_ts(self: Any, task_id: str) -> float | None:
        """Most recent ts at which the task entered AWAITING_REVIEW or
        MERGE_READY (the two "clean" post-PR states). Returns None if neither
        appears in the audit trail.
        """
        r = self.store.conn.execute(
            "SELECT MAX(ts) AS ts FROM state_log WHERE task_id = ? AND to_state IN (?, ?)",
            (task_id, State.AWAITING_REVIEW.value, State.MERGE_READY.value),
        ).fetchone()
        if r is None:
            return None
        v = r["ts"]
        return float(v) if v is not None else None

    def _maybe_notify_settled(
        self: Any,
        task_row: Mapping[str, Any],
        pr_status: _rt.github.PRStatus,
        threads: list[ReviewThread],
    ) -> None:
        """Ping the operator when a task has been MERGE_READY long enough that
        a review without anything changing is safe. One ping per settled
        period: re-pings only fire after the task LEFT MERGE_READY (responded
        to a thread, CI flip, etc.) and re-entered.

        Caller already gated on `state == MERGE_READY` — this just sanity-
        checks the live PR signals match (defense against stale poll data).

        Gates (all required):
          - cfg.notify_settled_channel != "none"
          - PR state == OPEN, mergeable == MERGEABLE, all checks SUCCESS
          - No unresolved review threads (any author)
          - Time-since-MERGE_READY-entry >= notify_settled_after_s
          - last_notified_settled_ts is None OR predates the most recent
            transition INTO merge_ready (means the task left + came back).
        """
        if not self._notify_settled_preconditions(pr_status, threads):
            return
        task_id = str(task_row["id"])
        entered_ts = self._merge_ready_entry_ts(task_id)
        if not entered_ts:
            return
        quiet_for = _rt.time.time() - entered_ts
        if quiet_for < self.cfg.notify_settled_after_s:
            return
        # Already notified for THIS settled period?
        last_notified = task_row.get("last_notified_settled_ts")
        if last_notified is not None and float(last_notified) >= entered_ts:
            return  # already pinged for this MERGE_READY period

        # Build + send the message.
        node = self.dag.nodes.get(task_id)
        title = node.title if node else ""
        cost = self.store.task_total_cost_usd(task_id)
        n_threads = len(threads)
        round_no = task_row.get("review_round") or 0
        summary = (
            f"MERGE_READY for {int(quiet_for // 60)}min · "
            f"{round_no} review round(s) · "
            f"{n_threads} thread(s) (all resolved)"
        )
        msg = _rt.notify.SettledMessage(
            task_id=task_id,
            title=title,
            pr_url=task_row.get("pr_url") or "",
            summary=summary,
            cost_usd=cost,
        )
        try:
            ok = _rt.notify.notify_settled(self.cfg, msg)
        except Exception as e:
            _rt.log.warning("notify_settled %s raised: %s", task_id, e)
            return
        if ok:
            self.store.set_field(task_id, last_notified_settled_ts=_rt.time.time())

    def _notify_settled_preconditions(
        self: Any, pr_status: _rt.github.PRStatus, threads: list[ReviewThread]
    ) -> bool:
        return (
            self.cfg.notify_settled_channel != "none"
            and pr_status.state == "OPEN"
            and pr_status.mergeable == "MERGEABLE"
            and pr_status.checks_status in ("success", "none")
            and not any(not thread.is_resolved for thread in threads)
        )

    def _merge_ready_entry_ts(self: Any, task_id: str) -> float:
        entered = self.store.conn.execute(
            "SELECT MAX(ts) FROM state_log WHERE task_id = ? AND to_state = ?",
            (task_id, State.MERGE_READY.value),
        ).fetchone()
        return float(entered[0]) if entered and entered[0] else 0.0
