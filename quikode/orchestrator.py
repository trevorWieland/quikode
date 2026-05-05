"""DAG-aware scheduler. Runs up to N task workers in parallel using threads."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import docker_env, github, github_graphql, notify, scheduler, triage
from .config import Config
from .dag import DAG
from .github_graphql import ReviewThread
from .state import State, Store
from .worker import TaskWorker

log = logging.getLogger("quikode.orchestrator")

_SKIP_MTIME_DIRS = {
    "target",
    "node_modules",
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".uv-cache",
    ".venv",
    "dist",
    "build",
}


def _worktree_mtime(path: Path) -> float | None:
    if not path.exists():
        return None
    latest = 0.0
    try:
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in _SKIP_MTIME_DIRS]
            for f in files:
                try:
                    mt = (Path(root) / f).stat().st_mtime
                    latest = max(latest, mt)
                except OSError:
                    continue
    except OSError:
        return None
    return latest if latest > 0 else None


class Orchestrator:
    def __init__(
        self,
        cfg: Config,
        dag: DAG,
        store: Store,
        *,
        task_filter: set[str] | None = None,
        awaiting_blocks_dependents: bool = True,
    ):
        self.cfg = cfg
        self.dag = dag
        self.store = store
        self.task_filter = task_filter  # if set, only schedule these IDs (and their deps)
        self.awaiting_blocks_dependents = awaiting_blocks_dependents
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        # Seed the store with PENDING entries for everything in scope
        scope = self.task_filter or set(self.dag.nodes)
        for nid in scope:
            self.store.upsert_pending(nid)

        # Warned-once tracker: avoid spamming the same stall warning every 5s
        warned: dict[str, float] = {}
        last_stats_sample = 0.0
        # Track previously-known MERGED set so we can detect external merges
        # (user merged a PR while the task was AWAITING_MERGE). When the merged
        # set grows, the new entries trigger intent reviews on in-flight tasks.
        previously_merged = self.store.completed_ids() & scope

        with ThreadPoolExecutor(max_workers=self.cfg.max_parallel, thread_name_prefix="qk-task") as pool:
            futures: dict[str, Future] = {}
            # Track which futures are review-response futures vs full-run futures.
            # Reaping treats them identically; this set just lets the heartbeat
            # surface a count for visibility.
            review_response_futures: set[str] = set()
            while not self._stop.is_set():
                self._check_stalls(warned, futures, review_response_futures)
                now = time.time()
                if now - last_stats_sample >= self.cfg.container_stats_sample_seconds:
                    self._sample_container_stats()
                    last_stats_sample = now
                # v3 Phase B: review-watcher pass — poll AWAITING_MERGE tasks
                # for new review threads + detect MERGED/CLOSED. Submits a
                # `run_review_response` future for any task with unresolved
                # threads to address.
                self._poll_review_threads(pool, futures, review_response_futures)
                # Write heartbeat early in the tick so it fires even on the
                # all-AWAITING_MERGE branch below (which `continue`s). Without
                # this, the watcher runs but staleness detection thinks the
                # daemon is dead.
                self._write_heartbeat(len(futures), len(review_response_futures))
                # Detect external merges (e.g. user merged an AWAITING_MERGE PR)
                # and trigger intent reviews on in-flight tasks. Phase B's
                # worker-side `_run_intent_review` is the heavy part; we just
                # raise the flag here.
                current_merged = self.store.completed_ids() & scope
                newly_merged = current_merged - previously_merged
                if newly_merged:
                    in_flight = [
                        r["id"]
                        for r in self.store.in_state(
                            State.DOING_SUBTASK,
                            State.CHECKING_SUBTASK,
                            State.TRIAGING_SUBTASK,
                            State.PR_OPENING,
                            State.POLLING_CI,
                        )
                        if r["id"] not in newly_merged
                    ]
                    if in_flight:
                        for merged_id in newly_merged:
                            self.store.mark_needs_intent_review(in_flight, triggered_by=merged_id)
                        log.info(
                            "external merge(s) detected (%s) → flagged %d in-flight task(s) for intent review",
                            ", ".join(sorted(newly_merged)),
                            len(in_flight),
                        )
                previously_merged = current_merged
                # Schedule new work if we have capacity
                while len(futures) < self.cfg.max_parallel:
                    nxt = self._pick_next(scope, in_flight=set(futures.keys()))
                    if nxt is None:
                        break
                    log.info("scheduling task %s", nxt)
                    fut = pool.submit(self._run_one, nxt)
                    futures[nxt] = fut

                if not futures:
                    if self._all_done(scope):
                        log.info("all tasks reached terminal state")
                        return
                    time.sleep(5)
                    continue

                # Reap finished
                done_ids = [tid for tid, f in futures.items() if f.done()]
                for tid in done_ids:
                    try:
                        outcome = futures[tid].result()
                        log.info("task %s → %s (%s)", tid, outcome.final_state, outcome.note)
                        # v2 Phase B: a MERGE may shift the world for in-flight
                        # tasks. Mark them so the worker triggers an intent review
                        # at its next safe checkpoint.
                        if outcome.final_state == State.MERGED:
                            in_flight = [
                                r["id"]
                                for r in self.store.in_state(
                                    *[
                                        State.DOING_SUBTASK,
                                        State.CHECKING_SUBTASK,
                                        State.TRIAGING_SUBTASK,
                                        State.PR_OPENING,
                                        State.POLLING_CI,
                                    ]
                                )
                                if r["id"] != tid
                            ]
                            if in_flight:
                                self.store.mark_needs_intent_review(in_flight, triggered_by=tid)
                                log.info(
                                    "flagged %d in-flight task(s) for intent review after %s merged",
                                    len(in_flight),
                                    tid,
                                )
                    except Exception as e:
                        log.exception("task %s raised: %s", tid, e)
                    del futures[tid]
                    review_response_futures.discard(tid)

                if not done_ids:
                    time.sleep(2)

    def _pick_next(self, scope: set[str], in_flight: set[str]) -> str | None:
        """Return the next ready task id, or None.

        Eligibility rules:
        - With `stacking_strategy=off` (default), a task is only ready when
          all its deps are MERGED.
        - With `"within-milestone"` or `"aggressive"`, a dep can be in a
          stack-ready state (has a remote branch we can fork off); the child
          branches off the dep's branch instead of main.

        Selection: among all eligible candidates, pick the highest-priority
        one via `_score_candidate`. Priority axes (see scorer for weights):
        type (stacked > fresh-root for chain throughput), unblock_boost
        (favors tasks with more downstream dependents in scope), id_tiebreak
        (lower R-XXXX = higher, preserves milestone order). This replaces
        the v3.0 "first eligible by sorted ID" rule, which under-utilized
        the orchestrator under sustained Phase 3+ parallelism by ignoring
        the chain-unlock value of each candidate.

        Side-effect on the chosen task: stamp/clear `parent_pr_branch` so
        the worker's `_provision_worktree` branches off the right ref.
        Side-effects only fire on the picked candidate, not on every
        eligible one.
        """
        candidates = self._collect_pick_candidates(scope, in_flight)
        if not candidates:
            return None
        # Sort: highest score first, lower task_id breaks score ties (so a
        # tied R-001 wins over R-005, preserving milestone-order intuition).
        candidates.sort(key=lambda c: (-self._score_candidate(c, scope), c["task_id"]))
        best = candidates[0]
        self._apply_pick_side_effects(best)
        return str(best["task_id"])

    def _collect_pick_candidates(self, scope: set[str], in_flight: set[str]) -> list[dict]:
        """Enumerate all tasks currently eligible for scheduling. No side
        effects. Each candidate carries the metadata `_apply_pick_side_effects`
        and `_score_candidate` need so they can act without re-deriving.

        Stacking eligibility honors `cfg.stacking_readiness` via
        `scheduler.is_parent_stack_ready`. Resume signals (has_open_pr,
        subtask done/total) are pulled per candidate via
        `scheduler._resume_signals` and consumed by `_score_candidate`.
        """
        completed = self.store.completed_ids() & scope
        active = self.store.active_ids() & scope
        candidates: list[dict] = []
        for nid in sorted(scope):
            if nid in completed or nid in active or nid in in_flight:
                continue
            n = self.dag.nodes.get(nid)
            if n is None:
                continue
            row = self.store.get(nid)
            if row and row["state"] not in (State.PENDING.value,):
                continue
            in_scope_deps = [d for d in n.depends_on if d in self.dag.nodes]
            unmet = [d for d in in_scope_deps if d not in completed]
            has_open_pr, sub_done, sub_total = scheduler._resume_signals(row, self.store)
            base_meta = {
                "row": row,
                "has_open_pr": has_open_pr,
                "subtask_done": sub_done,
                "subtask_total": sub_total,
            }
            if not unmet:
                candidates.append({"task_id": nid, "is_stacked": False, "unmet": [], **base_meta})
                continue
            if self.cfg.stacking_strategy == "off":
                continue
            unmet_states = {d: (self.store.get(d) or {}).get("state") for d in unmet}
            if not all(
                scheduler.is_parent_stack_ready(
                    cfg=self.cfg,
                    parent_state=s,
                    parent_id=d,
                    store=self.store,
                )
                for d, s in unmet_states.items()
            ):
                continue
            if self.cfg.stacking_strategy == "within-milestone" and not all(
                self.dag.nodes[d].milestone == n.milestone for d in unmet
            ):
                continue
            depth = self._stack_depth(unmet[0])
            if depth >= self.cfg.stacking_max_depth:
                continue
            if self._would_form_cycle(nid, unmet[0]):
                log.warning(
                    "refusing to stack %s on %s — would form parent_task_id cycle",
                    nid,
                    unmet[0],
                )
                continue
            root = self._stack_root(unmet[0])
            if self._stack_size_under_root(root) >= self.cfg.stacking_max_breadth_per_root:
                log.warning(
                    "task %s would exceed stacking_max_breadth_per_root (%d) under root %s",
                    nid,
                    self.cfg.stacking_max_breadth_per_root,
                    root,
                )
                continue
            candidates.append({"task_id": nid, "is_stacked": True, "unmet": unmet, **base_meta})
        return candidates

    def _score_candidate(self, c: dict, scope: set[str]) -> int:
        """Delegate to the shared scorer in `scheduler.py` so workers see
        the same priority signal at yield time."""
        return scheduler.score_candidate(
            task_id=c["task_id"],
            is_stacked=c["is_stacked"],
            dag=self.dag,
            scope=scope,
            has_open_pr=c.get("has_open_pr", False),
            subtask_done=c.get("subtask_done", 0),
            subtask_total=c.get("subtask_total", 0),
        )

    def _apply_pick_side_effects(self, c: dict) -> None:
        """Run any stamping/clearing required for the picked candidate.

        For stacked children, stamp the full parent chain (v3.5 Phase 2):
        `parent_task_ids` / `parent_branches` / `parent_pr_branches` JSON
        arrays carry every unmet stack-ready parent. The legacy scalar
        columns (`parent_task_id`, `parent_branch`, `parent_pr_branch`)
        also get the *deepest-id* entry so single-parent code paths
        (rebase-to-main, parent_branch lookups) keep working unchanged.
        For multi-parent picks (>1 unmet) the worker's provisioning step
        constructs a synthetic merge-base branch off this list.

        For fresh roots, clear any stale parent linkage left over from a
        prior stacking that no longer applies.
        """
        nid = c["task_id"]
        if not c["is_stacked"]:
            # Clear any stale parent linkage from a prior stacking round.
            if self.store.get_parent_task_ids(nid):
                self.store.clear_parent_branch(nid)
                self.store.set_parent_merge_base(nid, branch=None, sha=None)
            return
        # Collect the (id, branch) pairs for every unmet stack-ready parent.
        # Sorted by id for determinism — set_parent_chain stamps the JSON
        # arrays in this order so re-picks land at the same primary parent.
        unmet_with_branch: list[tuple[str, str]] = []
        for d in sorted(c["unmet"]):
            pr = self.store.get(d)
            if pr and pr.get("branch"):
                unmet_with_branch.append((d, str(pr["branch"])))
        if unmet_with_branch:
            ids = [tid for tid, _ in unmet_with_branch]
            branches = [br for _, br in unmet_with_branch]
            self.store.set_parent_chain(
                nid,
                parent_task_ids=ids,
                parent_branches=branches,
                parent_pr_branches=branches,
            )
            # Reset any prior merge-base bookkeeping; the worker recomputes
            # on the next provision when len(unmet) > 1.
            self.store.set_parent_merge_base(nid, branch=None, sha=None)

    def _stack_depth(self, task_id: str) -> int:
        """Compute how deep the stacking DAG is starting from `task_id`.

        v3.5 Phase 2 follow-up: walks the multi-parent DAG via
        `parent_task_ids`, taking the **maximum** depth across all paths
        upward. A child with two parents at depths 3 and 5 returns 5 (the
        deepest path), so the caller's `depth >= stacking_max_depth`
        check rejects when *any* path is too deep. Falls back to the
        scalar `parent_task_id` for legacy rows that haven't been
        backfilled yet.

        Defensive: a parent-DAG cycle would otherwise loop forever. The
        `visited` set short-circuits; on cycle detection we return a
        sentinel above max-depth so the caller refuses.
        """
        # `on_stack` tracks the *current* recursion path so a cycle (a→b→a)
        # is detected. We don't memoize cross-path visits — the same node
        # reached via different paths can have different depths if the
        # graph has DAG-shape diamonds, but our caller only cares about
        # max-depth so memoizing-then-reusing is also correct. We pick the
        # simpler stack-only variant since DAG depth maxes don't change
        # from re-traversal cost in practice (workspaces are small).
        # Match the legacy scalar-chain semantics: depth counts nodes in the
        # chain *inclusive* of the starting task. A root returns 1; B
        # stacked on A returns 2; etc. Critical so existing callers'
        # `depth >= cfg.stacking_max_depth` checks reject at the same
        # boundary they always have.
        on_stack: set[str] = set()
        cycle_detected = [False]

        def _depth(node: str) -> int:
            if node in on_stack:
                cycle_detected[0] = True
                return 0
            on_stack.add(node)
            try:
                parents = self._parents_of(node)
                if not parents:
                    return 1
                return 1 + max(_depth(p) for p in parents)
            finally:
                on_stack.discard(node)

        depth = _depth(task_id)
        if cycle_detected[0]:
            log.warning("stacking cycle detected from %s — refusing further stacking", task_id)
            return max(depth, self.cfg.stacking_max_depth + 1)
        return depth

    def _parents_of(self, task_id: str) -> list[str]:
        """Return the multi-parent list for `task_id` — single source of
        truth for the stack-walk helpers."""
        return self.store.get_parent_task_ids(task_id)

    def _would_form_cycle(self, child_id: str, prospective_parent_id: str) -> bool:
        """Multi-parent cycle detection. Walking the parent DAG from
        `prospective_parent_id` upward, would we re-encounter `child_id`?
        If so, stacking on this parent forms a cycle and must be refused.
        BFS over `parent_task_ids` so a multi-path cycle (a→b, b→c, c→a)
        is caught even when the cycle isn't on the lowest-id path.
        """
        if child_id == prospective_parent_id:
            return True
        seen: set[str] = set()
        frontier = [prospective_parent_id]
        while frontier:
            cur = frontier.pop()
            if cur in seen:
                continue
            seen.add(cur)
            if cur == child_id:
                return True
            for p in self._parents_of(cur):
                if p not in seen:
                    frontier.append(p)
        return False

    def _stack_root(self, task_id: str) -> str:
        """Find a stacking root for `task_id` — the topmost non-stacked
        ancestor. With multi-parent stacking, the DAG can have multiple
        roots; we return the lexicographically lowest one for
        determinism (callers use this only for the breadth-cap key).
        Cycle-safe."""
        seen: set[str] = set()
        frontier = [task_id]
        roots: list[str] = []
        while frontier:
            cur = frontier.pop()
            if cur in seen:
                continue
            seen.add(cur)
            parents = self._parents_of(cur)
            if not parents:
                roots.append(cur)
                continue
            for p in parents:
                if p not in seen:
                    frontier.append(p)
        if not roots:
            return task_id
        return min(roots)

    def _stack_size_under_root(self, root_task_id: str) -> int:
        """How many tasks (across the whole tree) are stacked off this
        root, including the root itself? Used for the breadth-cap check."""
        # Defensive linear scan — workspaces usually have <300 tasks total.
        count = 0
        for r in self.store.all_tasks():
            if self._stack_root(str(r["id"])) == root_task_id:
                count += 1
        return count

    def _sample_container_stats(self) -> None:
        """Periodic snapshot of cpu/mem usage for every active task's dev container.
        Records to `container_stats` table — used by `quikode resources` and
        `quikode briefing` to show live + max-RSS."""
        for row in self.store.in_state(
            *[
                State.PROVISIONING,
                State.PLANNING,
                State.DOING_SUBTASK,
                State.CHECKING_SUBTASK,
                State.TRIAGING_SUBTASK,
                State.COMMITTING,
                State.PUSHING,
                State.PR_OPENING,
            ]
        ):
            cid = row.get("container_id")
            if not cid:
                continue
            # We don't have the handle on the orchestrator side, but the
            # container name follows a stable pattern: qk-<slug>-<hex>-dev.
            # We can infer it by matching the running containers.
            for c in docker_env.list_quikode_containers():
                if c["name"].endswith("-dev") and c["id"].startswith(cid[:12]):
                    stats = docker_env.sample_container_stats(c["name"])
                    if stats:
                        self.store.record_container_stats(
                            row["id"],
                            c["name"],
                            stats.get("cpu_pct"),
                            stats.get("mem_bytes"),
                            stats.get("mem_pct"),
                        )
                    break

    def _check_stalls(
        self,
        warned: dict[str, float],
        futures: dict[str, Future] | None = None,
        review_response_futures: set[str] | None = None,
    ) -> None:
        """Warn once per stall-window when a DOING task's worktree has been quiet,
        AND auto-recover review-response futures that are silently stalled.

        Only DOING is expected to produce file edits — planner/checker/triage
        phases are read-only. We only warn during DOING.

        v3 follow-up to the 2026-05-04 R-0002 / R-0015 pool-slot leaks:
        when a `addressing_feedback` task has logged ZERO agent_call
        activity for `cfg.stall_warn_seconds` (default 1800s = 30min),
        the future is almost certainly leaked (silently crashed before
        the first agent invocation). We force-cancel + reset the task to
        AWAITING_MERGE so the watcher's next tick re-dispatches against
        a fresh pool slot. Without this, R-0002-style stalls persist
        indefinitely (every minute of stall = $0 progress + 1 reserved
        pool slot starving real work).
        """
        threshold = self.cfg.stall_warn_seconds
        if threshold <= 0:
            return
        now = time.time()
        for row in self.store.in_state(State.DOING_SUBTASK):
            wt = row.get("worktree_path")
            if not wt:
                continue
            mt = _worktree_mtime(Path(wt))
            if mt is None:
                continue
            quiet = now - mt
            if quiet < threshold:
                warned.pop(row["id"], None)
                continue
            last_warned = warned.get(row["id"], 0)
            if now - last_warned < threshold:
                continue  # already warned in this window
            log.warning(
                "task %s appears stalled: doer worktree quiet for %d min (no file edits since %s)",
                row["id"],
                int(quiet // 60),
                time.strftime("%H:%M:%S", time.localtime(mt)),
            )
            warned[row["id"]] = now

        # Stalled review-response detector. Triggered by direct observation
        # of the leak: future submitted, no agent_call ever fires, task sits
        # in addressing_feedback for 30+ min holding a pool slot.
        if futures is None or review_response_futures is None:
            return
        for tid in list(review_response_futures):
            row = self.store.get(tid)
            if row is None:
                continue
            if row["state"] != State.ADDRESSING_FEEDBACK.value:
                continue  # not stalled — task moved on
            # Most-recent agent_call timestamp for this task.
            last_call = self.store.conn.execute(
                "SELECT MAX(ts) FROM agent_calls WHERE task_id = ?",
                (tid,),
            ).fetchone()
            last_ts = float(last_call[0]) if last_call and last_call[0] else 0.0
            # last_ts may be from a PRIOR review-response cycle. Compare
            # against when this task last entered ADDRESSING_FEEDBACK.
            entered = self.store.conn.execute(
                "SELECT MAX(ts) FROM state_log WHERE task_id = ? AND to_state = ?",
                (tid, State.ADDRESSING_FEEDBACK.value),
            ).fetchone()
            entered_ts = float(entered[0]) if entered and entered[0] else 0.0
            # Effective "silence start" = max(entered, last agent_call). If
            # silent since either, that's our window.
            silence_start = max(entered_ts, last_ts)
            silence = now - silence_start
            if silence < threshold:
                continue
            log.error(
                "task %s: review-response stalled %d min — no agent_call since %s; "
                "resetting to AWAITING_MERGE for re-dispatch",
                tid,
                int(silence // 60),
                time.strftime("%H:%M:%S", time.localtime(silence_start)),
            )
            # Cancel the leaked Future (best-effort — may already be in a
            # broken state, can't .cancel() if running, but discard from
            # tracking sets so the slot frees on the next reap pass).
            fut = futures.get(tid)
            if fut is not None:
                fut.cancel()
                # Best-effort: if cancel fails because the future is "running"
                # (the silent-leak case), the future is wedged — drop it from
                # `futures` directly so the slot frees. The Future object will
                # eventually be garbage-collected; the worker thread it points
                # to is presumably already dead.
                futures.pop(tid, None)
            review_response_futures.discard(tid)
            # Reset to AWAITING_MERGE so the watcher's next tick re-dispatches.
            self.store.transition(
                tid,
                State.PENDING_CI,
                note=(
                    f"orchestrator force-recovery: review-response stalled "
                    f"{int(silence // 60)}min, slot freed for re-dispatch"
                ),
            )

    def _all_done(self, scope: set[str]) -> bool:
        """All tasks reached a TRULY terminal state (no orchestrator work left).

        With v3, AWAITING_MERGE is NOT terminal: the orchestrator's review
        watcher polls those PRs for new threads + human merge. We keep the
        loop alive on PENDING_CI, ADDRESSING_FEEDBACK, REBASING_TO_MAIN,
        CONFLICT_RESOLVING, INTENT_REVIEWING, and the subtask-loop states.

        The four genuine terminal states — MERGED, BLOCKED, FAILED,
        ABORTED — are the only ones that count toward "done."
        """
        terminal = {
            State.MERGED.value,
            State.BLOCKED.value,
            State.FAILED.value,
            State.ABORTED.value,
        }
        for nid in scope:
            row = self.store.get(nid)
            if not row:
                return False
            if row["state"] not in terminal:
                return False
        return True

    def _run_one(self, task_id: str):
        node = self.dag.nodes[task_id]
        worker = TaskWorker(self.cfg, self.dag, self.store, node)
        return worker.run()

    # ----- v3 Phase B: review-watcher pass -----

    def _poll_review_threads(
        self,
        pool: ThreadPoolExecutor,
        futures: dict[str, Future],
        review_response_futures: set[str],
    ) -> None:
        """One review-watcher tick.

        For every AWAITING_MERGE task whose last poll was older than
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
        now = time.time()
        cutoff = now - self.cfg.review_poll_interval_s
        candidates = self.store.tasks_needing_review_poll(cutoff=cutoff)
        for task_row in candidates:
            task_id = task_row["id"]
            # Skip tasks that already have an in-flight future (e.g. a
            # review response already running, or somehow re-entered the
            # active set).
            if task_id in futures:
                continue
            pr_number = task_row.get("pr_number")
            if not pr_number:
                # No PR opened yet (e.g. AWAITING_MERGE from a no-diff path).
                # Mark polled and move on; nothing to do.
                self.store.set_field(task_id, last_review_poll_ts=now)
                continue
            repo = self._repo_identifier(task_row)
            if not repo:
                log.warning(
                    "task %s: cannot derive repo identifier from pr_url; skipping review poll", task_id
                )
                self.store.set_field(task_id, last_review_poll_ts=now)
                continue

            # 1. Check PR state — caught-up MERGE/CLOSE detection.
            pr_status = github.poll_pr(self.cfg.repo_path, int(pr_number))

            # v3.5 Phase 2 follow-up: cascade-on-push detection. If the
            # PR's head sha changed since our last poll, every descendant
            # whose merge-base depended on this branch needs to rebase.
            # Fires for OPEN PRs only — MERGED/CLOSED handled below in the
            # existing branches.
            parent_branch_for_cascade = str(task_row.get("branch") or "")
            if pr_status.state == "OPEN" and pr_status.head_sha and parent_branch_for_cascade:
                last_seen = self.store.get_last_observed_branch_tip_sha(task_id)
                if last_seen and last_seen != pr_status.head_sha:
                    log.info(
                        "task %s: branch %s tip advanced %s → %s; scheduling cascade rebase for descendants",
                        task_id,
                        parent_branch_for_cascade,
                        last_seen[:8],
                        pr_status.head_sha[:8],
                    )
                    self._schedule_cascade_rebase(
                        parent_branch_for_cascade, pool, futures, review_response_futures
                    )
                # Always stamp the latest tip so the next tick has a baseline.
                self.store.set_last_observed_branch_tip_sha(task_id, pr_status.head_sha)

            if pr_status.state == "MERGED":
                # Capture the parent's branch BEFORE transitioning — we need
                # it to find children stacked on this branch. The transition
                # itself doesn't clear `branch`, but the branch field is the
                # only stable handle from this row to children's
                # parent_pr_branch; reading it from the row is cleaner than
                # re-querying after the state change.
                parent_branch = str(task_row.get("branch") or "")
                self.store.transition(task_id, State.MERGED, note="merged on github")
                self.store.set_field(task_id, last_review_poll_ts=now)
                # v3 Phase C: auto-rebase children stacked on this branch.
                if parent_branch:
                    self._schedule_rebases_for_merged_parent(
                        parent_branch, pool, futures, review_response_futures
                    )
                continue
            if pr_status.state == "CLOSED":
                # GitHub auto-closes a child PR when its base branch is
                # deleted (which happens immediately on the parent's
                # `--delete-branch` merge). If THIS task has a stacked
                # parent that just merged (parent_pr_branch set, parent
                # task in MERGED state, AND base branch on the PR no
                # longer exists), the close is a side-effect of github
                # cleanup, not a deliberate user close. Schedule a rebase
                # + fresh PR instead of aborting.
                stacked_parents = self.store.get_parent_branches(task_id)
                pr_base_ref = pr_status.base_ref_name or ""
                if (
                    stacked_parents
                    and pr_base_ref
                    and pr_base_ref != self.cfg.base_branch
                    and not self._remote_branch_exists(pr_base_ref)
                ):
                    log.info(
                        "task %s: PR #%s auto-closed — base %s deleted by parent merge; "
                        "scheduling rebase-to-main + re-PR",
                        task_id,
                        pr_number,
                        pr_base_ref,
                    )
                    self.store.set_field(task_id, last_review_poll_ts=now)
                    self._schedule_rebase_to_main(
                        task_id,
                        pool,
                        futures,
                        review_response_futures,
                        trigger_reason="parent_merge_auto_close",
                    )
                    continue
                # Real close (user-initiated). Capture this task's own
                # branch BEFORE transitioning so we can clear stale
                # parent_pr_branch on any stacked children.
                parent_branch = str(task_row.get("branch") or "")
                self.store.transition(task_id, State.ABORTED, note="closed without merge")
                self.store.set_field(task_id, last_review_poll_ts=now)
                # v3 stacked-diffs fix: parent ABORT means the stack base
                # no longer exists. Clear children's stale parent metadata
                # so their next provision/PR-open path goes against main.
                if parent_branch:
                    stranded = self.store.children_with_parent_branch(parent_branch)
                    for c in stranded:
                        self.store.clear_parent_branch(str(c["id"]))
                    if stranded:
                        log.info(
                            "parent %s closed without merge → cleared parent_pr_branch on %d child(ren)",
                            task_id,
                            len(stranded),
                        )
                continue

            # v3 enhancement: when a sibling task merges and creates a
            # mergeability conflict on this PR, GitHub flips mergeable to
            # CONFLICTING. The pre-v3 worker handled this inside _poll_pr_loop,
            # but with v3 the worker has exited; the daemon must trigger a
            # rebase + conflict-resolve cycle.
            if pr_status.mergeable == "CONFLICTING" and task_id not in futures:
                log.info("task %s: PR #%s is CONFLICTING — scheduling rebase to main", task_id, pr_number)
                self.store.set_field(task_id, last_review_poll_ts=now)
                self._schedule_rebase_to_main(
                    task_id,
                    pool,
                    futures,
                    review_response_futures,
                    trigger_reason="sibling_conflict",
                )
                continue

            # v3 fix (live regression on R-0002): GitHub CI can transition
            # to FAILURE *after* the worker has handed off to AWAITING_MERGE
            # — typical sequence is response cycle pushes a fixup commit,
            # worker exits to PENDING_CI, GitHub re-runs CI, CI fails
            # post-response. Pre-v3 the worker's _poll_pr_loop caught this
            # inline; v3 needs the daemon to dispatch a CI-fix cycle.
            #
            # We use the same fixup-decomposition path as for review
            # threads (kind="fixup-ci"). That gives us atomic per-slice
            # commits + reusing the existing planner/doer/checker loop.
            ci_failed = pr_status.checks_status == "failure" and pr_status.failed_checks
            if (
                ci_failed
                and task_id not in futures
                and len(futures) < self.cfg.max_parallel + self.cfg.review_response_extra_slots
            ):
                log.info(
                    "task %s: PR #%s CI failing (%d failed check(s)) — scheduling CI-fix cycle",
                    task_id,
                    pr_number,
                    len(pr_status.failed_checks),
                )
                self.store.set_field(task_id, last_review_poll_ts=now)
                self._schedule_ci_fix_response(task_id, pr_status, pool, futures, review_response_futures)
                continue

            # 2. Fetch + classify review threads.
            try:
                threads = github_graphql.get_review_threads(repo, int(pr_number))
            except Exception as e:
                log.warning("get_review_threads(%s, %s) raised: %s", repo, pr_number, e)
                threads = []
            to_address = self._classify_threads(task_id, threads)

            # 2b. v3.5: drive PENDING_CI ↔ AWAITING_REVIEW ↔ MERGE_READY based
            # on live CI + thread + settle-window signals. The classifier is
            # pure (no side effects) — we transition + refresh the in-memory
            # task_row so subsequent gates (auto-merge, notify-settled) see the
            # new state.
            target = self._classify_post_pr_target_state(task_row, pr_status, threads)
            if target is not None and target.value != task_row.get("state"):
                self.store.transition(
                    task_id,
                    target,
                    note=f"poll classified state → {target.value}",
                )
                task_row = dict(task_row, state=target.value)

            # 3. Update poll timestamp regardless — throttle is on poll cadence,
            # not on whether work was found.
            self.store.set_field(task_id, last_review_poll_ts=now)

            # 4. Cap on review rounds: codex-style reviewers can keep
            # finding nits indefinitely. cfg.review_rounds_max BLOCKs
            # the task once the count is exceeded so an operator can
            # decide whether to merge or close. Cap fires before the
            # round-N+1 dispatch.
            current_round = int(task_row.get("review_round") or 0)
            if to_address and current_round >= self.cfg.review_rounds_max:
                log.warning(
                    "task %s: review_rounds_max (%d) exhausted with %d unresolved thread(s); "
                    "BLOCKING for manual merge/close",
                    task_id,
                    self.cfg.review_rounds_max,
                    len(to_address),
                )
                self.store.transition(
                    task_id,
                    State.BLOCKED,
                    note=(
                        f"review_rounds_max ({self.cfg.review_rounds_max}) exhausted; "
                        f"{len(to_address)} thread(s) still unresolved. "
                        "Manual merge or close required."
                    ),
                    last_error=(
                        f"review_rounds_max={self.cfg.review_rounds_max} exhausted; "
                        f"{len(to_address)} unresolved threads remaining"
                    ),
                )
                self.store.set_field(task_id, last_review_poll_ts=now)
                continue

            # 4b. v3.5 Phase B: in-process Python triage. For each
            # actionable thread, call the sonnet classifier and split into
            # CORRECT (forward to planner) / INCORRECT (auto-reply + resolve)
            # / NEEDS_DISCUSSION (leave for human). Bounds the time spent in
            # TRIAGING_FEEDBACK so the daemon's tick stays cheap.
            if to_address:
                # Mark TRIAGING_FEEDBACK so the operator can see what's going
                # on (the row was just transitioned to PENDING_CI by the
                # post-PR classifier above).
                self.store.transition(
                    task_id,
                    State.TRIAGING_FEEDBACK,
                    note=f"classifying {len(to_address)} thread(s)",
                )
                task_row = dict(task_row, state=State.TRIAGING_FEEDBACK.value)
                plan_text = str(task_row.get("plan_text") or "")
                outcome = triage.triage_review_threads(
                    cfg=self.cfg,
                    plan_text=plan_text,
                    threads=list(to_address),
                )
                # Auto-reply + resolve INCORRECT threads in-process. The reply
                # uses REST `/comments/{databaseId}/replies` so it lands as a
                # proper thread reply (not a top-level PR comment); falls
                # back to silent-resolve if databaseId is unavailable. The
                # rationale is also stored on the audit row so operators can
                # trace why a thread was auto-resolved.
                for t, verdict in outcome.auto_resolved:
                    if verdict.reply and t.last_comment_database_id is not None:
                        try:
                            github_graphql.reply_to_review_thread(
                                repo=repo,
                                pr_number=int(pr_number),
                                last_comment_database_id=t.last_comment_database_id,
                                body=verdict.reply,
                            )
                        except Exception as e:
                            log.warning("auto-reply to thread %s failed: %s", t.thread_id, e)
                    try:
                        github_graphql.resolve_thread(t.thread_id)
                    except Exception as e:
                        log.warning("auto-resolve of thread %s failed: %s", t.thread_id, e)
                    self.store.mark_thread_addressed(
                        task_id,
                        t.thread_id,
                        f"auto-classifier-incorrect: {verdict.rationale[:80]}",
                    )
                if outcome.deferred:
                    log.info(
                        "task %s: %d thread(s) deferred to human review (needs_discussion)",
                        task_id,
                        len(outcome.deferred),
                    )
                if outcome.classifier_errors:
                    log.warning(
                        "task %s: %d classifier error(s) — those thread(s) fall through to ADDRESSING_FEEDBACK",
                        task_id,
                        outcome.classifier_errors,
                    )
                # The actionable list is the post-triage subset. If empty
                # (e.g. all threads INCORRECT), nothing to dispatch — drop
                # back to PENDING_CI so the next poll re-classifies.
                to_address = outcome.actionable_threads
                if not to_address:
                    self.store.transition(
                        task_id,
                        State.PENDING_CI,
                        note="triage handled all threads in-process; nothing to dispatch",
                    )
                    task_row = dict(task_row, state=State.PENDING_CI.value)
                    continue

            # 5. Schedule response if work found AND slack available.
            # Reviews are gated on `max_parallel + review_response_extra_slots`
            # rather than just `max_parallel` so response cycles can dispatch
            # even when the regular-worker pool is saturated. Without this,
            # post-PR rows accumulate unresolved threads under sustained
            # parallelism and the daemon never frees a slot to address them.
            review_cap = self.cfg.max_parallel + self.cfg.review_response_extra_slots
            if to_address and len(futures) < review_cap:
                self._schedule_review_response(task_id, to_address, pool, futures, review_response_futures)
                continue
            if to_address:
                log.info(
                    "task %s has %d unresolved review threads but pool is full (%d/%d); will retry next tick",
                    task_id,
                    len(to_address),
                    len(futures),
                    review_cap,
                )
                # If we transitioned to TRIAGING_FEEDBACK above but couldn't
                # dispatch, drop back to PENDING_CI so the next poll re-runs
                # the classifier from a clean state. Leaving the row at
                # TRIAGING_FEEDBACK indefinitely would mask the pool-full
                # condition and confuse the operator's view.
                if task_row.get("state") == State.TRIAGING_FEEDBACK.value:
                    self.store.transition(
                        task_id,
                        State.PENDING_CI,
                        note="pool full — re-deferring to next poll",
                    )
                continue

            # 5. v3.5 polish: auto-merge a MERGE_READY task when opt-in.
            # Other post-PR states (PENDING_CI / AWAITING_REVIEW) are
            # explicitly NOT eligible — the whole point of MERGE_READY is
            # that it's the single state where we know it's safe to land.
            if self.cfg.auto_merge_when_clean and task_row.get("state") == State.MERGE_READY.value:
                self._attempt_auto_merge(task_row, pr_status, threads)

            # 6. settled-task notification: ping the operator when the
            # task has reached MERGE_READY and stayed quiet for the notify
            # window. PENDING_CI / AWAITING_REVIEW intentionally don't
            # trigger — we'd be paging on incomplete state.
            if task_row.get("state") == State.MERGE_READY.value:
                self._maybe_notify_settled(task_row, pr_status, threads)

    def _attempt_auto_merge(
        self,
        task_row: Mapping[str, Any],
        pr_status: github.PRStatus,
        threads: list[ReviewThread],
    ) -> None:
        """Squash-merge `task_row`'s PR if it's safe to do so unattended.

        Preconditions (all must hold):
          - cfg.auto_merge_when_clean is True (caller checked, defensive recheck)
          - PR state == OPEN
          - PR mergeable == MERGEABLE
          - All checks SUCCESS (or none)
          - No unresolved review threads (regardless of bot status)
          - The task has been in AWAITING_MERGE for at least
            cfg.auto_merge_min_age_s

        On success: sets `auto_merged=1` and lets the next poll tick
        catch the actual MERGED transition through the existing path.
        Failures are logged but never raised — a transient `gh pr merge`
        error gets retried on the next watcher tick.
        """
        if not self.cfg.auto_merge_when_clean:
            return
        if pr_status.state != "OPEN":
            return
        if pr_status.mergeable != "MERGEABLE":
            return
        if pr_status.checks_status not in ("success", "none"):
            return
        # Every visible thread must be resolved — even bot threads, even
        # ones we wouldn't normally respond to. Auto-merging while a
        # human comment chain is open is a footgun.
        if any(not t.is_resolved for t in threads):
            return
        # Time-in-state check.
        last_change = self._last_state_change_ts(str(task_row["id"]))
        if last_change is not None and (time.time() - last_change) < self.cfg.auto_merge_min_age_s:
            return

        task_id = str(task_row["id"])
        pr_number = int(task_row.get("pr_number") or 0)
        if not pr_number:
            return
        log.info(
            "task %s: auto-merge preconditions met → gh pr merge --squash --delete-branch #%d",
            task_id,
            pr_number,
        )
        try:
            r = subprocess.run(
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
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("auto-merge for task %s raised %s; will retry on next tick", task_id, e)
            return
        if r.returncode != 0:
            log.warning(
                "auto-merge for task %s PR #%d failed (rc=%d): %s",
                task_id,
                pr_number,
                r.returncode,
                (r.stderr or r.stdout)[:300],
            )
            return
        self.store.set_field(task_id, auto_merged=1)
        log.info("task %s: PR #%d auto-merged successfully", task_id, pr_number)

    def _last_state_change_ts(self, task_id: str) -> float | None:
        """Most-recent `state_log` ts for a task, or None when missing."""
        r = self.store.conn.execute(
            "SELECT MAX(ts) AS ts FROM state_log WHERE task_id = ?", (task_id,)
        ).fetchone()
        if r is None:
            return None
        v = r["ts"]
        return float(v) if v is not None else None

    def _classify_post_pr_target_state(
        self,
        task_row: Mapping[str, Any],
        pr_status: github.PRStatus,
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
        if last_clean_entry is None or (time.time() - last_clean_entry) < quiet_s:
            return State.AWAITING_REVIEW
        return State.MERGE_READY

    def _last_clean_post_pr_entry_ts(self, task_id: str) -> float | None:
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
        self,
        task_row: Mapping[str, Any],
        pr_status: github.PRStatus,
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
        if self.cfg.notify_settled_channel == "none":
            return
        if pr_status.state != "OPEN" or pr_status.mergeable != "MERGEABLE":
            return
        if pr_status.checks_status not in ("success", "none"):
            return
        if any(not t.is_resolved for t in threads):
            return

        task_id = str(task_row["id"])
        # When did this task most recently enter MERGE_READY?
        entered = self.store.conn.execute(
            "SELECT MAX(ts) FROM state_log WHERE task_id = ? AND to_state = ?",
            (task_id, State.MERGE_READY.value),
        ).fetchone()
        entered_ts = float(entered[0]) if entered and entered[0] else 0.0
        if not entered_ts:
            return
        quiet_for = time.time() - entered_ts
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
        msg = notify.SettledMessage(
            task_id=task_id,
            title=title,
            pr_url=task_row.get("pr_url") or "",
            summary=summary,
            cost_usd=cost,
        )
        try:
            ok = notify.notify_settled(self.cfg, msg)
        except Exception as e:
            log.warning("notify_settled %s raised: %s", task_id, e)
            return
        if ok:
            self.store.set_field(task_id, last_notified_settled_ts=time.time())

    def _classify_threads(self, task_id: str, threads: list[ReviewThread]) -> list[ReviewThread]:
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
            # comment is newer than what we last saw at address time. We
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
        self,
        task_id: str,
        threads: list[ReviewThread],
        pool: ThreadPoolExecutor,
        futures: dict[str, Future],
        review_response_futures: set[str],
    ) -> None:
        """Mark the task ADDRESSING_FEEDBACK and submit a worker future."""
        self.store.transition(
            task_id,
            State.ADDRESSING_FEEDBACK,
            note=f"daemon scheduled response to {len(threads)} thread(s)",
        )
        log.info("scheduling review response for task %s (%d threads)", task_id, len(threads))
        fut = pool.submit(self._run_review_response_one, task_id, threads)
        futures[task_id] = fut
        review_response_futures.add(task_id)

    def _run_review_response_one(self, task_id: str, threads: list[ReviewThread]):
        node = self.dag.nodes[task_id]
        worker = TaskWorker(self.cfg, self.dag, self.store, node)
        return worker.run_review_response(threads)

    def _schedule_ci_fix_response(
        self,
        task_id: str,
        pr_status: github.PRStatus,
        pool: ThreadPoolExecutor,
        futures: dict[str, Future],
        review_response_futures: set[str],
    ) -> None:
        """Dispatch a CI-fix cycle when GitHub CI fails *after* the worker
        has handed off to AWAITING_MERGE. Re-uses the review-response
        worker entry mode (which fixup-decomposes into per-slice subtasks)
        with a CI-failure trigger context."""
        self.store.transition(
            task_id,
            State.ADDRESSING_FEEDBACK,
            note=f"daemon scheduled CI-fix for {len(pr_status.failed_checks)} failed check(s)",
        )
        log.info(
            "scheduling CI-fix for task %s (%d failed checks)",
            task_id,
            len(pr_status.failed_checks),
        )
        fut = pool.submit(self._run_ci_fix_response_one, task_id, pr_status)
        futures[task_id] = fut
        review_response_futures.add(task_id)

    def _run_ci_fix_response_one(self, task_id: str, pr_status: github.PRStatus):
        """Worker entry for daemon-detected CI failure on AWAITING_MERGE.

        Fetches the failed-check logs, builds a synthetic ReviewThread-shaped
        payload describing the CI failure, and routes through the same
        `run_review_response` path. The worker's fixup planner sees the
        failure context and emits CI-fix subtasks.
        """
        node = self.dag.nodes[task_id]
        worker = TaskWorker(self.cfg, self.dag, self.store, node)
        return worker.run_ci_fix_response(pr_status)

    # ----- v3 Phase C: stacked-diff auto-rebase on parent merge -----

    def _schedule_rebases_for_merged_parent(
        self,
        parent_branch: str,
        pool: ThreadPoolExecutor,
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
        log.info(
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
                    pr_status = github.poll_pr(self.cfg.repo_path, int(pr_number))
                except (OSError, subprocess.SubprocessError) as e:
                    log.warning(
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
                        log.info(
                            "child %s PR #%s mergeable + base intact; skipping rebase, cleared parent metadata",
                            child_id,
                            pr_number,
                        )
                        skipped += 1
                        continue
                    if pr_status.mergeable == "CONFLICTING":
                        log.info(
                            "child %s PR #%s CONFLICTING — scheduling rebase",
                            child_id,
                            pr_number,
                        )
                    elif not base_intact:
                        log.info(
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
                log.info(
                    "child %s has active worker; flagged needs_parent_rebase for inline handling",
                    child_id,
                )
                continue
            self._schedule_rebase_to_main(child_id, pool, futures, review_response_futures)
        if skipped:
            log.info(
                "parent branch %s: %d child(ren) skipped rebase (PR still mergeable)",
                parent_branch,
                skipped,
            )

    def _schedule_cascade_rebase(
        self,
        parent_branch: str,
        pool: ThreadPoolExecutor,
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
        log.info(
            "parent branch %s tip advanced → cascading rebase to %d direct descendant(s)",
            parent_branch,
            len(children),
        )
        # Track which descendants have been queued already so we don't
        # re-enqueue across recursion.
        scheduled: set[str] = set()

        def _enqueue(child_row: dict) -> None:
            child_id = str(child_row["id"])
            if child_id in scheduled:
                return
            scheduled.add(child_id)
            # Always raise the mid-flight flag. When the worker exits the
            # current safe checkpoint it'll pick this up and rebase inline.
            self.store.mark_needs_parent_rebase(child_id)
            if child_id in futures:
                log.info(
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

    def _remote_branch_exists(self, branch: str) -> bool:
        """Check if `branch` still exists on the configured remote.

        Used by the smart-rebase path to decide whether a child whose PR
        is currently MERGEABLE actually needs a rebase. If the remote
        branch is gone (parent merged with --delete-branch), github will
        have auto-closed the child PR and we DO need to recreate / rebase.
        """
        try:
            r = subprocess.run(
                ["git", "ls-remote", "--heads", self.cfg.pr_remote, branch],
                cwd=self.cfg.repo_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("ls-remote for %s failed: %s; assuming branch exists", branch, e)
            return True
        if r.returncode != 0:
            return True  # be conservative — assume present on error
        return bool(r.stdout.strip())

    def _schedule_rebase_to_main(
        self,
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
            log.warning("_schedule_rebase_to_main: task %s missing from store", task_id)
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
            if last_ts is not None and (time.time() - last_ts) < window:
                log.info(
                    "task %s: coalescing rebase trigger (%s) — last trigger %.1fs ago < %ds window",
                    task_id,
                    trigger_reason,
                    time.time() - last_ts,
                    window,
                )
                return
        self.store.set_last_rebase_scheduled(task_id, time.time())
        pre_state = str(row.get("state") or State.PENDING.value)
        # Stash the pre-rebase active state so the rebase worker can restore
        # it on success. AWAITING_MERGE flows back to AWAITING_MERGE; mid-loop
        # active states return where they were (the worker re-enters from the
        # FSM at the same point).
        self.store.set_pre_rebase_state(task_id, pre_state)
        reason_label = {
            "parent_merged": "parent merged",
            "sibling_conflict": "sibling conflict via mergeable=CONFLICTING",
            "worker_checkpoint_flag": "worker checkpoint flag",
            "manual": "manual",
        }.get(trigger_reason, trigger_reason)
        self.store.transition(
            task_id,
            State.REBASING_TO_MAIN,
            note=f"rebasing onto main ({reason_label}; was {pre_state})",
        )
        log.info(
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

    def _run_rebase_to_main_one(self, task_id: str):
        node = self.dag.nodes[task_id]
        worker = TaskWorker(self.cfg, self.dag, self.store, node)
        return worker.run_rebase_to_main()

    def _repo_identifier(self, task_row: Mapping[str, Any]) -> str:
        """Derive `owner/name` for GraphQL calls.

        Sources, in priority order:
          1. `task_row['repo']` if a future migration ever stores it directly
             (the plan doc references this; today's schema doesn't have it
             so we keep this branch for forward-compat).
          2. Parse from the row's `pr_url`
             (`https://github.com/<owner>/<name>/pull/<n>`).
          3. Fall back to `gh repo view --json nameWithOwner` against the
             repo path on disk. Cached on the orchestrator for the lifetime
             of the process.
        """
        explicit = task_row.get("repo")
        if isinstance(explicit, str) and "/" in explicit:
            return explicit
        pr_url = task_row.get("pr_url") or ""
        if isinstance(pr_url, str) and pr_url:
            m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/\d+", pr_url)
            if m:
                return f"{m.group(1)}/{m.group(2)}"
        # Last resort: gh repo view (cached).
        cached = getattr(self, "_repo_id_cache", None)
        if cached:
            return cached
        try:
            r = subprocess.run(
                ["gh", "repo", "view", "--json", "nameWithOwner"],
                cwd=self.cfg.repo_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("gh repo view failed: %s", e)
            return ""
        if r.returncode != 0:
            return ""
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            return ""
        repo_id = str(data.get("nameWithOwner") or "")
        if repo_id:
            self._repo_id_cache = repo_id
        return repo_id

    def _write_heartbeat(self, in_flight: int, addressing_feedback_futures: int) -> None:
        """Write a small JSON liveness blob to `state_dir/orchestrator.heartbeat`.

        Light touch — the supervisor loop in batch 8 will use this for
        crash-restart, and the TUI may read it for liveness display. For now
        this just records the file every tick.
        """
        try:
            self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
            awaiting_merge = len(self.store.in_state(State.PENDING_CI))
            responding = len(self.store.in_state(State.ADDRESSING_FEEDBACK))
            payload = {
                "ts": time.time(),
                "in_flight": in_flight,
                "awaiting_merge": awaiting_merge,
                "addressing_feedback": responding,
                "addressing_feedback_futures": addressing_feedback_futures,
            }
            (self.cfg.state_dir / "orchestrator.heartbeat").write_text(json.dumps(payload))
        except OSError as e:
            log.debug("heartbeat write failed: %s", e)
