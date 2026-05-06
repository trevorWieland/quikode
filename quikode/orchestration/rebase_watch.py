"""Parent merge and rebase cascade mixin."""

from __future__ import annotations

import sys
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from quikode import fsm_runtime
from quikode.state import State, TaskRow


class _RunnerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.orchestration.runner"], name)


_rt = _RunnerGlobals()


class RebaseWatchMixin:
    def _schedule_rebases_for_merged_parent(
        self: Any,
        parent_branch: str,
        pool: ThreadPoolExecutor | None,
        futures: dict[str, Future],
        review_response_futures: set[str],
    ) -> None:
        """When a parent task transitions to MERGED, scan for children
        whose `parent_pr_branch` matches and trigger an auto-rebase only
        for children that actually need one. Children that are already
        terminal (MERGED/ABORTED/etc) are excluded by
        `children_of_parent_branch`.

        Smart-skip: if a child's PR is still MERGEABLE against the base
        branch AND its base ref still exists, no rebase is required —
        github already maintained the rebased view. We just clear the
        stale parent metadata so the child is treated as a top-level task
        going forward. Rebase is only scheduled when the child is
        CONFLICTING or its base ref has been deleted.
        """
        children = self.store.children_of_parent_branch(parent_branch)
        if not children:
            return
        _rt.log.info(
            "parent branch %s merged → evaluating %d child(ren) for rebase",
            parent_branch,
            len(children),
        )
        skipped = 0
        for child in children:
            child_id = str(child["id"])
            pr_number = child.get("pr_number")
            # Decide whether a rebase is actually needed. With no PR yet
            # (e.g. mid-DOING_SUBTASK before pr_opening), we keep the
            # current behavior — flag + schedule — because we don't have
            # a mergeable signal to consult.
            needs_rebase = True
            if pr_number:
                try:
                    pr_status = _rt.github.poll_pr(self.cfg.repo_path, int(pr_number))
                except (OSError, _rt.subprocess.SubprocessError) as e:
                    _rt.log.warning(
                        "poll_pr for child %s PR #%s failed (%s); falling back to scheduling rebase",
                        child_id,
                        pr_number,
                        e,
                    )
                    pr_status = None
                if pr_status is not None:
                    base_intact = self._remote_branch_exists(parent_branch)
                    if pr_status.mergeable == "MERGEABLE" and base_intact:
                        # No work to do — child PR is still in good shape
                        # against its base ref. Clear stale metadata so
                        # later picks treat the child as top-level.
                        self.store.clear_parent_branch(child_id)
                        _rt.log.info(
                            "child %s PR #%s mergeable + base intact; skipping rebase, cleared parent metadata",
                            child_id,
                            pr_number,
                        )
                        skipped += 1
                        continue
                    if pr_status.mergeable == "CONFLICTING":
                        _rt.log.info(
                            "child %s PR #%s CONFLICTING — scheduling rebase",
                            child_id,
                            pr_number,
                        )
                    elif not base_intact:
                        _rt.log.info(
                            "child %s base ref %s missing on remote — scheduling rebase",
                            child_id,
                            parent_branch,
                        )
            if not needs_rebase:
                continue
            # Always raise the mid-flight flag. The worker checks it at
            # safe checkpoints and handles the rebase inline. For non-active
            # children we additionally schedule a worker future as today.
            self.store.mark_needs_parent_rebase(child_id)
            if child_id in futures:
                # An active worker is mid-flight on this child. Don't submit
                # a duplicate future — the flag is enough; the worker will
                # handle the rebase + PR retarget before continuing.
                _rt.log.info(
                    "child %s has active worker; flagged needs_parent_rebase for inline handling",
                    child_id,
                )
                continue
            self._schedule_rebase_to_main(child_id, pool, futures, review_response_futures)
        if skipped:
            _rt.log.info(
                "parent branch %s: %d child(ren) skipped rebase (PR still mergeable)",
                parent_branch,
                skipped,
            )

    def _schedule_cascade_rebase(
        self: Any,
        parent_branch: str,
        pool: ThreadPoolExecutor | None,
        futures: dict[str, Future],
        review_response_futures: set[str],
    ) -> None:
        """v3.5 Phase 2 follow-up: when a parent's branch advances (push, not
        merge), schedule rebases for every descendant whose merge-base or
        single-parent base referenced this branch. Walks the parent DAG
        downward via `children_of_parent_branch` (matches both scalar and
        JSON-array linkage). Active workers get `needs_parent_rebase=1`
        (handled inline at safe checkpoints); non-active children are
        scheduled through the existing rebase pool.

        Critical guarantee: descendants are queued in topo order so a child
        rebases AFTER its own parents have themselves rebased. We approximate
        topo-order by sorting candidate ids and recursing — workspaces are
        small (< 300 nodes), so the overhead is negligible.
        """
        children = self.store.children_of_parent_branch(parent_branch)
        if not children:
            return
        _rt.log.info(
            "parent branch %s tip advanced → cascading rebase to %d direct descendant(s)",
            parent_branch,
            len(children),
        )
        # Track which descendants have been queued already so we don't
        # re-enqueue across recursion.
        scheduled: set[str] = set()

        def _enqueue(child_row: TaskRow) -> None:
            child_id = str(child_row["id"])
            if child_id in scheduled:
                return
            scheduled.add(child_id)
            # Always raise the mid-flight flag. When the worker exits the
            # current safe checkpoint it'll pick this up and rebase inline.
            self.store.mark_needs_parent_rebase(child_id)
            if child_id in futures:
                _rt.log.info(
                    "cascade rebase: %s has active worker; flagged needs_parent_rebase",
                    child_id,
                )
            else:
                self._schedule_rebase_to_main(
                    child_id,
                    pool,
                    futures,
                    review_response_futures,
                    trigger_reason="parent_tip_advanced",
                )
            # Recurse into the *child's* descendants — D depends on B, B
            # advances → D rebases → D's downstream descendants also need
            # to rebase against the new D.
            child_branch = child_row.get("branch")
            if child_branch:
                grandchildren = self.store.children_of_parent_branch(str(child_branch))
                for gc in grandchildren:
                    _enqueue(gc)

        for child in children:
            _enqueue(child)

    def _remote_branch_exists(self: Any, branch: str) -> bool:
        """Check if `branch` still exists on the configured remote.

        Used by the smart-rebase path to decide whether a child whose PR
        is currently MERGEABLE actually needs a rebase. If the remote
        branch is gone (parent merged with --delete-branch), github will
        have auto-closed the child PR and we DO need to recreate / rebase.
        """
        try:
            r = _rt.subprocess.run(
                ["git", "ls-remote", "--heads", self.cfg.pr_remote, branch],
                cwd=self.cfg.repo_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (_rt.subprocess.TimeoutExpired, OSError) as e:
            _rt.log.warning("ls-remote for %s failed: %s; assuming branch exists", branch, e)
            return True
        if r.returncode != 0:
            return True  # be conservative — assume present on error
        return bool(r.stdout.strip())

    def _schedule_rebase_to_main(
        self: Any,
        task_id: str,
        pool: ThreadPoolExecutor,
        futures: dict[str, Future],
        review_response_futures: set[str],
        *,
        trigger_reason: str = "parent_merged",
    ) -> None:
        """Stash the child's pre-rebase state, transition to REBASING_TO_MAIN,
        and submit a worker future. Mirror of `_schedule_review_response`.

        `trigger_reason` is one of: parent_merged, sibling_conflict,
        worker_checkpoint_flag, manual. It's surfaced in the state-log
        note so debuggers don't have to guess WHY a given rebase fired.
        """
        row = self.store.get(task_id)
        if row is None:
            _rt.log.warning("_schedule_rebase_to_main: task %s missing from store", task_id)
            return
        # Coalescing: if a rebase was already triggered for this task within
        # the configured window, skip. The first rebase will run shortly and
        # the watcher's next tick will surface any genuinely-new conflict
        # against fresh main; another trigger fires from there if needed.
        # This avoids burning a container + agent call on the second of two
        # back-to-back triggers (e.g. parent-merge then sibling-merge within
        # ~30s) where the first rebase already covers both shifts.
        window = self.cfg.rebase_coalesce_window_s
        if window > 0:
            last_ts = self.store.get_last_rebase_scheduled_ts(task_id)
            if last_ts is not None and (_rt.time.time() - last_ts) < window:
                _rt.log.info(
                    "task %s: coalescing rebase trigger (%s) — last trigger %.1fs ago < %ds window",
                    task_id,
                    trigger_reason,
                    _rt.time.time() - last_ts,
                    window,
                )
                return
        self.store.set_last_rebase_scheduled(task_id, _rt.time.time())
        pre_state = str(row.get("state") or State.PENDING.value)
        # Stash the pre-rebase active state so the rebase worker can restore
        # it on success. Post-PR flows return to the post-PR state; mid-loop
        # active states return where they were (the worker re-enters from the
        # FSM at the same point).
        self.store.set_pre_rebase_state(task_id, pre_state)
        reason_label = {
            "parent_merged": "parent merged",
            "sibling_conflict": "sibling conflict via mergeable=CONFLICTING",
            "worker_checkpoint_flag": "worker checkpoint flag",
            "manual": "manual",
        }.get(trigger_reason, trigger_reason)
        fsm_runtime.enter_rebasing_to_main(
            self.store,
            task_id,
            note=f"rebasing onto main ({reason_label}; was {pre_state})",
        )
        _rt.log.info(
            "scheduling rebase-to-main for task %s (pre-rebase state %s, reason %s)",
            task_id,
            pre_state,
            trigger_reason,
        )
        fut = pool.submit(self._run_rebase_to_main_one, task_id)
        futures[task_id] = fut
        # Track in the same set as review-response futures so the heartbeat
        # surfaces the count under "addressing_feedback_futures". Ground truth
        # lives in the store's REBASING_TO_MAIN state.
        review_response_futures.add(task_id)

    def _run_rebase_to_main_one(self: Any, task_id: str):
        node = self.dag.nodes[task_id]
        worker = _rt.TaskWorker(self.cfg, self.dag, self.store, node)
        return worker.run_rebase_to_main()
