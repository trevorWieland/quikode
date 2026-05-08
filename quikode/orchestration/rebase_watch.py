"""Parent merge and rebase cascade mixin (plan 31).

Two scheduling entries, one per worker target:

- `_schedule_rebase_to_main(task_id, ..., trigger_reason=...)` — fires when
  a parent merged (its branch is gone) OR this task's own PR is
  CONFLICTING against main. Worker reattaches the child to main and
  retargets the PR.

- `_schedule_rebase_to_parent_tip(task_id, ..., parent_branch=...)` — fires
  when a parent's PR branch advanced (push/fixup, not merge). Worker stays
  stacked on the parent's new tip; PR base preserved.

Plus cascade-walk-level coalesce: per-parent-branch `_last_cascade_walk_ts`
suppresses redundant descendant-tree walks within
`cfg.rebase_coalesce_window_s`. Per-child coalesce inside the schedulers
remains as belt-and-suspenders.
"""

from __future__ import annotations

import sys
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from quikode import fsm_runtime, merge_node
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
        """Parent task merged → for each child whose `parent_pr_branch`
        matches, schedule a rebase-to-main (parent's branch is gone).

        Smart-skip: if a child's PR is still MERGEABLE against the base
        branch AND its base ref still exists, no rebase is required.
        """
        # Plan 32: parent merged → propagate to merge-nodes BEFORE walking
        # spec-task descendants. Affected merge-nodes drop the merged source;
        # if the parent set becomes empty, retire; otherwise re-merge.
        parent_id = self._task_id_for_branch(parent_branch)
        if parent_id:
            merge_node.propagate_parent_merged(self.store, parent_id)
        children = self.store.children_of_parent_branch(parent_branch)
        if not children:
            return
        _rt.log.info(
            "parent branch %s merged → evaluating %d child(ren) for rebase-to-main",
            parent_branch,
            len(children),
        )
        skipped = 0
        for child in children:
            child_id = str(child["id"])
            pr_number = child.get("pr_number")
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
                            "child %s PR #%s CONFLICTING — scheduling rebase-to-main",
                            child_id,
                            pr_number,
                        )
                    elif not base_intact:
                        _rt.log.info(
                            "child %s base ref %s missing on remote — scheduling rebase-to-main",
                            child_id,
                            parent_branch,
                        )
            if not needs_rebase:
                continue
            self.store.mark_needs_parent_rebase(child_id)
            if child_id in futures:
                _rt.log.info(
                    "child %s has active worker; flagged needs_parent_rebase for inline handling",
                    child_id,
                )
                continue
            self._schedule_rebase_to_main(
                child_id, pool, futures, review_response_futures, trigger_reason="parent_merged"
            )
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
        """Parent branch advanced (push, not merge) → cascade rebase-to-parent-tip
        through every descendant. Plan 31 keeps children stacked on the
        parent's new tip (not un-stacked onto main).

        Cascade-walk-level coalesce: per-parent-branch ts map suppresses
        redundant walks within `cfg.rebase_coalesce_window_s`. Without this
        the orchestrator walks the descendant tree on every observed
        `head_sha` change, even when the per-child coalesce would dedupe
        each individual schedule. The walk itself is cheap but the recursion
        + DB queries add up at high cascade fanout.
        """
        if not self._cascade_walk_should_proceed(parent_branch):
            return
        # Plan 32: when the advancing parent is a source of any merge-node,
        # propagate the advance — affected merge-nodes go back to PENDING
        # for a re-merge cycle. This fires BEFORE the descendant walk so
        # downstream children of the merge-node see the new tip after the
        # re-merge completes.
        parent_id = self._task_id_for_branch(parent_branch)
        if parent_id:
            merge_node.propagate_parent_advanced(self.store, parent_id)
        children = self.store.children_of_parent_branch(parent_branch)
        if not children:
            return
        _rt.log.info(
            "parent branch %s tip advanced → cascading parent_tip rebase to %d direct descendant(s)",
            parent_branch,
            len(children),
        )
        scheduled: set[str] = set()

        def _enqueue(child_row: TaskRow, parent_for_child: str) -> None:
            child_id = str(child_row["id"])
            if child_id in scheduled:
                return
            scheduled.add(child_id)
            self.store.mark_needs_parent_rebase(child_id)
            if child_id in futures:
                _rt.log.info(
                    "cascade rebase: %s has active worker; flagged needs_parent_rebase",
                    child_id,
                )
            else:
                self._schedule_rebase_to_parent_tip(
                    child_id,
                    pool,
                    futures,
                    review_response_futures,
                    parent_branch=parent_for_child,
                )
            child_branch = child_row.get("branch")
            if child_branch:
                grandchildren = self.store.children_of_parent_branch(str(child_branch))
                for gc in grandchildren:
                    _enqueue(gc, str(child_branch))

        for child in children:
            _enqueue(child, parent_branch)

    def _cascade_walk_should_proceed(self: Any, parent_branch: str) -> bool:
        """Plan 31 cascade-walk-level coalesce. Suppress redundant descendant
        walks for the same parent within `cfg.rebase_coalesce_window_s`.
        """
        window = self.cfg.rebase_coalesce_window_s
        if window <= 0:
            return True
        if not hasattr(self, "_last_cascade_walk_ts"):
            self._last_cascade_walk_ts = {}
        now = _rt.time.time()
        last = self._last_cascade_walk_ts.get(parent_branch)
        if last is not None and (now - last) < window:
            _rt.log.info(
                "cascade-walk coalesce: parent %s last walked %.1fs ago < %ds window; skipping",
                parent_branch,
                now - last,
                window,
            )
            return False
        self._last_cascade_walk_ts[parent_branch] = now
        return True

    def _task_id_for_branch(self: Any, branch: str) -> str | None:
        """Plan 32 helper: reverse-lookup a task id from its branch name.
        Used to translate cascade triggers (keyed on branch) into the
        task-id-keyed propagate_parent_* APIs."""
        with self.store._tx_lock:
            r = self.store.conn.execute("SELECT id FROM tasks WHERE branch = ? LIMIT 1", (branch,)).fetchone()
        return str(r["id"]) if r else None

    def _remote_branch_exists(self: Any, branch: str) -> bool:
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
            return True
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
        """Schedule a rebase-to-main worker future. Used when the parent
        branch is gone (parent merged) or this task's own PR is CONFLICTING.
        """
        if not self._begin_scheduled_rebase(task_id, trigger_reason, "main"):
            return
        fut = pool.submit(self._run_rebase_to_main_one, task_id)
        futures[task_id] = fut
        review_response_futures.add(task_id)

    def _schedule_rebase_to_parent_tip(
        self: Any,
        task_id: str,
        pool: ThreadPoolExecutor,
        futures: dict[str, Future],
        review_response_futures: set[str],
        *,
        parent_branch: str,
    ) -> None:
        """Schedule a rebase-to-parent-tip worker future. Used when a
        parent's branch advanced via push/fixup (parent still un-merged).
        """
        if not self._begin_scheduled_rebase(task_id, "parent_tip_advanced", "parent_tip"):
            return
        _rt.log.info(
            "scheduling rebase-to-parent-tip for task %s (parent branch %s)",
            task_id,
            parent_branch,
        )
        fut = pool.submit(self._run_rebase_to_parent_tip_one, task_id)
        futures[task_id] = fut
        review_response_futures.add(task_id)

    def _begin_scheduled_rebase(
        self: Any,
        task_id: str,
        trigger_reason: str,
        target_kind: str,
    ) -> bool:
        """Common pre-flight for both schedulers. Coalesces, stashes
        pre-rebase state, transitions to REBASING_TO_MAIN. Returns False
        when the trigger should be suppressed (coalesce window, missing row).
        """
        row = self.store.get(task_id)
        if row is None:
            _rt.log.warning("_begin_scheduled_rebase: task %s missing from store", task_id)
            return False
        window = self.cfg.rebase_coalesce_window_s
        if window > 0:
            last_ts = self.store.get_last_rebase_scheduled_ts(task_id)
            if last_ts is not None and (_rt.time.time() - last_ts) < window:
                _rt.log.info(
                    "task %s: coalescing rebase trigger (%s/%s) — last trigger %.1fs ago < %ds",
                    task_id,
                    trigger_reason,
                    target_kind,
                    _rt.time.time() - last_ts,
                    window,
                )
                return False
        self.store.set_last_rebase_scheduled(task_id, _rt.time.time())
        pre_state = str(row.get("state") or State.PENDING.value)
        self.store.set_pre_rebase_state(task_id, pre_state)
        reason_label = {
            "parent_merged": "parent merged",
            "parent_tip_advanced": "parent tip advanced",
            "sibling_conflict": "sibling conflict via mergeable=CONFLICTING",
            "worker_checkpoint_flag": "worker checkpoint flag",
            "manual": "manual",
            "parent_merge_auto_close": "parent merge auto-close",
        }.get(trigger_reason, trigger_reason)
        fsm_runtime.enter_rebasing_to_main(
            self.store,
            task_id,
            note=f"rebasing onto {target_kind} ({reason_label}; was {pre_state})",
        )
        _rt.log.info(
            "scheduling rebase (%s) for task %s (pre-rebase state %s, reason %s)",
            target_kind,
            task_id,
            pre_state,
            trigger_reason,
        )
        return True

    def _run_rebase_to_main_one(self: Any, task_id: str):
        # Plan 32: rebase entries only fire on spec tasks (merge-nodes aren't
        # rebased — they're rebuilt). Use the factory so `kind` dispatch
        # routes correctly even if a future caller invokes it for a merge-node.
        if task_id in self.dag.nodes:
            node: Any = self.dag.nodes[task_id]
        else:
            node = task_id
        worker = _rt.build_task_worker(self.cfg, self.dag, self.store, node)
        return worker.run_rebase_to_main()

    def _run_rebase_to_parent_tip_one(self: Any, task_id: str):
        if task_id in self.dag.nodes:
            node: Any = self.dag.nodes[task_id]
        else:
            node = task_id
        worker = _rt.build_task_worker(self.cfg, self.dag, self.store, node)
        return worker.run_rebase_to_parent_tip()
