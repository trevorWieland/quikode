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
from typing import Any, cast

from quikode import docker_env, github, github_graphql, triage
from quikode.config import Config
from quikode.dag import DAG
from quikode.github_graphql import ReviewThread
from quikode.orchestration import scheduler
from quikode.orchestration.merge_watch import MergeWatchMixin
from quikode.orchestration.rebase_watch import RebaseWatchMixin
from quikode.orchestration.review_watch import ReviewWatchMixin
from quikode.orchestration.supervision import SupervisionMixin
from quikode.state import State, Store, TaskRow
from quikode.workers.task_worker import TaskWorker

log = logging.getLogger("quikode.orchestrator")

__all__ = [
    "Orchestrator",
    "ReviewThread",
    "TaskRow",
    "TaskWorker",
    "_worktree_mtime",
    "cast",
    "docker_env",
    "github",
    "github_graphql",
    "subprocess",
    "time",
    "triage",
]

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


class Orchestrator(SupervisionMixin, ReviewWatchMixin, MergeWatchMixin, RebaseWatchMixin):
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
        scope = self.task_filter or set(self.dag.nodes)
        for nid in scope:
            self.store.upsert_pending(nid)
        warned: dict[str, float] = {}
        last_stats_sample = 0.0
        previously_merged = self.store.completed_ids() & scope

        with ThreadPoolExecutor(max_workers=self.cfg.max_parallel, thread_name_prefix="qk-task") as pool:
            futures: dict[str, Future] = {}
            review_response_futures: set[str] = set()
            while not self._stop.is_set():
                self._check_stalls(warned, futures, review_response_futures)
                last_stats_sample = self._sample_stats_if_due(last_stats_sample)
                self._poll_review_threads(pool, futures, review_response_futures)
                self._write_heartbeat(len(futures), len(review_response_futures))
                current_merged = self._flag_external_merges(scope, previously_merged)
                previously_merged = current_merged
                self._schedule_ready_tasks(pool, scope, futures)

                if not futures:
                    if self._all_done(scope):
                        log.info("all tasks reached terminal state")
                        return
                    time.sleep(5)
                    continue

                done_ids = self._reap_finished_tasks(futures, review_response_futures)
                if not done_ids:
                    time.sleep(2)

    def _sample_stats_if_due(self, last_stats_sample: float) -> float:
        now = time.time()
        if now - last_stats_sample >= self.cfg.container_stats_sample_seconds:
            self._sample_container_stats()
            return now
        return last_stats_sample

    def _flag_external_merges(self, scope: set[str], previously_merged: set[str]) -> set[str]:
        current_merged = self.store.completed_ids() & scope
        newly_merged = current_merged - previously_merged
        if newly_merged:
            in_flight = self._intent_review_targets(exclude=newly_merged)
            for merged_id in newly_merged:
                self.store.mark_needs_intent_review(in_flight, triggered_by=merged_id)
            if in_flight:
                log.info("external merge(s) detected (%s)", ", ".join(sorted(newly_merged)))
        return current_merged

    def _intent_review_targets(self, *, exclude: set[str]) -> list[str]:
        rows = self.store.in_state(
            State.DOING_SUBTASK,
            State.CHECKING_SUBTASK,
            State.TRIAGING_SUBTASK,
            State.PR_OPENING,
            State.PENDING_CI,
        )
        return [r["id"] for r in rows if r["id"] not in exclude]

    def _schedule_ready_tasks(
        self, pool: ThreadPoolExecutor, scope: set[str], futures: dict[str, Future]
    ) -> None:
        while len(futures) < self.cfg.max_parallel:
            nxt = self._pick_next(scope, in_flight=set(futures.keys()))
            if nxt is None:
                return
            log.info("scheduling task %s", nxt)
            futures[nxt] = pool.submit(self._run_one, nxt)

    def _reap_finished_tasks(
        self, futures: dict[str, Future], review_response_futures: set[str]
    ) -> list[str]:
        done_ids = [tid for tid, future in futures.items() if future.done()]
        for tid in done_ids:
            self._handle_finished_task(tid, futures[tid], futures)
            del futures[tid]
            review_response_futures.discard(tid)
        return done_ids

    def _handle_finished_task(self, task_id: str, future: Future, futures: dict[str, Future]) -> None:
        try:
            outcome = future.result()
        except Exception as e:
            log.exception("task %s raised: %s", task_id, e)
            return
        log.info("task %s -> %s (%s)", task_id, outcome.final_state, outcome.note)
        if outcome.final_state == State.MERGED:
            in_flight = self._intent_review_targets(exclude={task_id})
            if in_flight:
                self.store.mark_needs_intent_review(in_flight, triggered_by=task_id)

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
        # Plan 30: hard-tier filter — only fall through to stacked candidates
        # when no primary (no unmet deps) is pickable. Stacked work waits for
        # primaries to drain so the orchestrator never starves the high-fan-out
        # work, and stacked children that DO get scheduled have the strongest
        # possible foundation (parent in AWAITING_REVIEW + settled).
        candidates = scheduler.prefer_primary_candidates(candidates)
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
        arrays carry every unmet stack-ready parent. Single-parent code paths
        (rebase-to-main, parent_branch lookups) read the same chain data.
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
        older scalar parent data when present.

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
        # Match stored stack-chain semantics: depth counts nodes in the
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

    def _all_done(self, scope: set[str]) -> bool:
        """All tasks reached a TRULY terminal state (no orchestrator work left).

        Post-PR states are not terminal: the orchestrator's review watcher
        polls those PRs for new threads + human merge. We keep the
        loop alive on PENDING_CI, ADDRESSING_FEEDBACK, REBASING_TO_MAIN,
        CONFLICT_RESOLVING, TRIAGING_FEEDBACK, and the subtask-loop states.

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

    # ----- v3 Phase C: stacked-diff auto-rebase on parent merge -----

    def _repo_identifier(self, task_row: Mapping[str, Any]) -> str:
        """Derive `owner/name` for GraphQL calls.

        Sources, in priority order:
          1. `task_row['repo']` if a future schema stores it directly
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
        parsed = _parse_github_pr_repo(pr_url) if isinstance(pr_url, str) else ""
        if parsed:
            return parsed
        cached = getattr(self, "_repo_id_cache", None)
        if cached:
            return cached
        repo_id = _query_repo_identifier(self.cfg.repo_path)
        if repo_id:
            self._repo_id_cache = repo_id
        return repo_id


def _parse_github_pr_repo(pr_url: str) -> str:
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/\d+", pr_url)
    return f"{match.group(1)}/{match.group(2)}" if match else ""


def _query_repo_identifier(repo_path: Path) -> str:
    try:
        r = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner"],
            cwd=repo_path,
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
    return str(data.get("nameWithOwner") or "")
