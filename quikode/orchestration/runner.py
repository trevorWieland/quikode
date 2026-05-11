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
from quikode.orchestration import scheduler, stacking_helpers
from quikode.orchestration.candidates import CandidatesMixin
from quikode.orchestration.merge_watch import MergeWatchMixin
from quikode.orchestration.rebase_watch import RebaseWatchMixin
from quikode.orchestration.review_watch import ReviewWatchMixin
from quikode.orchestration.supervision import SupervisionMixin
from quikode.runtime_shutdown import request_stop
from quikode.state import State, Store, TaskRow
from quikode.workers.factory import build_task_worker
from quikode.workers.task_worker import TaskWorker

log = logging.getLogger("quikode.orchestrator")

__all__ = [
    "Orchestrator",
    "ReviewThread",
    "TaskRow",
    "TaskWorker",
    "_worktree_mtime",
    "build_task_worker",
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


class Orchestrator(SupervisionMixin, ReviewWatchMixin, MergeWatchMixin, RebaseWatchMixin, CandidatesMixin):
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
        request_stop()
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
        arrays carry every unmet stack-ready parent. For plan-32 multi-parent
        picks (`unmet=[merge_node_id]` after reduction), the chain is
        single-element-merge-node — single-parent code paths apply.

        For fresh roots, clear any stale parent linkage left over from a
        prior stacking that no longer applies. Plan 32: never clear linkage
        for `kind="merge"` rows (their parent_task_ids hold the source
        parent set, which propagation logic depends on).
        """
        nid = c["task_id"]
        row = c.get("row") or self.store.get(nid) or {}
        is_merge_node = (row.get("kind") or "spec") == "merge"
        if not c["is_stacked"]:
            # Clear any stale parent linkage from a prior stacking round —
            # but never for merge-nodes, whose parent_task_ids carry the
            # source parent set used by propagate_parent_* hooks.
            if not is_merge_node and self.store.get_parent_task_ids(nid):
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
        """Plan 59 fix C: delegates to `stacking_helpers.stack_depth`.

        The stacking-walk helpers live in `stacking_helpers.py` now so
        the TUI's pending-eligibility controller can use them too. This
        method stays as a thin shim so existing callers (mixins) keep
        working without churn.
        """
        return stacking_helpers.stack_depth(
            self.store, self.dag, task_id, max_depth_sentinel=self.cfg.stacking_max_depth
        )

    def _parents_of(self, task_id: str) -> list[str]:
        """Plan 59 fix C: delegates to `stacking_helpers.parents_of`."""
        return stacking_helpers.parents_of(self.store, task_id)

    def _would_form_cycle(self, child_id: str, prospective_parent_id: str) -> bool:
        """Plan 59 fix C: delegates to `stacking_helpers.would_form_cycle`."""
        return stacking_helpers.would_form_cycle(self.store, self.dag, child_id, prospective_parent_id)

    def _stack_root(self, task_id: str) -> str:
        """Plan 59 fix C: delegates to `stacking_helpers.stack_root`."""
        return stacking_helpers.stack_root(self.store, self.dag, task_id)

    def _stack_size_under_root(self, root_task_id: str) -> int:
        """Plan 59 fix C: delegates to `stacking_helpers.stack_size_under_root`."""
        return stacking_helpers.stack_size_under_root(self.store, self.dag, root_task_id)

    def _all_done(self, scope: set[str]) -> bool:
        """All tasks reached a TRULY terminal state (no orchestrator work left).

        Post-PR states are not terminal: the orchestrator's review watcher
        polls those PRs for new threads + human merge. We keep the
        loop alive on PENDING_CI, AWAITING_REVIEW, ADDRESSING_FEEDBACK,
        REBASING_TO_MAIN, CONFLICT_RESOLVING, and the subtask-loop states.

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
        # Plan 32: pick worker class by `kind`. Spec tasks have a DAG node;
        # merge-nodes don't (runtime-created), so pass the id and let the
        # factory synthesize a Node from the store row.
        if task_id in self.dag.nodes:
            node: Any = self.dag.nodes[task_id]
        else:
            node = task_id
        worker = build_task_worker(self.cfg, self.dag, self.store, node)
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
