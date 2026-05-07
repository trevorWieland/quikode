"""Merge-readiness + auto-merge mixin (plan 28 — streamlined).

Pre-plan-28 this module had:
- `_classify_post_pr_target_state` 3-way truth table (PENDING_CI /
  AWAITING_REVIEW / MERGE_READY) driven by CI + thread state + a settle
  window timer.
- `_maybe_notify_settled` notification surface.
- `_attempt_auto_merge` gated on the settle window + thread-clean check.

Plan 28 cuts all of that. The post-PR FSM has two states (PENDING_CI,
AWAITING_REVIEW) and one auto-merge trigger: a formal `APPROVED` review on a
clean PR (CI green, mergeable). The settle window dies with MERGE_READY; bot
chatter never blocks merge anymore because bot reviews don't count.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any


class _RunnerGlobals:
    def __getattr__(self, name: str) -> Any:
        return getattr(sys.modules["quikode.orchestration.runner"], name)


_rt = _RunnerGlobals()


class MergeWatchMixin:
    def _attempt_auto_merge(
        self: Any,
        task_row: Mapping[str, Any],
        pr_status: _rt.github.PRStatus,
        latest_approval_id: str | None,
    ) -> None:
        """Squash-merge `task_row`'s PR if it's safe to do so unattended.

        Plan-28 preconditions (all must hold):
          - cfg.auto_merge_when_clean is True
          - PR state == OPEN
          - PR mergeable == MERGEABLE
          - All checks SUCCESS (or none)
          - We've observed a non-bot APPROVED review (caller passes its id)

        On success: sets `auto_merged=1`. The next poll observes `pr_status.state
        == "MERGED"` and fires the existing MERGED transition path.
        Failures are logged but never raised — a transient `gh pr merge`
        error gets retried on the next watcher tick.
        """
        if not latest_approval_id or not self.cfg.auto_merge_when_clean:
            return
        if pr_status.state != "OPEN" or pr_status.mergeable != "MERGEABLE":
            return
        if pr_status.checks_status not in ("success", "none"):
            return
        task_id = str(task_row["id"])
        pr_number = int(task_row.get("pr_number") or 0)
        if not pr_number:
            return
        _rt.log.info(
            "task %s: APPROVED + clean → gh pr merge --squash --delete-branch #%d",
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
