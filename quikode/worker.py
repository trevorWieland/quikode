"""Per-task FSM driver. One worker drives one task from PROVISIONING → terminal.

v2 (Phase 0) flow:
  PROVISIONING → PLANNING (emits structured-JSON plan with subtasks)
              → for each subtask in topological order:
                    DOING_SUBTASK → CHECKING_SUBTASK ↔ TRIAGING_SUBTASK
              → FINAL_CHECKING (whole-spec gate: just ci + whole-spec checker agent)
                    on fail → TRIAGING (whole-spec) → DOING (legacy whole-spec doer for fixup)
              → COMMITTING → PUSHING → PR_OPENING → POLLING_CI → AWAITING_MERGE
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import (
    docker_env,
    github,
    github_graphql,
    manual_probe,
    pre_pr_audit,
    prompts,
    retry_classify,
    scheduler,
    sound,
    stacking,
    worktree,
)
from .agents import build_agent
from .agents.progress import (
    ProgressAttempt,
    ProgressVerdict,
    build_progress_agent,
)
from .config import Config
from .dag import DAG, Node
from .docker_env import TaskContainer, exec_in
from .github_graphql import ReviewThread
from .state import State, Store, SubtaskState
from .subtask_schema import (
    FixupPlan,
    Plan,
    PlanValidationError,
    Subtask,
    parse_fixup_planner_output,
    parse_planner_output,
)
from .types import IntentReviewOutcome, IntentVerdict, Verdict

log = logging.getLogger("quikode.worker")


@dataclass
class WorkerOutcome:
    final_state: State
    note: str = ""


@dataclass
class _CheckerOutcome:
    """Structured result from `_check_subtask`. Carries the verdict + the
    full subprocess shape (rc, stdout, stderr) so callers can drive both
    the FAIL-handler path AND the retry-cause classifier with real data
    instead of `rc=?` placeholders.
    """

    verdict: Verdict
    checker_text: str
    transient: bool
    rc: int | None
    stderr: str


@dataclass
class _SubtaskPassOutcome:
    """Result of running the pre-commit + commit + push gate after a
    Verdict.PASS from the checker.

    - kind="settled": gate passed and commit landed; subtask is DONE.
    - kind="transient_retry": push failed for network reasons; loop should
      retry without burning the real-failure budget.
    - kind="fail": gate or commit failed for real reasons; loop should
      treat this exactly like a checker FAIL (triage, retries++).
    """

    kind: Literal["settled", "transient_retry", "fail"]
    synthesized_checker_text: str = ""


class TaskWorker:
    def __init__(self, cfg: Config, dag: DAG, store: Store, node: Node):
        self.cfg = cfg
        self.dag = dag
        self.store = store
        self.node = node
        self.handle: TaskContainer | None = None
        self.log_path = cfg.log_dir / f"{node.id}.log"
        self.plan_text: str = ""  # raw planner stdout (kept for artifact + PR body)
        self.plan: Plan | None = None  # parsed structured plan
        self.last_doer_summary: str = ""
        self.last_triage_notes: str = ""

    @property
    def _h(self) -> TaskContainer:
        """Narrow `self.handle` for type-checker happiness; asserts at call.
        Once provision runs, self.handle is set and stays set for the worker's
        lifetime. Methods called after _provision can use self._h instead of
        repeating the assert."""
        assert self.handle is not None, "_provision() must run before this method"
        return self.handle

    def _row(self) -> dict[str, object]:
        """Narrow `Store.get(self.node.id)` away from `dict | None`. Used after
        _provision when the row is guaranteed present."""
        row = self.store.get(self.node.id)
        assert row is not None, f"task {self.node.id!r} should be in store but isn't"
        return row

    # ----- top-level lifecycle -----

    def run(self) -> WorkerOutcome:
        try:
            self._provision()
            self._plan()
            outcome = self._subtask_loop()
            if outcome:
                return outcome
            outcome = self._commit_push()
            if outcome:
                return outcome
            # 4-stage gate (local-CI + 3 audits) BEFORE opening the PR.
            # Catches issues early so reviewers see fewer nits and the fixup
            # cycle happens in-process instead of through review threads.
            outcome = self._run_pre_pr_pipeline()
            if outcome:
                return outcome
            outcome = self._open_pr()
            if outcome:
                return outcome
            return self._poll_pr_loop()
        except Exception as e:
            log.exception("task %s crashed", self.node.id)
            self.store.transition(self.node.id, State.FAILED, note=str(e), last_error=str(e)[:1000])
            return WorkerOutcome(State.FAILED, str(e))
        finally:
            self._teardown()

    def run_review_response(self, threads_to_address: list[ReviewThread]) -> WorkerOutcome:
        """Alternate worker entry mode for v3 Phase B review responses.

        Submitted to the worker pool by the daemon when its review-watcher
        pass detects unresolved review threads on an AWAITING_MERGE task.
        Skips planning + the spec subtask loop + new-worktree provisioning;
        spins up a fresh container against the existing worktree, lets the
        fixup planner decompose the threads into per-thread mini-subtasks,
        drives them through the per-subtask doer/checker/commit gate (each
        thread's fix lands as its own commit + push on the PR branch),
        resolves the threads, and returns the task to AWAITING_MERGE.

        Lifecycle (humans drive cadence — no per-task retry budget):
          1. PROVISIONING → reuse worktree, fresh container
          2. ADDRESSING_FEEDBACK → fixup planner (kind=fixup-review)
          3. FIXUP_PLANNING → per-thread subtask plan emitted
          4. DOING_SUBTASK / CHECKING_SUBTASK loop, one slice per thread
             (each slice commits + pushes via per-subtask gate)
          5. resolve threads via GraphQL
          6. transition back to AWAITING_MERGE
        """
        if not threads_to_address:
            log.warning("run_review_response called with empty thread list; nothing to do")
            return WorkerOutcome(State.PENDING_CI, "no threads to address")

        try:
            # 1. provision container against existing worktree
            self._provision(provision_worktree=False)
            self.store.transition(
                self.node.id,
                State.ADDRESSING_FEEDBACK,
                note=f"addressing {len(threads_to_address)} review thread(s)",
            )

            # Re-hydrate plan_text from the row so the fixup-planner prompt
            # has spec context. Resume plan if available — otherwise fall
            # back to "" which the prompts handle.
            row = self._row()
            self.plan_text = str(row.get("plan_text") or "")

            # Render the threads as a block the fixup planner can consume.
            # Each line = author + path:line + truncated body, so the planner
            # can scope each emitted subtask to one thread's file/line.
            thread_lines = []
            for i, t in enumerate(threads_to_address, 1):
                path_line = f"{t.path or '(no path)'}:{t.line or '?'}"
                body = (t.last_comment_body or "").strip().replace("\n", " ")
                if len(body) > 400:
                    body = body[:400] + "…"
                thread_lines.append(
                    f"{i}. [{path_line}] (by {t.last_comment_author}, "
                    f"bot={'yes' if t.last_comment_is_bot else 'no'}): {body}"
                )
            review_threads_block = "\n".join(thread_lines)

            # 2-4. v3 fixup decomposition: plan + run per-thread mini-subtasks.
            # Each lands as its own commit on the PR branch via the per-subtask
            # commit gate, replacing the legacy whole-spec _do(attempt=300)
            # monolith that historically ran 30-60 min on a small set of
            # threads with shaky convergence.
            round_no = int(row.get("review_round") or 0) + 1
            outcome = self._run_fixup_round(
                kind="fixup-review",
                round_no=round_no,
                trigger="review",
                review_threads_block=review_threads_block,
            )
            if outcome and outcome.final_state == State.BLOCKED:
                # Don't surface BLOCKED to the orchestrator — review response
                # is human-driven; let the operator see the partial progress
                # and re-trigger via a fresh thread or a manual retry.
                log.warning(
                    "review response fixup round blocked: %s — returning to AWAITING_MERGE",
                    outcome.note,
                )
                self.store.transition(
                    self.node.id,
                    State.PENDING_CI,
                    note=f"review response fixup blocked: {outcome.note[:200]}",
                )
                return WorkerOutcome(
                    State.PENDING_CI,
                    f"review response fixup blocked: {outcome.note[:200]}",
                )

            # 5. resolve threads (best-effort). Use the latest commit sha on
            # the branch as the addressed-in marker — the per-subtask commit
            # gate has already pushed each thread's slice.
            commit_sha = self._latest_commit_sha_on_branch()
            for t in threads_to_address:
                try:
                    ok = github_graphql.resolve_thread(t.thread_id)
                except Exception as e:
                    log.warning("resolve_thread %s raised: %s", t.thread_id, e)
                    ok = False
                if not ok:
                    log.warning("resolve_thread %s returned False; continuing", t.thread_id)
                if commit_sha:
                    self.store.upsert_review_thread(
                        self.node.id,
                        thread_id=t.thread_id,
                        is_resolved=t.is_resolved,
                        last_comment_ts=t.last_comment_created_at,
                        last_comment_author=t.last_comment_author,
                        last_comment_is_bot=t.last_comment_is_bot,
                    )
                    self.store.mark_thread_addressed(self.node.id, t.thread_id, commit_sha)

            # 8. counters + transition
            self.store.increment_review_round(self.node.id)
            self.store.transition(
                self.node.id,
                State.PENDING_CI,
                note=f"responded to {len(threads_to_address)} thread(s)",
            )
            return WorkerOutcome(
                State.PENDING_CI,
                f"responded to {len(threads_to_address)} threads",
            )
        except Exception as e:
            log.exception("review response for task %s crashed", self.node.id)
            # Don't FAIL the task — return it to AWAITING_MERGE so humans
            # can intervene without losing the existing PR.
            self.store.transition(
                self.node.id,
                State.PENDING_CI,
                note=f"review response crashed: {e}",
                last_error=str(e)[:1000],
            )
            return WorkerOutcome(State.PENDING_CI, f"review response crashed: {e}")
        finally:
            # Tear down container only — keep the worktree so subsequent
            # response cycles (or merge) can reuse it.
            if self.handle is not None:
                docker_env.teardown(self._h)
                self.handle = None

    def run_ci_fix_response(self, pr_status: github.PRStatus) -> WorkerOutcome:
        """Worker entry mode for daemon-detected post-merge CI failures.

        When GitHub's CI flips to FAILURE while the task is in
        AWAITING_MERGE (typically because a review-response push landed
        and re-triggered CI which then failed), the daemon dispatches
        this worker. We re-use the fixup-decomposition path with
        kind='fixup-ci' and the failure log as the trigger context.

        Critical for unattended operation: without this path, a CI
        failure post-AWAITING-MERGE leaves the task stuck indefinitely
        until an operator notices.
        """
        try:
            self._provision(provision_worktree=False)
            self.store.transition(
                self.node.id,
                State.ADDRESSING_FEEDBACK,
                note=f"addressing CI failure ({len(pr_status.failed_checks)} failed check(s))",
            )
            row = self._row()
            self.plan_text = str(row.get("plan_text") or "")

            # Fetch the failed-check log excerpts for the fixup planner.
            try:
                ci_log = github.fetch_failed_check_logs(self.cfg.repo_path, int(pr_status.number))
            except Exception as e:
                log.warning("fetch_failed_check_logs raised: %s — using minimal context", e)
                ci_log = "\n".join(f"failed: {c.get('name', '<unknown>')}" for c in pr_status.failed_checks)
            ci_excerpt = _last_lines(ci_log, 80)

            # ci_triage_retries is the cumulative count for this task,
            # used as the round_no so successive CI failures get distinct
            # subtask ID prefixes (F-1-1-..., F-2-1-..., etc).
            round_no = int(row.get("ci_triage_retries") or 0) + 1
            self.store.increment(self.node.id, "ci_triage_retries")
            outcome = self._run_fixup_round(
                kind="fixup-ci",
                round_no=round_no,
                trigger="ci",
                ci_excerpt=ci_excerpt,
            )
            if outcome and outcome.final_state == State.BLOCKED:
                log.warning(
                    "ci-fix fixup round blocked: %s — returning to AWAITING_MERGE",
                    outcome.note,
                )
                self.store.transition(
                    self.node.id,
                    State.PENDING_CI,
                    note=f"ci-fix fixup blocked: {outcome.note[:200]}",
                )
                return WorkerOutcome(
                    State.PENDING_CI,
                    f"ci-fix fixup blocked: {outcome.note[:200]}",
                )

            # All fixup subtasks settled (per-subtask commits already
            # pushed). Return to AWAITING_MERGE; GitHub will re-run CI
            # against the new commits and the daemon's next poll picks
            # up either CI-pass or another failure.
            self.store.transition(
                self.node.id,
                State.PENDING_CI,
                note=f"ci-fix round {round_no} pushed {len(pr_status.failed_checks)} fix slice(s)",
            )
            return WorkerOutcome(
                State.PENDING_CI,
                f"ci-fix round {round_no} complete",
            )
        except Exception as e:
            log.exception("ci-fix for task %s crashed", self.node.id)
            self.store.transition(
                self.node.id,
                State.PENDING_CI,
                note=f"ci-fix crashed: {e}",
                last_error=str(e)[:1000],
            )
            return WorkerOutcome(State.PENDING_CI, f"ci-fix crashed: {e}")
        finally:
            if self.handle is not None:
                docker_env.teardown(self._h)
                self.handle = None

    def run_rebase_to_main(self) -> WorkerOutcome:
        """v3 Phase C alternate worker entry mode: parent merged → rebase
        this child's branch onto main, retarget its PR, and restore the
        prior active state.

        Lifecycle:
          1. provision a fresh container against the existing worktree
          2. fetch origin main
          3. rebase the worktree branch onto origin/main
          4. on conflict → reuse `_spawn_conflict_resolver` (which is
             scoped to a generic "resolve current rebase conflict" task,
             so it works the same whether the conflict came from a
             scheduled rebase or a parent-merge rebase)
          5. on success: force-push, retarget the PR base to main, clear
             `parent_pr_branch` + `parent_branch`, transition back to the
             stashed `pre_rebase_state`
          6. on any failure: leave the row in REBASING_TO_MAIN with
             last_error set so an operator can intervene

        Returns the WorkerOutcome carrying the resumed state. The
        orchestrator's reaper just logs it; the persistent state in the
        store is what drives subsequent picks/polls.
        """
        row = self.store.get(self.node.id) or {}
        pre_state_str = self.store.get_pre_rebase_state(self.node.id) or row.get("state") or ""
        # The pre-rebase state may be REBASING_TO_MAIN itself if the row was
        # already in that state when we arrived (defensive — shouldn't
        # normally happen). Fall back to AWAITING_MERGE in that case as a
        # safe terminal-ish landing.
        try:
            pre_state = State(pre_state_str)
        except ValueError:
            pre_state = State.PENDING_CI
        if pre_state is State.REBASING_TO_MAIN:
            pre_state = State.PENDING_CI

        try:
            # 1. provision container against existing worktree
            self._provision(provision_worktree=False)

            # 2. capture the rebase --onto target BEFORE fetch.
            #
            # The recompute path is:
            #   a. Use the *prior* merge-base sha as `--onto`'s "upstream"
            #      reference (the boundary above which child commits live).
            #      Capture it before any fetch since the local ref can be
            #      reset by `construct_merge_base` below.
            #   b. Recompute a fresh merge-base off the parents' new tips.
            #      If parents' tips haven't changed, the new sha == prior
            #      sha and the rebase is a no-op (useful idempotence).
            #   c. Rebase --onto <new_merge_base> <prior_merge_base> branch.
            # If recompute fails (conflict between parents), the worker
            # BLOCKs — there's no clean rebase target without a merge-base.
            #
            # For a single-parent child, the parent's branch IS the merge
            # base; we read it from `parent_branches[0]` and rev-parse to
            # get the upstream sha for the rebase.
            prior_merge_base_sha = str(row.get("parent_merge_base_sha") or "")
            parent_branches = self.store.get_parent_branches(self.node.id)
            onto_sha = ""
            new_merge_base_sha = ""

            if prior_merge_base_sha:
                # Verify the prior merge-base sha is still in the worktree's
                # git database. If it isn't (e.g. branch was deleted from
                # the repo), fall through and recompute from parent_branches.
                rc_ps, _ = self._git_in_workspace(["rev-parse", "--verify", prior_merge_base_sha])
                if rc_ps == 0:
                    onto_sha = prior_merge_base_sha

            if len(parent_branches) >= 1:
                # Make sure the parents' refs are up to date.
                for pb in parent_branches:
                    self._git_in_workspace(["fetch", self.cfg.pr_remote, pb])
                if len(parent_branches) > 1:
                    # Multi-parent: recompute the synthetic merge-base.
                    mb_name = stacking.compute_merge_base_branch_name(self.node.id, parent_branches)
                    mb_sha = stacking.construct_merge_base(
                        repo_path=self.cfg.repo_path,
                        parent_branches=parent_branches,
                        branch_name=mb_name,
                        base_branch=self.cfg.base_branch,
                    )
                    if mb_sha:
                        new_merge_base_sha = mb_sha
                        self.store.set_parent_merge_base(self.node.id, branch=mb_name, sha=mb_sha)
                    else:
                        note = (
                            f"multi-parent merge-base recompute failed for {parent_branches}; cannot rebase"
                        )
                        self.store.transition(
                            self.node.id,
                            State.BLOCKED,
                            note=note,
                            last_error=note[:1000],
                        )
                        return WorkerOutcome(State.BLOCKED, note)
                elif not onto_sha:
                    # Single-parent: parent_branches[0] IS the upstream.
                    rc_ps, ps_out = self._git_in_workspace(["rev-parse", "--verify", parent_branches[0]])
                    if rc_ps == 0:
                        onto_sha = ps_out.strip().splitlines()[-1] if ps_out.strip() else ""

            # 3. fetch origin main
            rc, fetch_out = self._git_in_workspace(["fetch", self.cfg.pr_remote, self.cfg.base_branch])
            if rc != 0:
                self.store.transition(
                    self.node.id,
                    pre_state,
                    note=f"rebase-to-main: fetch failed ({fetch_out[:200]}); restoring {pre_state.value}",
                    last_error=f"rebase fetch: {fetch_out[:500]}",
                )
                return WorkerOutcome(pre_state, "rebase fetch failed")

            # 4. rebase. Three paths:
            #
            #   (a) Multi-parent recompute succeeded → rebase --onto the
            #       new merge-base sha, treating the prior merge-base as
            #       the upstream boundary. Drops the old merge content,
            #       replays only the child's commits onto the new base.
            #   (b) Single-parent (or fallback) with a known --onto sha →
            #       drop parent's commits (already squashed into main),
            #       replay only child's exclusive work.
            #   (c) No --onto known → plain rebase against origin/main.
            rebase_target = (
                new_merge_base_sha if new_merge_base_sha else f"{self.cfg.pr_remote}/{self.cfg.base_branch}"
            )
            if onto_sha:
                rc, _rebase_out = self._git_in_workspace(
                    [
                        "-c",
                        "core.editor=true",
                        "rebase",
                        "--onto",
                        rebase_target,
                        onto_sha,
                    ]
                )
            else:
                rc, _rebase_out = self._git_in_workspace(["-c", "core.editor=true", "rebase", rebase_target])
            if rc != 0:
                # 4. conflict — try the resolver. _spawn_conflict_resolver
                # already handles staging, --continue, verify, and push;
                # on success it returns None and the caller continues.
                resolver_outcome = self._spawn_conflict_resolver()
                if resolver_outcome is not None:
                    # Resolver gave up or post-resolve verification failed.
                    # The store has already been transitioned to BLOCKED by
                    # _spawn_conflict_resolver; no further state work needed
                    # here, but we surface the rebase context.
                    log.warning(
                        "rebase-to-main: conflict resolver gave up for task %s",
                        self.node.id,
                    )
                    return resolver_outcome
                # Resolver succeeded — it has already pushed.
                push_already_done = True
            else:
                push_already_done = False

            # 5. force-push (skip when resolver already pushed)
            branch = str(row.get("branch") or self._row().get("branch") or "")
            # Defensive: if the rebase ate everything (e.g. conflict-resolver
            # accepted main's version of every conflict), the branch is now
            # 0 commits ahead of base — pushing it would land an empty PR
            # that github will auto-close. BLOCK with a clear note instead.
            if branch:
                ahead = self._git_ahead_count(branch)
                if ahead == 0:
                    worktree_path = row.get("worktree_path") or self._row().get("worktree_path") or ""
                    note = (
                        f"post-rebase branch is 0 commits ahead of {self.cfg.base_branch} — "
                        f"the rebase likely dropped task changes. Inspect the worktree at "
                        f"{worktree_path} before retrying."
                    )
                    self.store.transition(
                        self.node.id,
                        State.BLOCKED,
                        note=note,
                        last_error=note[:1000],
                    )
                    return WorkerOutcome(State.BLOCKED, "post-rebase empty branch")
            if not push_already_done and branch:
                rc, push_out = self._git_in_workspace(
                    ["push", "--force-with-lease", "-u", self.cfg.pr_remote, branch]
                )
                if rc != 0:
                    self.store.transition(
                        self.node.id,
                        State.BLOCKED,
                        note=f"rebase-to-main: force-push failed: {push_out[:200]}",
                        last_error=f"rebase push: {push_out[:500]}",
                    )
                    return WorkerOutcome(State.BLOCKED, "rebase push failed")

            # Capture new main sha for downstream intent-review bookkeeping.
            _, new_main_sha = self._git_in_workspace(
                ["rev-parse", f"{self.cfg.pr_remote}/{self.cfg.base_branch}"]
            )

            # 6. retarget PR base to main. Github auto-closes a PR when its
            # base branch is deleted (which happens when the parent merges
            # with --delete-branch). The PR can NOT be reopened — github
            # rejects reopen + base-change on a closed-with-deleted-base PR.
            # So if the PR went CLOSED while we were rebasing, we create a
            # fresh PR pointing at main; the rebased branch + commits are
            # already in place from step 5.
            #
            # SAFETY: a transient `gh pr edit` failure is NOT a CLOSED PR.
            # We must distinguish: if retarget fails, query the PR's actual
            # state. Only create a new PR if the existing one is CLOSED.
            # On OPEN+retarget-fail, retry once with a brief backoff. On
            # truly unreachable PR (gh fails to even read state), mark
            # BLOCKED rather than risk a duplicate PR.
            pr_number = row.get("pr_number") or self._row().get("pr_number")
            if pr_number:
                self._safe_retarget_or_recreate(int(pr_number))

            # 7. clear stacking metadata + restore pre-rebase state
            self.store.clear_parent_branch(self.node.id)
            self.store.set_field(
                self.node.id,
                last_synced_main_sha=new_main_sha.strip() or None,
                pre_rebase_state=None,
            )
            self.store.transition(
                self.node.id,
                pre_state,
                note=f"rebased onto main; restored {pre_state.value}",
            )
            return WorkerOutcome(pre_state, "rebased onto main")
        except Exception as e:
            log.exception("rebase-to-main for task %s crashed", self.node.id)
            self.store.transition(
                self.node.id,
                pre_state,
                note=f"rebase-to-main crashed: {e}; restoring {pre_state.value}",
                last_error=str(e)[:1000],
            )
            return WorkerOutcome(pre_state, f"rebase-to-main crashed: {e}")
        finally:
            # Tear down the container only — keep the worktree.
            if self.handle is not None:
                docker_env.teardown(self._h)
                self.handle = None

    def _retarget_pr_to_main(self, pr_number: int) -> bool:
        """Retarget an open PR's base to `cfg.base_branch`. Returns True on
        success, False on any failure (including the github auto-close
        case when the original base branch was deleted)."""
        try:
            r = subprocess.run(
                ["gh", "pr", "edit", str(pr_number), "--base", self.cfg.base_branch],
                cwd=self.cfg.repo_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            if r.returncode != 0:
                log.warning(
                    "gh pr edit --base %s for PR #%d failed (rc=%d): %s",
                    self.cfg.base_branch,
                    pr_number,
                    r.returncode,
                    (r.stderr or r.stdout)[:300],
                )
                return False
            log.info("retargeted PR #%d base → %s", pr_number, self.cfg.base_branch)
            return True
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("gh pr edit raised: %s", e)
            return False

    def _create_new_pr_for_rebased_branch(self) -> tuple[str | None, int | None]:
        """Create a fresh PR (base=main) for the current rebased branch.

        Used when github auto-closed the original PR because its base was
        the parent's deleted branch. The branch + commits are intact; we
        just need a new PR pointing at main. Reuses the existing
        `github.open_pr` helper so the PR title/body match the worker's
        normal _open_pr output.
        """
        title = f"{self.node.id}: {self.node.title}"
        body = self._pr_body()
        rc, url, out = github.open_pr(self._h, title, body, base=self.cfg.base_branch, log_path=self.log_path)
        if rc != 0 or not url:
            log.warning(
                "task %s: failed to create replacement PR after rebase (rc=%d): %s",
                self.node.id,
                rc,
                out[:300],
            )
            return None, None
        m = re.search(r"/pull/(\d+)", url)
        new_pr_number = int(m.group(1)) if m else None
        return url, new_pr_number

    def _pr_state(self, pr_number: int) -> str | None:
        """Query the actual lifecycle state of a PR via gh.

        Returns one of "OPEN", "MERGED", "CLOSED", or None when the
        query failed entirely (network/auth error). The caller decides
        what to do with None — see `_safe_retarget_or_recreate`.
        """
        try:
            r = subprocess.run(
                ["gh", "pr", "view", str(pr_number), "--json", "state"],
                cwd=self.cfg.repo_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("gh pr view --json state for #%d raised: %s", pr_number, e)
            return None
        if r.returncode != 0:
            log.warning(
                "gh pr view --json state for #%d failed (rc=%d): %s",
                pr_number,
                r.returncode,
                (r.stderr or r.stdout)[:200],
            )
            return None
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            return None
        s = data.get("state")
        return str(s) if isinstance(s, str) else None

    def _safe_retarget_or_recreate(self, pr_number: int) -> None:
        """Retarget the PR; on retarget failure, decide carefully.

        Decision tree:
          1. Try retarget. Success → done.
          2. Retarget failed: query the PR's actual state.
              - state OPEN → retry retarget once after a brief sleep.
                Still failing → mark BLOCKED (network/perm issue we
                can't safely fall back from).
              - state CLOSED → create a fresh PR on main and update
                pr_number/pr_url.
              - state MERGED → unexpected (we wouldn't be rebasing if
                merged). Log + leave as-is.
              - state unknown → mark BLOCKED rather than risk a
                duplicate PR.
        """
        if self._retarget_pr_to_main(pr_number):
            return
        state = self._pr_state(pr_number)
        if state == "OPEN":
            # Transient hiccup; one retry with a tiny backoff.
            time.sleep(2)
            if self._retarget_pr_to_main(pr_number):
                return
            log.warning(
                "task %s: PR #%d still OPEN but retarget failed twice; marking BLOCKED",
                self.node.id,
                pr_number,
            )
            self.store.transition(
                self.node.id,
                State.BLOCKED,
                note=f"rebase-to-main: retarget of OPEN PR #{pr_number} failed twice",
                last_error=f"retarget #{pr_number} failed; PR is OPEN",
            )
            return
        if state == "CLOSED":
            new_url, new_pr_number = self._create_new_pr_for_rebased_branch()
            if new_url and new_pr_number:
                self.store.set_field(
                    self.node.id,
                    pr_url=new_url,
                    pr_number=new_pr_number,
                )
                log.info(
                    "task %s: stale PR #%d closed; created fresh PR #%d on main",
                    self.node.id,
                    pr_number,
                    new_pr_number,
                )
            return
        if state == "MERGED":
            log.warning(
                "task %s: PR #%d already MERGED but rebase ran — leaving state alone",
                self.node.id,
                pr_number,
            )
            return
        # state is None — couldn't even read it. Refuse to create a new
        # PR (might be transient), mark BLOCKED so a human eyeballs it.
        log.warning(
            "task %s: PR #%d state unreachable; refusing to recreate to avoid duplicate",
            self.node.id,
            pr_number,
        )
        self.store.transition(
            self.node.id,
            State.BLOCKED,
            note=f"rebase-to-main: PR #{pr_number} state unreachable",
            last_error=f"could not read state for PR #{pr_number}; refusing to recreate",
        )

    def _latest_commit_sha_on_branch(self) -> str:
        """Return the current HEAD sha in /workspace, or "" on error.

        Used by `run_review_response` to mark threads addressed against the
        most-recent commit landed on the PR branch — which after fixup
        decomposition is the last per-subtask commit pushed by
        `_handle_subtask_pass`.
        """
        try:
            rc, out, _err = exec_in(
                self._h,
                ["bash", "-lc", "cd /workspace && git rev-parse HEAD"],
                log_path=self.log_path,
                timeout=30,
            )
        except Exception as e:
            log.warning("latest_commit_sha lookup failed: %s", e)
            return ""
        if rc != 0:
            return ""
        return (out or "").strip()

    def _commit_and_push_response(self) -> worktree.CommitResult:
        """Commit + push the in-flight worktree edits for a review response."""
        row = self._row()
        branch = str(row.get("branch") or "")
        msg = f"{self.node.id}: address review feedback"
        return worktree.commit_response(
            self._h,
            msg,
            branch=branch,
            remote=self.cfg.pr_remote,
            push=True,
            log_path=self.log_path,
        )

    # ----- phase: provision -----

    def _provision(self, *, provision_worktree: bool = True) -> None:
        """Stand up the worktree (optional) and dev container for a task.

        With `provision_worktree=True` (default), creates a fresh worktree
        and branch as the run() entry-point flow expects. With `=False`,
        skips worktree creation and reuses whatever the task row already has
        — used by `run_review_response()` so the response cycle inherits the
        existing branch + PR.
        """
        self.store.transition(self.node.id, State.PROVISIONING, note="creating worktree + container")
        if provision_worktree:
            self._provision_worktree()
        wt_path = self._existing_worktree_path()
        self._provision_container(wt_path)

    def _provision_worktree(self) -> None:
        """Create the per-task worktree + branch and persist them on the row.

        Resume case: if the row already has `worktree_path` + `branch` AND the
        path exists on disk AND git knows about it as a worktree, reuse them
        verbatim. Generating a fresh branch suffix here would orphan any work
        the human did inside the existing worktree (the unblock flow!).
        """
        existing_row = self.store.get(self.node.id) or {}
        existing_path = existing_row.get("worktree_path")
        existing_branch = existing_row.get("branch")
        if existing_path and existing_branch and Path(existing_path).exists():
            # Verify git still knows about it — otherwise treat as fresh.
            listing = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=self.cfg.repo_path,
                capture_output=True,
                text=True,
                check=False,
            )
            registered = any(
                line.strip() == f"worktree {existing_path}" for line in listing.stdout.splitlines()
            )
            if registered:
                # Resume: keep branch + worktree as-is. Don't reset row fields.
                return
        branch = worktree.branch_for(self.node.id)
        # Worktree dir uses the same suffix as the branch so multiple attempts
        # at the same task don't collide on disk.
        suffix = branch.rsplit("-", 1)[-1] if "-" in branch.rsplit("/", 1)[-1] else ""
        wt_dir = docker_env.slugify(self.node.id) + (f"-{suffix}" if suffix else "")
        wt_path = (self.cfg.worktree_root / wt_dir).resolve()

        # Multi-parent stacking. The orchestrator stamps the full parent
        # chain into `parent_task_ids` / `parent_branches` JSON arrays.
        # When > 1 parent, build a synthetic merge-base branch
        # (`quikode/<id>-base-<6hex>`) off `git merge` of the parent tips
        # and fork the worktree from there. When == 1 parent, branch off
        # that parent's branch directly. When 0, branch off main.
        parent_branches = self.store.get_parent_branches(self.node.id)
        parent_branch: str | None = None
        if len(parent_branches) > 1:
            # Build the merge-base branch off origin/main + every parent's tip.
            worktree.fetch_base(self.cfg.repo_path, self.cfg.pr_remote, self.cfg.base_branch)
            mb_name = stacking.compute_merge_base_branch_name(self.node.id, parent_branches)
            mb_sha = stacking.construct_merge_base(
                repo_path=self.cfg.repo_path,
                parent_branches=parent_branches,
                branch_name=mb_name,
                base_branch=self.cfg.base_branch,
            )
            if not mb_sha:
                note = (
                    f"multi-parent merge-base construction failed for "
                    f"{parent_branches}; cannot provision worktree"
                )
                self.store.transition(
                    self.node.id,
                    State.BLOCKED,
                    note=note,
                    last_error=note[:1000],
                )
                raise RuntimeError(note)
            self.store.set_parent_merge_base(self.node.id, branch=mb_name, sha=mb_sha)
            parent_branch = mb_name
        elif len(parent_branches) == 1:
            parent_branch = parent_branches[0]
        worktree.fetch_base(self.cfg.repo_path, self.cfg.pr_remote, self.cfg.base_branch)
        # Capture the main SHA at branch creation. Used by Phase A's
        # conflict resolver to compute "what landed since" and by Phase B's
        # intent reviewer to detect drift.
        base_sha_proc = subprocess.run(
            ["git", "rev-parse", f"{self.cfg.pr_remote}/{self.cfg.base_branch}"],
            cwd=self.cfg.repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        base_ref_sha = base_sha_proc.stdout.strip() if base_sha_proc.returncode == 0 else None
        # If stacking, branch off parent_branch; else main.
        if parent_branch:
            worktree.add_worktree_off_branch(
                self.cfg.repo_path,
                wt_path,
                branch,
                parent_branch,
                remote=self.cfg.pr_remote,
            )
        else:
            worktree.add_worktree(
                self.cfg.repo_path, wt_path, branch, self.cfg.base_branch, self.cfg.pr_remote
            )

        self.store.set_field(
            self.node.id,
            branch=branch,
            worktree_path=str(wt_path),
            base_ref_sha=base_ref_sha,
            last_synced_main_sha=base_ref_sha,
        )

    def _existing_worktree_path(self) -> Path:
        """Resolve the worktree path stored on the task row. Required for
        re-provisioning a fresh container against an existing worktree
        (review-response cycles, rebase-to-main).

        Resilience: if `worktree_path` is missing but `branch` is set,
        reconstruct the canonical wt path from `cfg.worktree_root` + the
        slug derived from `branch`. This protects against a known race
        where a task entered `_run_rebase_to_main_one` with worktree_path
        cleared by an earlier resume / orphan-recovery path. If the
        reconstructed path exists, persist it so subsequent calls are
        cheap; otherwise raise the original error.
        """
        row = self._row()
        wt = row.get("worktree_path")
        if wt:
            return Path(str(wt))
        # Fallback: reconstruct from branch.
        branch = str(row.get("branch") or "")
        if branch:
            # Worktree dir mirrors the branch's hex suffix per
            # `_provision_worktree`. Branch format: "<prefix>/<slug>-<hex>"
            tail = branch.rsplit("/", 1)[-1]
            suffix = tail.rsplit("-", 1)[-1] if "-" in tail else ""
            wt_dir = docker_env.slugify(self.node.id) + (f"-{suffix}" if suffix else "")
            candidate = (self.cfg.worktree_root / wt_dir).resolve()
            if candidate.exists():
                log.warning(
                    "task %s had no worktree_path; reconstructed %s from branch %s",
                    self.node.id,
                    candidate,
                    branch,
                )
                self.store.set_field(self.node.id, worktree_path=str(candidate))
                return candidate
            log.warning(
                "task %s: reconstructed candidate worktree %s does not exist on disk",
                self.node.id,
                candidate,
            )
        raise RuntimeError(
            f"task {self.node.id} has no worktree_path; cannot provision container without worktree"
        )

    def _provision_container(self, wt_path: Path) -> None:
        """Spin a fresh dev container against `wt_path`. Used both by the
        full provision path and by review-response re-provisioning."""
        handle = docker_env.make_handle(self.node.id)
        ws_label = docker_env.workspace_label(self.cfg)
        docker_env.network_create(handle.network_name, label=ws_label)
        docker_env.start_postgres(handle, label=ws_label)
        docker_env.wait_postgres_healthy(handle)
        cid = docker_env.start_dev_container(handle, self.cfg, wt_path)
        # Wait for the container's entrypoint to finish copying agent auth files
        # before any agent CLI is invoked. Without this, claude/codex see a
        # half-copied .claude.json and fail with cryptic errors.
        docker_env.wait_dev_ready(handle, timeout_s=60)

        # Postgres is up; schema migrations are the doer's responsibility (tanren
        # ships them via tanren-cli migrate up; whether `just ci` needs them
        # depends on the task — leaving this to the doer keeps provisioning fast).

        self.handle = handle
        self.store.set_field(self.node.id, container_id=cid)

    # ----- phase: plan -----

    def _plan(self) -> None:
        # Resume path: when `quikode resume <id>` set the flag, skip the
        # planner agent and reconstruct the Plan from the existing subtasks
        # (and stored plan_text). The subtask loop will skip rows already
        # in DONE state, so work picks up where it left off.
        row = self._row()
        if row.get("resume_from_existing_subtasks") and row.get("plan_text"):
            self.store.transition(self.node.id, State.PLANNING, note="resume — skipping planner")
            self.plan_text = row["plan_text"] or ""
            try:
                self.plan = parse_planner_output(self.plan_text, expected_node_id=self.node.id)
            except PlanValidationError as e:
                # plan_text was malformed for some reason — fall through to
                # re-plan with the agent rather than crash.
                log.warning(
                    "resume: stored plan_text failed re-parse (%s); falling back to fresh planning", e
                )
            else:
                # Clear the flag so subsequent runs follow the normal path.
                self.store.set_field(self.node.id, resume_from_existing_subtasks=0)
                return

        self.store.transition(self.node.id, State.PLANNING)
        agent = build_agent(self.cfg.planner)
        prompt = prompts.planner_prompt(self.cfg, self.dag, self.node)
        self._write_log_header("PLANNER", prompt)
        result = agent.run(prompt, handle=self._h, log_path=self.log_path, timeout=1800)
        self.store.record_agent_call(
            self.node.id,
            phase="planner",
            cli=self.cfg.planner.cli,
            model=self.cfg.planner.model,
            rc=result.rc,
            duration_s=result.duration_s or 0,
            tokens_used=result.tokens_used,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            tokens_cached_read=result.tokens_cached_read,
            tokens_cached_creation=result.tokens_cached_creation,
            cost_usd=result.cost_usd,
        )
        if not result.ok:
            raise RuntimeError(f"planner agent exited {result.rc}: {result.stderr[:500]}")
        self.plan_text = result.stdout
        self.store.add_artifact(self.node.id, "planner_output", result.stdout)
        self.store.set_field(self.node.id, plan_text=self.plan_text)

        # v2: parse the structured plan. On parse failure, re-prompt the planner
        # once with the validation error before giving up.
        plan = self._parse_or_retry_plan(result.stdout)
        self.plan = plan
        # Persist subtasks so `quikode show / subtasks / export` can surface them.
        self.store.upsert_subtasks(
            self.node.id,
            [
                {
                    "subtask_id": s.id,
                    "title": s.title,
                    "depends_on": list(s.depends_on),
                    "files_to_touch": list(s.files_to_touch),
                    "boundary": s.boundary,
                    "acceptance": list(s.acceptance),
                    "notes": s.notes,
                }
                for s in plan.subtasks
            ],
        )

    def _parse_or_retry_plan(self, stdout: str) -> Plan:
        try:
            return parse_planner_output(stdout, expected_node_id=self.node.id)
        except PlanValidationError as e:
            # One retry with the validation error fed back as user input
            log.warning("planner output failed validation (%s); re-prompting once", e)
            agent = build_agent(self.cfg.planner)
            prompt = prompts.planner_prompt(self.cfg, self.dag, self.node)
            prompt += (
                "\n\n## RETRY\n\n"
                "Your prior output failed validation:\n\n"
                f"```\n{e}\n```\n\n"
                "Re-emit a single fenced ```json ... ``` block that conforms strictly to the schema. "
                "No prose outside the fence other than a one-line preamble."
            )
            self._write_log_header("PLANNER (retry after validation error)", prompt)
            result = agent.run(prompt, handle=self._h, log_path=self.log_path, timeout=1800)
            self.store.record_agent_call(
                self.node.id,
                phase="planner_retry",
                cli=self.cfg.planner.cli,
                model=self.cfg.planner.model,
                rc=result.rc,
                duration_s=result.duration_s or 0,
                tokens_used=result.tokens_used,
                tokens_input=result.tokens_input,
                tokens_output=result.tokens_output,
                tokens_cached_read=result.tokens_cached_read,
                tokens_cached_creation=result.tokens_cached_creation,
                cost_usd=result.cost_usd,
            )
            self.store.add_artifact(self.node.id, "planner_output", result.stdout)
            return parse_planner_output(result.stdout, expected_node_id=self.node.id)

    # ----- phase: subtask loop (v2 Phase 0) -----

    def _subtask_loop(self) -> WorkerOutcome | None:
        """Iterate subtasks in topological order. Each subtask retries
        (effectively) unbounded; the v3 progress-check agent decides when
        to give up.

        Three gating fields collaborate:
        - ``subtask_hard_max_attempts`` (default 30): absolute ceiling.
        - ``subtask_progress_check_after`` / ``_every``: cadence at which
          the progress-check agent fires.
        - ``subtask_flatline_block_count``: consecutive FLATLINED verdicts
          before BLOCK.

        Transient retries (push-network blip, container OOM mid-doer)
        DO NOT bump the attempt counter — only real-failure attempts
        count. This lets infrastructure flakes free-retry without eating
        into the convergence budget.

        Critical contract: a subtask exhausting attempts (hard ceiling or
        flatline block) is a **task-level failure**. We return BLOCKED
        immediately and skip final_check — every later subtask depends,
        transitively or explicitly, on its predecessors having landed
        cleanly. Continuing past a BLOCKED subtask just builds on broken
        foundation, burns token budget, and produces a misleadingly-
        further-along task that can't actually pass.

        The user's recovery path is `quikode resume <id>`: fix whatever
        was wrong (prompt change, model swap, manual code edit), then
        resume from the failed subtask with the rest of the plan intact.
        """
        assert self.plan is not None, "_plan() must run before _subtask_loop()"
        return self._run_subtask_set(self.plan.topo_order())

    def _run_subtask_set(self, subtasks: list[Subtask]) -> WorkerOutcome | None:
        """Drive a sequence of subtasks through the doer/checker/triage loop.

        Used by both the original spec loop (`_subtask_loop`) and the v3
        fixup-decomposition flow (`_run_fixup_round`). Subtasks already in
        DONE state in the store are skipped (idempotent for resume + for
        re-entry after a fixup round). On a subtask block, every still-
        PENDING subtask in the *passed-in* set after the failure is marked
        SKIPPED — the original-spec topo of `self.plan` is still consulted
        too, so cascade-skip semantics work transitively across spec ↔ fixup
        boundaries.
        """
        hard_max = self.cfg.subtask_hard_max_attempts
        for subtask in subtasks:
            # v3 stacked-diffs fix: if a parent merged while we were mid-flight,
            # rebase + retarget before starting the next subtask. Safe boundary.
            rebase_outcome = self._handle_parent_rebase_if_needed()
            if rebase_outcome:
                return rebase_outcome
            # v3 own-branch divergence: detect upstream commits to our branch
            # (operator hand-edit, parallel workspace, force-push). Pure-FF
            # auto-recovers via reset --hard; force-push BLOCKS cleanly.
            divergence_outcome = self._handle_branch_divergence_if_needed()
            if divergence_outcome:
                return divergence_outcome
            # Resume path: a subtask already marked DONE in the store stays
            # done — don't re-run the doer/checker. Same for SKIPPED. This
            # makes `quikode resume <id>` a no-op for already-finished slices.
            existing = self.store.get_subtask(self.node.id, subtask.id)
            if existing and existing["state"] == SubtaskState.DONE.value:
                continue
            if existing and existing["state"] == SubtaskState.SKIPPED.value:
                # Pre-existing skipped state from a prior partial run is
                # treated as a hard failure — we can't safely continue.
                return WorkerOutcome(State.BLOCKED, f"subtask {subtask.id} pre-existed in SKIPPED state")
            # v3 priority preempt: at this subtask boundary, check whether a
            # higher-priority queued task warrants yielding. Off by default;
            # opt in via cfg.preempt_at_subtask_boundary. Yield surrenders
            # the slot via the resume path so the next pick fills with a
            # more urgent candidate (e.g. a stacked child of a now-merged
            # parent, or a high-fan-out root that just became ready).
            yield_outcome = self._maybe_yield_at_boundary()
            if yield_outcome is not None:
                return yield_outcome
            triage_notes: str | None = None
            settled = False
            # v3 fix #24: seed the local attempt counter from the cumulative
            # `retries` column so progress-check cadence (fires at attempt ==
            # cfg.subtask_progress_check_after, then every N) keeps firing
            # across daemon restarts. Without this, a long-running stuck
            # subtask could survive multiple restart cycles, each restarting
            # `attempt = 0` and only ever firing the progress check at the
            # FIRST cadence point — never at higher attempt numbers where
            # flatline-block would have caught it earlier.
            #
            # `existing` is the SubtaskRow read above; on resume it carries
            # the cumulative retries. Fresh subtasks (no row yet) start at 0.
            attempt = int((existing or {}).get("retries") or 0)
            block_reason: str | None = None
            consecutive_transients = 0
            transient_max = self.cfg.subtask_transient_max_retries
            while attempt < hard_max:
                attempt += 1
                self._do_subtask(subtask, attempt, triage_notes)
                outcome = self._check_subtask(subtask)
                verdict, checker_text, transient = outcome.verdict, outcome.checker_text, outcome.transient
                if transient:
                    # Container-level / fast-fail noise from the checker.
                    # Free-retry: don't bump attempt or trigger triage. Cap
                    # consecutive transients via subtask_transient_max_retries
                    # so a vanished container can't loop forever.
                    consecutive_transients += 1
                    if consecutive_transients > transient_max:
                        block_reason = (
                            f"subtask transient checker failures exceeded cap "
                            f"({transient_max}) — container or checker CLI looks gone"
                        )
                        break
                    self.store.increment_subtask_transient_retries(self.node.id, subtask.id)
                    # v3.5 retry classification: a transient checker failure
                    # is the most common infra-flake retry shape; record it
                    # so the operator can distinguish "container vanished 8x"
                    # from "doer produced bad code 8x".
                    cat, sig = retry_classify.classify_retry(
                        rc=outcome.rc,
                        stderr=outcome.stderr,
                        stdout=outcome.checker_text,
                        hint="checker",
                    )
                    self.store.append_retry_reason(
                        self.node.id,
                        subtask.id,
                        attempt=attempt,
                        category=cat,
                        signature=sig,
                        transient=True,
                    )
                    attempt -= 1  # don't burn the real-attempt budget
                    time.sleep(15)
                    continue
                consecutive_transients = 0
                if verdict is Verdict.PASS:
                    # v3 Phase A: gate the PASS through pre-commit + commit +
                    # push. Hook failure or commit/push failure is treated
                    # as a checker FAIL (triage runs, retry++) — except for
                    # transient push errors, which free-retry without
                    # touching the real-failure budget.
                    pass_outcome = self._handle_subtask_pass(subtask)
                    if pass_outcome.kind == "settled":
                        settled = True
                        break
                    if pass_outcome.kind == "transient_retry":
                        # Don't bump retries OR the attempt counter; loop
                        # again from the top of the doer/checker pair.
                        attempt -= 1
                        continue
                    # kind == "fail" — synthesize a checker FAIL and fall
                    # through to the existing triage path.
                    checker_text = pass_outcome.synthesized_checker_text
                self.store.transition(
                    self.node.id, State.TRIAGING_SUBTASK, note=f"{subtask.id} attempt {attempt} failed"
                )
                triage_notes = self._triage_subtask(subtask, attempt, hard_max, checker_text)
                self.store.update_subtask(
                    self.node.id, subtask.id, triage_notes=triage_notes, state=SubtaskState.TRIAGING.value
                )
                self.store.increment_subtask_retries(self.node.id, subtask.id)
                # v3.5 retry classification: real verdict-FAIL retry. Pass
                # the actual subprocess rc + stderr from `_check_subtask` so
                # the classifier has real signal (not `rc=?` placeholders).
                # The hint ensures the catch-all bucket is
                # `doer_output_invalid` rather than `other` when no pattern
                # matches.
                cat, sig = retry_classify.classify_retry(
                    rc=outcome.rc,
                    stderr=outcome.stderr,
                    stdout=outcome.checker_text,
                    hint="checker",
                )
                self.store.append_retry_reason(
                    self.node.id,
                    subtask.id,
                    attempt=attempt,
                    category=cat,
                    signature=sig,
                    transient=False,
                )

                # v3 Phase A: decide whether to run a progress check.
                # Cadence: first at attempt == subtask_progress_check_after,
                # then every subtask_progress_check_every attempts after that.
                if self._should_run_progress_check(attempt):
                    verdict_obj = self._run_progress_check(subtask, attempt)
                    if verdict_obj.verdict == "flatlined":
                        flatline_count = self.store.increment_subtask_flatline_count(self.node.id, subtask.id)
                        if flatline_count >= self.cfg.subtask_flatline_block_count:
                            block_reason = (
                                f"progress check flatlined {flatline_count} consecutive times — "
                                "stopping to prevent runaway retries"
                            )
                            break
                    else:
                        # progressing OR uncertain — reset consecutive flatline
                        # counter so a single isolated flatline doesn't poison
                        # later legitimate progress.
                        self.store.reset_subtask_flatline_count(self.node.id, subtask.id)
            if not settled:
                if block_reason is None:
                    block_reason = f"exhausted hard ceiling of {hard_max} attempts"
                self._mark_subtask_blocked(subtask, block_reason)
                # Mark every still-pending subtask as SKIPPED so the user
                # can see at-a-glance which slices never got a chance.
                self._mark_remaining_pending_as_skipped(after=subtask.id, subtasks=subtasks)
                reason = (
                    f"subtask {subtask.id} blocked: {block_reason}; "
                    "remaining subtasks skipped. use `quikode resume <id>` after fixing the cause."
                )
                self.store.transition(self.node.id, State.BLOCKED, note=reason, last_error=reason[:1000])
                return WorkerOutcome(State.BLOCKED, reason)
        return None  # all subtasks settled — fall through to caller (final_check or fixup re-check)

    def _maybe_yield_at_boundary(self) -> WorkerOutcome | None:
        """Check if a higher-priority queued task warrants yielding this slot.

        At a subtask-completion boundary, the worktree is clean (commit just
        landed, push just finished) — a safe pause point. If yielding, we
        transition the task back to PENDING with the resume marker, return
        a special outcome whose final_state is PENDING so the orchestrator
        frees the slot. The yielded task gets re-picked when its priority
        is highest among queued candidates.

        Off by default. Returns None unless `cfg.preempt_at_subtask_boundary`
        is True AND the priority delta exceeds `cfg.preempt_yield_threshold`.
        """
        if not self.cfg.preempt_at_subtask_boundary:
            return None
        # Score self as if currently pickable. Stacked-state isn't relevant
        # at yield time; what matters is unblock_boost + id ranking.
        my_score = scheduler.task_priority_if_picked(
            task_id=self.node.id,
            dag=self.dag,
            scope=set(self.dag.nodes.keys()),
        )
        # Best queued (PENDING) priority across all tasks. We pass scope=all
        # nodes since the worker doesn't see the orchestrator's --only scope;
        # in practice scope == all nodes for most ops modes, and a yield to a
        # task outside the orchestrator's scope is a no-op (the orchestrator
        # just won't pick it; some other in-flight slot will fill instead).
        best_id, best_score = scheduler.best_queued_priority(
            cfg=self.cfg,
            dag=self.dag,
            store=self.store,
            scope=set(self.dag.nodes.keys()),
            in_flight=set(),
        )
        if best_score is None:
            return None
        delta = best_score - my_score
        if delta <= self.cfg.preempt_yield_threshold:
            return None
        # Yield: re-pend self with resume marker so the next provision
        # picks up at the next subtask. The container/worktree are torn
        # down by the worker's normal exit path. Cost is ~2-3 min of
        # re-provision overhead — only worth it for sizable priority deltas.
        log.info(
            "task %s yielding subtask-boundary slot to %s (priority delta=%d, threshold=%d)",
            self.node.id,
            best_id,
            delta,
            self.cfg.preempt_yield_threshold,
        )
        note = (
            f"yielded to higher-priority candidate {best_id} "
            f"(delta={delta}, threshold={self.cfg.preempt_yield_threshold})"
        )
        self.store.transition(
            self.node.id,
            State.PENDING,
            note=note,
            resume_from_existing_subtasks=1,
            container_id=None,
        )
        return WorkerOutcome(State.PENDING, note)

    def _should_run_progress_check(self, attempt: int) -> bool:
        """Decide whether the progress-check agent should fire at this attempt.

        Fires at attempt == cfg.subtask_progress_check_after, and again
        every cfg.subtask_progress_check_every attempts after that.
        """
        after = self.cfg.subtask_progress_check_after
        every = self.cfg.subtask_progress_check_every
        if attempt < after:
            return False
        if every <= 0:
            return attempt == after
        return (attempt - after) % every == 0

    def _run_progress_check(self, subtask: Subtask, attempt: int) -> ProgressVerdict:
        """Run the progress-check agent and persist an audit row.

        Returns a ProgressVerdict; never raises (even if the agent times
        out or its output fails to parse — those collapse to
        verdict='uncertain' inside ProgressAgent.check).
        """
        attempts = self._recent_attempt_history(subtask)
        agent = build_progress_agent(self.cfg)
        outcome = agent.check(
            subtask=subtask,
            attempts=attempts,
            acceptance=tuple(subtask.acceptance),
            handle=self._h,
            log_path=self.log_path,
            timeout=180,
        )
        self.store.record_progress_check(
            self.node.id,
            subtask.id,
            attempts_at_check=attempt,
            verdict=outcome.verdict,
            rationale=outcome.rationale,
        )
        log.info(
            "subtask %s/%s progress check at attempt %d: %s — %s",
            self.node.id,
            subtask.id,
            attempt,
            outcome.verdict,
            outcome.rationale[:200],
        )
        return outcome

    def _recent_attempt_history(self, subtask: Subtask, n: int = 5) -> list[ProgressAttempt]:
        """Pull the last N (checker_root_cause, triage_notes) pairs.

        Sources: `recent_subtask_checker_outputs` (artifact stream — the
        canonical record of checker stdout) for root causes, and the
        subtask row's `triage_notes` column for the last triage notes.
        Older triage notes are not preserved per-attempt today; the
        progress agent gets the latest triage notes plus the full ladder
        of checker outputs, which is enough to diagnose flatline.
        """
        checker_outputs = self.store.recent_subtask_checker_outputs(self.node.id, subtask.id, limit=n)
        sub = self.store.get_subtask(self.node.id, subtask.id) or {}
        triage_notes = str(sub.get("triage_notes") or "")
        attempts: list[ProgressAttempt] = []
        # Number from oldest=1 to most-recent=N (left-to-right in the prompt).
        total = len(checker_outputs)
        for i, output in enumerate(checker_outputs):
            attempts.append(
                ProgressAttempt(
                    attempt_no=i + 1,
                    checker_root_cause=_extract_root_cause(output),
                    # Only the most recent triage_notes column is available.
                    triage_notes=triage_notes if i == total - 1 else "(earlier triage notes not retained)",
                )
            )
        return attempts

    # ----- subtask-level doer / checker / triage -----

    def _do_subtask(self, subtask: Subtask, attempt: int, triage_notes: str | None) -> None:
        self.store.transition(self.node.id, State.DOING_SUBTASK, note=f"{subtask.id} attempt {attempt}")
        self.store.update_subtask(self.node.id, subtask.id, state=SubtaskState.DOING.value)
        agent = build_agent(self.cfg.doer)
        prompt = prompts.subtask_doer_prompt(self.cfg, self.node, subtask, triage_notes=triage_notes)
        self._write_log_header(f"SUBTASK DOER {subtask.id} (attempt {attempt})", prompt)
        # 1h timeout per subtask — way under the whole-spec 4h, but plenty for
        # any single focused slice.
        result = agent.run(
            prompt, handle=self._h, log_path=self.log_path, timeout=self.cfg.subtask_doer_timeout_s
        )
        self.store.record_agent_call(
            self.node.id,
            phase="subtask_doer",
            cli=self.cfg.doer.cli,
            model=self.cfg.doer.model,
            rc=result.rc,
            duration_s=result.duration_s or 0,
            tokens_used=result.tokens_used,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            tokens_cached_read=result.tokens_cached_read,
            tokens_cached_creation=result.tokens_cached_creation,
            cost_usd=result.cost_usd,
            subtask_id=subtask.id,
        )
        self.last_doer_summary = result.stdout[-2000:]
        self.store.add_artifact(self.node.id, f"subtask_doer:{subtask.id}", result.stdout)

    def _check_subtask(self, subtask: Subtask) -> _CheckerOutcome:
        """Run the per-subtask checker. Returns a `_CheckerOutcome` carrying
        verdict + the full subprocess shape (rc, stdout, stderr) so callers
        can drive both the FAIL-handler path AND the retry-cause classifier
        with structured data.

        `transient=True` means the checker run itself failed in a way that
        looks like infrastructure (rc=124 timeout, container daemon error,
        etc) — the caller should free-retry without burning the attempt
        budget. As a backstop we also flag a degenerate fast-fail (rc!=0,
        <5s, no parseable VERDICT) — catches cases where the agent CLI
        bailed before the container was usable. Without this, three
        "attempts" can finish in seconds and burn the hard ceiling on
        infrastructure noise.
        """
        self.store.transition(self.node.id, State.CHECKING_SUBTASK, note=subtask.id)
        self.store.update_subtask(self.node.id, subtask.id, state=SubtaskState.CHECKING.value)
        agent = build_agent(self.cfg.checker)
        prompt = prompts.subtask_checker_prompt(self.cfg, self.node, subtask)
        self._write_log_header(f"SUBTASK CHECKER {subtask.id}", prompt)
        result = agent.run(
            prompt, handle=self._h, log_path=self.log_path, timeout=self.cfg.subtask_checker_timeout_s
        )
        self.store.record_agent_call(
            self.node.id,
            phase="subtask_checker",
            cli=self.cfg.checker.cli,
            model=self.cfg.checker.model,
            rc=result.rc,
            duration_s=result.duration_s or 0,
            tokens_used=result.tokens_used,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            tokens_cached_read=result.tokens_cached_read,
            tokens_cached_creation=result.tokens_cached_creation,
            cost_usd=result.cost_usd,
            subtask_id=subtask.id,
        )
        self.store.add_artifact(self.node.id, f"subtask_checker:{subtask.id}", result.stdout)
        transient = bool(result.transient)
        if (
            not transient
            and result.rc != 0
            and (result.duration_s or 0) < 5
            and "VERDICT:" not in (result.stdout or "")
        ):
            transient = True
        return _CheckerOutcome(
            verdict=_parse_verdict(result.stdout),
            checker_text=result.stdout or "",
            transient=transient,
            rc=int(result.rc) if result.rc is not None else None,
            stderr=getattr(result, "stderr", "") or "",
        )

    def _triage_subtask(self, subtask: Subtask, attempt: int, budget: int, checker_output: str) -> str:
        agent = build_agent(self.cfg.triage)
        prompt = prompts.subtask_triage_prompt(
            self.cfg,
            self.node,
            subtask,
            retry_count=attempt,
            retry_budget=budget,
            checker_output=checker_output,
            recent_doer_summary=self.last_doer_summary,
        )
        self._write_log_header(f"SUBTASK TRIAGE {subtask.id} (attempt {attempt})", prompt)
        result = agent.run(
            prompt, handle=self._h, log_path=self.log_path, timeout=self.cfg.subtask_checker_timeout_s
        )
        self.store.record_agent_call(
            self.node.id,
            phase="subtask_triage",
            cli=self.cfg.triage.cli,
            model=self.cfg.triage.model,
            rc=result.rc,
            duration_s=result.duration_s or 0,
            tokens_used=result.tokens_used,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            tokens_cached_read=result.tokens_cached_read,
            tokens_cached_creation=result.tokens_cached_creation,
            cost_usd=result.cost_usd,
            subtask_id=subtask.id,
        )
        self.store.add_artifact(self.node.id, f"subtask_triage:{subtask.id}", result.stdout)
        return result.stdout

    def _mark_subtask_done(self, subtask: Subtask) -> None:
        self.store.update_subtask(self.node.id, subtask.id, state=SubtaskState.DONE.value)

    # ----- v3 Phase A: per-subtask commit + pre-commit gate -----

    def _handle_subtask_pass(self, subtask: Subtask) -> _SubtaskPassOutcome:
        """Gate a Verdict.PASS through pre-commit + commit + push.

        Sequence:
        1. Run the configured pre-commit hook scoped to `subtask.files_to_touch`.
           Hook FAIL → bump `pre_commit_failures`, synthesize a checker FAIL.
        2. `git add` the declared files, `git commit`, `git push origin <branch>`.
           - Push FAIL with a transient marker → bump `transient_retries`,
             tell caller to free-retry (don't bump real `retries`).
           - Any other commit/push FAIL → synthesize a checker FAIL.
        3. On success: record `commit_sha` on the subtask row, mark DONE.

        The synthesized-checker-FAIL path keeps the existing FAIL handler
        (triage → retry) authoritative for *all* failures the operator
        cares about.
        """
        # 1. pre-commit gate
        gate_ok, gate_output = self._pre_commit_gate(subtask)
        if not gate_ok:
            self.store.increment_subtask_pre_commit_failures(self.node.id, subtask.id)
            checker_text = (
                f"VERDICT: FAIL\nROOT_CAUSE: pre-commit hook failed\nDETAILS:\n{gate_output[:4000]}"
            )
            return _SubtaskPassOutcome(kind="fail", synthesized_checker_text=checker_text)

        # 2. commit + push
        branch = str(self._row()["branch"])
        commit_msg = f"subtask({subtask.id}): {subtask.title}"
        result = worktree.commit_subtask(
            self._h,
            subtask,
            commit_msg,
            branch=branch,
            remote=self.cfg.pr_remote,
            push=True,
            log_path=self.log_path,
        )
        if not result.success:
            if result.transient:
                # Free retry — push failed for a network-y reason. Note: the
                # commit may have landed locally already (commit_sha is set
                # in that case); a subsequent attempt's `git commit` will
                # cleanly fail with "nothing to commit" and we'll surface
                # *that* as the real failure if the doer didn't add new
                # changes. For now, bump the counter and let the loop go.
                self.store.increment_subtask_transient_retries(self.node.id, subtask.id)
                log.warning(
                    "subtask %s/%s: transient push failure; free-retrying. output: %s",
                    self.node.id,
                    subtask.id,
                    result.output[:300],
                )
                return _SubtaskPassOutcome(kind="transient_retry")
            checker_text = f"VERDICT: FAIL\nROOT_CAUSE: commit/push failed\nDETAILS:\n{result.output[:4000]}"
            return _SubtaskPassOutcome(kind="fail", synthesized_checker_text=checker_text)

        # 3. settled
        self.store.update_subtask(self.node.id, subtask.id, commit_sha=result.commit_sha)
        self._mark_subtask_done(subtask)
        return _SubtaskPassOutcome(kind="settled")

    def _pre_commit_gate(self, subtask: Subtask) -> tuple[bool, str]:
        """Run the configured pre-commit hook against `subtask.files_to_touch`.

        Detection is per `cfg.pre_commit_runner`:
        - `none`: skipped, returns (True, "skipped").
        - `auto`: probes the worktree for `lefthook.yml` then
          `.pre-commit-config.yaml`; missing both → (True, "no hook configured").
        - `lefthook` / `pre-commit`: explicit, runs that runner.

        Hook timeout is `cfg.pre_commit_timeout_s` (default 300s). A
        timeout is treated as a real failure (NOT transient) — a hanging
        hook is a real problem the operator should see.
        """
        runner = self.cfg.pre_commit_runner
        if runner == "none" or not subtask.files_to_touch:
            return True, "skipped"

        if runner == "auto":
            resolved = self._detect_pre_commit_runner()
            if resolved is None:
                return True, "no hook configured"
            runner = resolved

        if runner == "lefthook":
            # lefthook v2 renamed --files (plural, space-separated) to --file
            # (singular, repeatable) and added --files-from-stdin. Use stdin
            # to be agnostic to file-list size and shell escaping. v1 didn't
            # support --files-from-stdin, but v2 (>=1.7) is what ships in
            # the dev image — staying on v2 syntax.
            cmd = "cd /workspace && lefthook run pre-commit --files-from-stdin"
            stdin = "\n".join(subtask.files_to_touch)
        elif runner == "pre-commit":
            files_arg = " ".join(shlex.quote(p) for p in subtask.files_to_touch)
            cmd = f"cd /workspace && pre-commit run --files {files_arg}"
            stdin = None
        else:
            return True, f"unknown runner {runner!r}; skipped"

        try:
            rc, out, err = exec_in(
                self._h,
                ["bash", "-lc", cmd],
                log_path=self.log_path,
                stdin=stdin,
                timeout=self.cfg.pre_commit_timeout_s,
            )
        except subprocess.TimeoutExpired:
            return False, f"pre-commit timed out after {self.cfg.pre_commit_timeout_s}s"

        combined = (out or "") + ("\n" + err if err else "")
        return rc == 0, combined

    def _detect_pre_commit_runner(self) -> Literal["lefthook", "pre-commit"] | None:
        """Probe the worktree for a known pre-commit toolchain config.
        lefthook wins over pre-commit when both are present (matches
        tanren's setup; explicit override available via cfg)."""
        rc, _, _ = exec_in(
            self._h,
            ["bash", "-lc", "test -f /workspace/lefthook.yml || test -f /workspace/lefthook.yaml"],
            log_path=self.log_path,
            timeout=10,
        )
        if rc == 0:
            return "lefthook"
        rc, _, _ = exec_in(
            self._h,
            ["bash", "-lc", "test -f /workspace/.pre-commit-config.yaml"],
            log_path=self.log_path,
            timeout=10,
        )
        if rc == 0:
            return "pre-commit"
        return None

    def _mark_subtask_blocked(self, subtask: Subtask, reason: str) -> None:
        self.store.update_subtask(
            self.node.id,
            subtask.id,
            state=SubtaskState.BLOCKED.value,
            last_error=reason,
        )
        log.warning("subtask %s/%s blocked: %s", self.node.id, subtask.id, reason)
        # v3 Phase D operator-polish: when a subtask blocks, surface that on
        # the PR (if one is open) so the user sees the failure mode, the
        # last few root causes, and the three intervention paths without
        # having to spelunk through SQLite or the worktree.
        try:
            self._post_blocked_pr_comment(subtask, reason)
        except Exception:
            # Best-effort: never let the PR comment failure mask the BLOCKED
            # transition itself. The block is recorded regardless.
            log.exception("failed to post BLOCKED PR comment for %s/%s", self.node.id, subtask.id)

    def _post_blocked_pr_comment(self, subtask: Subtask, reason: str) -> None:
        """Post a PR comment + add the `quikode:blocked` label when a
        subtask blocks. Best-effort; logs and continues on `gh` failures.

        The PR is found via either `draft_pr_number` (set when v3 opens the
        draft PR after S-01) or `pr_number` (set by the legacy ready-for-
        review flow). If neither is set there's no PR yet — nothing to do.
        """
        row = self.store.get(self.node.id) or {}
        pr_number = row.get("draft_pr_number") or row.get("pr_number")
        if not pr_number:
            return  # No PR open yet — nothing to comment on.
        try:
            pr_number_int = int(pr_number)
        except (TypeError, ValueError):
            return

        # Pull last 3 attempt root causes from the same helper the progress
        # check uses, so the comment matches what the agent saw.
        try:
            attempts = self._recent_attempt_history(subtask, n=3)
        except Exception:
            attempts = []
        attempts_section = ""
        if attempts:
            lines = []
            for a in attempts:
                rc = (a.checker_root_cause or "(no checker output captured)").strip()
                lines.append(f"- attempt {a.attempt_no}: {rc[:300]}")
            attempts_section = "\n\nLast {} attempts:\n{}".format(len(attempts), "\n".join(lines))

        worktree_path = row.get("worktree_path") or "(unknown — see `quikode show`)"
        body = (
            f"## Blocked at {subtask.id}\n\n"
            f"Reason: {reason}{attempts_section}\n\n"
            "To unblock, choose one:\n"
            f"1. **Push fixes to this branch directly.** Daemon detects new commits and resumes from {subtask.id}.\n"
            "2. **Reply with guidance** as a review comment on this PR. Daemon picks it up via the review loop.\n"
            f"3. **Locally**: `quikode unblock {self.node.id}` (opens worktree at {worktree_path}) "
            f"then `quikode resume {self.node.id}`.\n"
        )

        # Run gh from the repo root; HTTPS auth comes from gh's own state.
        try:
            subprocess.run(
                ["gh", "pr", "comment", str(pr_number_int), "--body", body],
                cwd=str(self.cfg.repo_path),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("gh pr comment failed for #%d: %s", pr_number_int, e)
        try:
            subprocess.run(
                ["gh", "pr", "edit", str(pr_number_int), "--add-label", "quikode:blocked"],
                cwd=str(self.cfg.repo_path),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("gh pr edit add-label failed for #%d: %s", pr_number_int, e)

    def _mark_subtask_skipped(self, subtask: Subtask, reason: str) -> None:
        self.store.update_subtask(
            self.node.id,
            subtask.id,
            state=SubtaskState.SKIPPED.value,
            last_error=reason,
        )
        log.info("subtask %s/%s skipped: %s", self.node.id, subtask.id, reason)

    def _mark_remaining_pending_as_skipped(
        self, *, after: str, subtasks: list[Subtask] | None = None
    ) -> None:
        """When a subtask blocks the task, every still-pending subtask after
        it in the iteration order needs to be visibly marked SKIPPED so the
        user can see at-a-glance which slices the failure cost them. Resume
        flips them back to PENDING via the cascade-skipped re-pend path.

        `subtasks` defaults to the original spec topo order. Pass an explicit
        list when running fixup decomposition so post-block skips operate on
        the fixup-round's slices instead of (or in addition to) the spec ones.
        """
        if subtasks is None:
            assert self.plan is not None
            subtasks = self.plan.topo_order()
        seen_marker = False
        for subtask in subtasks:
            if subtask.id == after:
                seen_marker = True
                continue
            if not seen_marker:
                continue
            existing = self.store.get_subtask(self.node.id, subtask.id)
            if existing and existing["state"] == SubtaskState.PENDING.value:
                self._mark_subtask_skipped(subtask, f"upstream subtask {after} blocked")

    # ----- final whole-spec check -----

    # ----- fixup decomposition (used by audit-gauntlet failures + CI-fix dispatch) -----

    def _run_fixup_round(
        self,
        *,
        kind: str,
        round_no: int,
        trigger: str,
        checker_output: str | None = None,
        ci_excerpt: str | None = None,
        review_threads_block: str | None = None,
        triage_root_cause: str | None = None,
        expected_finding_ids: list[str] | None = None,
    ) -> WorkerOutcome | None:
        """Plan a fixup round, append the slices to the subtasks table, and
        run them through the same per-subtask machinery as spec subtasks.

        On planner failure (parse error, empty output) falls back to the
        legacy monolithic `_do(attempt=...)` so we never get stuck without
        ANY attempt at fixing the failure.

        Returns:
            None if all fixup subtasks settled (caller re-checks).
            WorkerOutcome(BLOCKED) if a fixup subtask blocked or the
                fixup planner failed AND the legacy fallback also can't
                make progress (which the caller surfaces as task BLOCKED).
        """
        self.store.transition(
            self.node.id,
            State.FIXUP_PLANNING,
            note=f"{kind} round {round_no} ({trigger})",
        )
        fixup_plan = self._invoke_fixup_planner(
            kind=kind,
            round_no=round_no,
            trigger=trigger,
            checker_output=checker_output,
            ci_excerpt=ci_excerpt,
            review_threads_block=review_threads_block,
            triage_root_cause=triage_root_cause,
        )
        # Completeness check: for audit-driven fixup rounds, every
        # `expected_finding_ids` entry must appear in the planner's
        # `findings_addressed` AND in at least one subtask's
        # `addresses_findings`. If any are missing, re-prompt the planner
        # once with an explicit gap list. If still missing after the
        # retry, BLOCK — better to surface to the operator than to ship
        # a fixup that drops findings.
        if (
            fixup_plan is not None
            and fixup_plan.subtasks
            and expected_finding_ids
            and kind == "fixup-pre-pr-audit"
        ):
            missing = self._missing_finding_coverage(fixup_plan, expected_finding_ids)
            if missing:
                log.warning(
                    "fixup planner missed %d finding(s) for %s round %d; re-prompting: %s",
                    len(missing),
                    kind,
                    round_no,
                    ", ".join(sorted(missing)[:8]),
                )
                gap_addendum = (
                    "## ⚠️ Coverage gap from your previous attempt\n\n"
                    "Your previous plan missed the following finding ids — "
                    "include each one in `findings_addressed` and assign "
                    "each to a subtask's `addresses_findings`:\n\n"
                    + "\n".join(f"- `{fid}`" for fid in sorted(missing))
                    + "\n\n"
                    "Re-emit the COMPLETE plan (do not emit only the deltas)."
                )
                augmented_root = (gap_addendum + "\n\n---\n\n" + (triage_root_cause or ""))[:16000]
                fixup_plan = self._invoke_fixup_planner(
                    kind=kind,
                    round_no=round_no,
                    trigger=trigger,
                    checker_output=checker_output,
                    ci_excerpt=ci_excerpt,
                    review_threads_block=review_threads_block,
                    triage_root_cause=augmented_root,
                )
                if fixup_plan is not None and fixup_plan.subtasks:
                    still_missing = self._missing_finding_coverage(fixup_plan, expected_finding_ids)
                    if still_missing:
                        note = (
                            f"fixup planner still missed {len(still_missing)} finding(s) "
                            f"after re-prompt for {kind} round {round_no}: "
                            f"{', '.join(sorted(still_missing)[:8])}; BLOCKing"
                        )
                        log.warning(note)
                        self.store.transition(
                            self.node.id,
                            State.BLOCKED,
                            note=note,
                            last_error=note[:1000],
                        )
                        return WorkerOutcome(State.BLOCKED, note)
        if fixup_plan is None or not fixup_plan.subtasks:
            note = (
                f"fixup planner returned empty/invalid plan for {kind} round "
                f"{round_no} ({trigger}); BLOCKing for operator review"
            )
            log.warning(note)
            self.store.transition(
                self.node.id,
                State.BLOCKED,
                note=note,
                last_error=note[:1000],
            )
            return WorkerOutcome(State.BLOCKED, note)

        # Persist the new fixup subtasks (additive — does NOT delete spec rows).
        self.store.append_subtasks(
            self.node.id,
            [
                {
                    "subtask_id": s.id,
                    "title": s.title,
                    "depends_on": list(s.depends_on),
                    "files_to_touch": list(s.files_to_touch),
                    "boundary": s.boundary,
                    "acceptance": list(s.acceptance),
                    "notes": s.notes,
                    "kind": s.kind or kind,
                }
                for s in fixup_plan.subtasks
            ],
        )
        log.info(
            "fixup round %d (%s): planned %d subtask(s): %s",
            round_no,
            kind,
            len(fixup_plan.subtasks),
            ", ".join(s.id for s in fixup_plan.subtasks),
        )
        return self._run_subtask_set(list(fixup_plan.subtasks))

    @staticmethod
    def _missing_finding_coverage(plan: FixupPlan, expected_finding_ids: list[str]) -> set[str]:
        """Compute the set of expected finding ids the plan does NOT cover.

        Coverage = id appears in `plan.findings_addressed` AND in at
        least one subtask's `addresses_findings` (extra notes field).
        Since `Subtask` doesn't have a typed `addresses_findings` field
        (extra="forbid" on the model), the planner's per-subtask mapping
        rides only in the `findings_addressed` plan-level array. We
        therefore validate completeness against that single source.
        """
        expected = set(expected_finding_ids)
        covered = set(plan.findings_addressed)
        return expected - covered

    def _invoke_fixup_planner(
        self,
        *,
        kind: str,
        round_no: int,
        trigger: str,
        checker_output: str | None,
        ci_excerpt: str | None,
        review_threads_block: str | None,
        triage_root_cause: str | None,
    ) -> FixupPlan | None:
        """Run the fixup planner agent and parse its output. Returns None on
        any error so the caller can fall back to the legacy monolithic doer."""
        agent = build_agent(self.cfg.planner)
        # Build the context the planner needs: original final_acceptance,
        # done spec subtasks, and any prior fixup subtasks (so the new round
        # can avoid duplicating earlier fixup work).
        rows = self.store.list_subtasks(self.node.id)
        done_subtasks: list[dict] = []
        prior_fixup_subtasks: list[dict] = []
        for r in rows:
            kind_val = (r.get("kind") or "spec") if isinstance(r, dict) else "spec"
            row_view = {
                "subtask_id": r["subtask_id"],
                "title": r.get("title") or "",
                "kind": kind_val,
                "state": r["state"],
            }
            if kind_val == "spec":
                if r["state"] == SubtaskState.DONE.value:
                    done_subtasks.append(row_view)
            else:
                prior_fixup_subtasks.append(row_view)
        original_final_acceptance: list[str] = []
        if self.plan is not None:
            original_final_acceptance = list(self.plan.final_acceptance)
        prompt = prompts.fixup_planner_prompt(
            self.cfg,
            self.node,
            kind=kind,
            round_no=round_no,
            max_rounds=self.cfg.fixup_max_rounds,
            trigger=trigger,
            original_final_acceptance=original_final_acceptance,
            done_subtasks=done_subtasks,
            prior_fixup_subtasks=prior_fixup_subtasks,
            checker_output=checker_output,
            ci_excerpt=ci_excerpt,
            review_threads_block=review_threads_block,
            triage_root_cause=triage_root_cause,
        )
        self._write_log_header(f"FIXUP PLANNER {kind} round {round_no}", prompt)
        result = agent.run(prompt, handle=self._h, log_path=self.log_path, timeout=600)
        self.store.record_agent_call(
            self.node.id,
            phase=f"fixup_planner:{kind}",
            cli=self.cfg.planner.cli,
            model=self.cfg.planner.model,
            rc=result.rc,
            duration_s=result.duration_s or 0,
            tokens_used=result.tokens_used,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            tokens_cached_read=result.tokens_cached_read,
            tokens_cached_creation=result.tokens_cached_creation,
            cost_usd=result.cost_usd,
        )
        self.store.add_artifact(
            self.node.id,
            f"fixup_planner_output:{kind}:{round_no}",
            result.stdout,
        )
        if result.rc != 0:
            log.warning("fixup planner exited rc=%d (kind=%s round=%d)", result.rc, kind, round_no)
            return None
        try:
            return parse_fixup_planner_output(result.stdout)
        except PlanValidationError as e:
            log.warning(
                "fixup planner output didn't validate (kind=%s round=%d): %s",
                kind,
                round_no,
                e,
            )
            return None

    def _run_manual_probes(self) -> str:
        """Scan the node's `expected_evidence` for `kind="manual"` items,
        run them through `ManualProbeRunner`, and return a pre-rendered
        block to inject into the checker prompt.

        Defensive: NEVER raises. Parse errors, runner failures, container
        glitches all degrade to "no manual probes ran" — the checker
        agent then judges PASS/FAIL on `just ci` + acceptance alone, the
        same as the pre-runner behavior.
        """
        try:
            probes = manual_probe.collect_probes_from_evidence(list(self.node.expected_evidence))
        except Exception as e:
            log.warning("manual probes: collect raised %s; skipping", e)
            return ""
        if not probes:
            return ""
        if self.handle is None:
            log.info("manual probes: no container handle; skipping %d probe(s)", len(probes))
            return ""
        log.info("manual probes: running %d probe(s) for task %s", len(probes), self.node.id)
        try:
            with manual_probe.ManualProbeRunner(
                handle=self.handle,
                exec_in=exec_in,
                log_path=self.log_path,
                credentials=manual_probe.credentials_from_env(["TANREN_MCP_API_KEY", "TANREN_API_KEY"]),
            ) as runner:
                results = runner.run_all_probes(probes)
        except Exception as e:
            log.warning("manual probes: runner raised %s; degrading", e)
            return ""
        return manual_probe.render_probe_block(results)

    # ----- phase: commit + push -----

    def _commit_push(self) -> WorkerOutcome | None:
        # v3 stacked-diffs fix: parent merge may have landed during final-check.
        rebase_outcome = self._handle_parent_rebase_if_needed()
        if rebase_outcome:
            return rebase_outcome
        self.store.transition(self.node.id, State.COMMITTING)
        msg = f"{self.node.id}: {self.node.title}\n\nPlanned and implemented by quikode."
        rc, out = github.commit_all(self._h, msg, log_path=self.log_path)
        branch = str(self._row()["branch"])
        if rc != 0:
            if "nothing to commit" in out or "no changes added to commit" in out:
                # Working tree is clean. With v3 per-subtask commits, this is
                # the common case: every subtask already committed its slice
                # during the loop. Check if the branch carries those commits
                # ahead of the base; if so, push and continue. Only treat as a
                # genuine no-op when the branch is also empty.
                ahead = github.ahead_count(self._h, branch, base=self.cfg.base_branch, log_path=self.log_path)
                if ahead > 0:
                    log.info(
                        "no uncommitted diff but branch is %d commits ahead of %s — proceeding to push",
                        ahead,
                        self.cfg.base_branch,
                    )
                    # fall through to push
                else:
                    self.store.transition(
                        self.node.id,
                        State.PENDING_CI,
                        note="no diff — task already complete or doer made no changes",
                    )
                    sound.ding()
                    return WorkerOutcome(State.PENDING_CI, "no diff")
            else:
                # commit failed for a real reason (hook gate, repo state) and
                # the per-subtask flow already commits each slice — so a
                # whole-spec commit failure here means something the audit
                # gauntlet would flag anyway. Block with the failure output;
                # operator inspects the worktree.
                self.store.transition(
                    self.node.id,
                    State.BLOCKED,
                    note=f"commit failed (post-subtasks): {out[:200]}",
                    last_error=out[:1000],
                )
                return WorkerOutcome(State.BLOCKED, "commit failed post-subtasks")

        self.store.transition(self.node.id, State.PUSHING)
        rc, out = github.push(self._h, branch, remote=self.cfg.pr_remote, log_path=self.log_path)
        if rc != 0:
            self.store.transition(
                self.node.id,
                State.BLOCKED,
                note=f"push failed: {out[:200]}",
                last_error=out[:1000],
            )
            return WorkerOutcome(State.BLOCKED, "push failed")
        return None

    # ----- v3.6 phase: pre-PR pipeline (local-CI + 3 audits) -----

    def _run_pre_pr_pipeline(self) -> WorkerOutcome | None:
        """4-stage gate before opening a PR. Returns:

          - `None` on full pass (caller proceeds to `_open_pr`).
          - `WorkerOutcome(BLOCKED)` when `cfg.pre_pr_audit_max_cycles`
            cycles all fail — operator decides next steps via the
            BLOCK-forensics report.

        Each cycle runs all four stages (local-CI, rubric, standards,
        behavior) regardless of failure along the way — we want a single
        consolidated report rather than serial fail-fast that misses
        downstream issues. Failures merge into a triage bundle, the fixup
        planner emits subtasks (`kind="fixup-pre-pr-audit"`), the
        per-subtask loop addresses them, then we re-enter the pipeline
        from the top.
        """
        for cycle in range(1, self.cfg.pre_pr_audit_max_cycles + 1):
            log.info(
                "task %s: pre-pr pipeline cycle %d/%d", self.node.id, cycle, self.cfg.pre_pr_audit_max_cycles
            )
            # Seed the audit summary so the TUI shows queued / in-flight /
            # done states for each stage as the cycle progresses.
            self.store.begin_pre_pr_audit_cycle(self.node.id, cycle)
            self.store.transition(
                self.node.id,
                State.LOCAL_CI_CHECKING,
                note=f"pre-pr cycle {cycle}: local-ci ({self.cfg.local_ci_command})",
            )

            # Build the diff excerpt against the base branch — every audit
            # consumes this. Compute once per cycle (commits may have
            # changed during the prior cycle's fixup loop).
            diff_excerpt = self._compute_branch_diff_excerpt()
            plan_text = str(self._row().get("plan_text") or "")

            # Stage 0: local CI gate (inside dev container).
            local_ci = pre_pr_audit.run_local_ci_gate(
                cfg=self.cfg,
                handle=self._h,
                log_path=self.log_path,
            )
            self.store.update_pre_pr_audit_stage(
                self.node.id,
                cycle=cycle,
                stage_name="local_ci",
                passed=local_ci.passed,
                summary=local_ci.summary,
            )

            # Stages 1-3: audit agents — each transitions PRE_PR_AUDITING
            # with a stage-specific note so the TUI can show "rubric
            # audit" rather than the opaque "auditing" umbrella state.
            standards_text = pre_pr_audit.collect_standards_text(self.cfg)

            self.store.transition(
                self.node.id,
                State.PRE_PR_AUDITING,
                note=f"pre-pr cycle {cycle}: rubric audit (codex)",
            )
            rubric = pre_pr_audit.run_rubric_audit(
                cfg=self.cfg,
                handle=self._h,
                diff_excerpt=diff_excerpt,
                plan_text=plan_text,
                log_path=self.log_path,
            )
            self.store.update_pre_pr_audit_stage(
                self.node.id,
                cycle=cycle,
                stage_name="rubric",
                passed=rubric.passed,
                summary=rubric.summary,
            )

            self.store.transition(
                self.node.id,
                State.PRE_PR_AUDITING,
                note=f"pre-pr cycle {cycle}: standards audit (claude-opus)",
            )
            standards = pre_pr_audit.run_standards_audit(
                cfg=self.cfg,
                handle=self._h,
                diff_excerpt=diff_excerpt,
                standards_text=standards_text,
                log_path=self.log_path,
            )
            self.store.update_pre_pr_audit_stage(
                self.node.id,
                cycle=cycle,
                stage_name="standards",
                passed=standards.passed,
                summary=standards.summary,
            )

            self.store.transition(
                self.node.id,
                State.PRE_PR_AUDITING,
                note=f"pre-pr cycle {cycle}: behavior audit (codex)",
            )
            behavior = pre_pr_audit.run_behavior_audit(
                cfg=self.cfg,
                handle=self._h,
                expected_evidence=list(self.node.expected_evidence or []),
                diff_excerpt=diff_excerpt,
                plan_text=plan_text,
                log_path=self.log_path,
            )
            self.store.update_pre_pr_audit_stage(
                self.node.id,
                cycle=cycle,
                stage_name="behavior",
                passed=behavior.passed,
                summary=behavior.summary,
            )

            cycle_result = pre_pr_audit.PipelineCycleResult(
                cycle=cycle,
                stages=[local_ci, rubric, standards, behavior],
            )
            for s in cycle_result.stages:
                log.info(
                    "task %s pre-pr cycle %d stage `%s`: %s",
                    self.node.id,
                    cycle,
                    s.name,
                    "PASS" if s.passed else "FAIL",
                )

            if cycle_result.passed:
                log.info(
                    "task %s pre-pr pipeline passed on cycle %d/%d — proceeding to open PR",
                    self.node.id,
                    cycle,
                    self.cfg.pre_pr_audit_max_cycles,
                )
                return None

            # Failure path: merge findings → triage → fixup planner.
            self.store.transition(
                self.node.id,
                State.PRE_PR_TRIAGING,
                note=(
                    f"pre-pr cycle {cycle} failed: " + ", ".join(s.name for s in cycle_result.failed_stages)
                ),
            )
            findings_block = pre_pr_audit.merge_failed_stage_reports(cycle_result.failed_stages)
            expected_finding_ids = pre_pr_audit.collect_finding_ids(cycle_result.failed_stages)
            self.store.add_artifact(
                self.node.id,
                f"pre_pr_audit:cycle_{cycle}",
                findings_block,
            )
            # Completeness-augmented findings block: prepend an explicit
            # "every id below MUST appear in your `findings_addressed`"
            # instruction so the planner cannot drop findings to fit a
            # smaller subtask count.
            if expected_finding_ids:
                augmented = (
                    "## Required finding coverage\n\n"
                    "Every id below MUST appear in your output's "
                    "`findings_addressed` array AND be referenced by at "
                    "least one subtask's `addresses_findings` field. "
                    "Dropping ids is forbidden.\n\n"
                    + "\n".join(f"- `{fid}`" for fid in expected_finding_ids)
                    + "\n\n---\n\n"
                    + findings_block
                )
            else:
                augmented = findings_block
            outcome = self._run_fixup_round(
                kind="fixup-pre-pr-audit",
                round_no=cycle,
                trigger="pre_pr_audit",
                triage_root_cause=augmented[:16000],
                expected_finding_ids=expected_finding_ids,
            )
            if outcome and outcome.final_state == State.BLOCKED:
                # Fixup ceiling exhausted on the audit round — surface as
                # a task BLOCK with the merged findings as the operator
                # context. The block-forensics dump (separate path) picks
                # this up automatically since the artifact is on the row.
                return outcome
            # Loop back to the top — re-run the full pipeline against
            # whatever the doer just landed.

        # Exhausted cycles.
        note = (
            f"pre-PR audit pipeline exhausted {self.cfg.pre_pr_audit_max_cycles} "
            "cycle(s) without a clean pass — manual review required"
        )
        self.store.transition(
            self.node.id,
            State.BLOCKED,
            note=note,
            last_error=note[:1000],
        )
        return WorkerOutcome(State.BLOCKED, note)

    def _compute_branch_diff_excerpt(self, max_lines: int = 1500) -> str:
        """Capture the worktree branch diff against `cfg.base_branch`. Used
        by the audit agents as the canonical "what changed" reference.

        Truncated to `max_lines` so the prompts stay within model context
        windows. The audit prompts further truncate per-stage based on
        which stage is most diff-hungry."""
        rc, out = self._git_in_workspace(["diff", f"{self.cfg.pr_remote}/{self.cfg.base_branch}...HEAD"])
        if rc != 0 or not out:
            return ""
        lines = out.splitlines()
        if len(lines) > max_lines:
            head = lines[:max_lines]
            head.append(f"... (diff truncated; {len(lines) - max_lines} more lines)")
            return "\n".join(head)
        return out

    # ----- phase: open PR + poll -----

    def _open_pr(self) -> WorkerOutcome | None:
        # v3 stacked-diffs fix: prefer the explicit flag set by the
        # orchestrator over the ls-remote race below. When the flag is set
        # we know with certainty the parent merged; do the rebase + clear
        # parent metadata before attempting to open the PR against main.
        rebase_outcome = self._handle_parent_rebase_if_needed()
        if rebase_outcome:
            return rebase_outcome
        self.store.transition(self.node.id, State.PR_OPENING)
        # Idempotent re-entry: if this task already has a PR row in the
        # store (from a prior run that pushed but crashed before
        # transitioning to PENDING_CI, or from a daemon restart that
        # orphan-recovered the task post-PR-open), skip `gh pr create`
        # and reuse the existing PR. Without this, the second pass fails
        # with `gh: a pull request for branch X already exists` and the
        # task BLOCKs unnecessarily — observed live on R-0015 after the
        # 2026-05-04 daemon restart.
        row = self._row()
        if row.get("pr_number") and row.get("pr_url"):
            log.info(
                "task %s: PR #%s already exists at %s — reusing instead of re-creating",
                self.node.id,
                row["pr_number"],
                row["pr_url"],
            )
            return None
        title = f"{self.node.id}: {self.node.title}"
        body = self._pr_body()
        # v2 Phase C: stacked PR targets the parent branch when stacking.
        # v3 fix: if the parent merged + GitHub deleted its branch while we
        # were running but the flag wasn't raised in time (e.g. orchestrator
        # missed the merge event), fall back to main via ls-remote check
        # so `gh pr create` doesn't fail with "Base ref must be a branch".
        pr_base = row.get("parent_branch") or self.cfg.base_branch
        if pr_base != self.cfg.base_branch and not self._remote_branch_exists(pr_base):
            log.info(
                "task %s: parent branch %s no longer exists on remote (likely merged); "
                "rebasing onto %s and retargeting PR",
                self.node.id,
                pr_base,
                self.cfg.base_branch,
            )
            rebase_ok = self._rebase_to_base_branch()
            if not rebase_ok:
                self.store.transition(
                    self.node.id,
                    State.BLOCKED,
                    note=f"parent branch {pr_base} deleted; rebase to {self.cfg.base_branch} failed",
                )
                return WorkerOutcome(State.BLOCKED, "post-parent-merge rebase failed")
            self.store.set_field(self.node.id, parent_branch=None, parent_pr_branch=None)
            pr_base = self.cfg.base_branch
        rc, url, out = github.open_pr(self._h, title, body, base=pr_base, log_path=self.log_path)
        if rc != 0 or not url:
            self.store.transition(self.node.id, State.BLOCKED, note=f"PR create failed: {out[:300]}")
            return WorkerOutcome(State.BLOCKED, "PR create failed")
        m = re.search(r"/pull/(\d+)", url)
        pr_number = int(m.group(1)) if m else 0
        self.store.set_field(self.node.id, pr_url=url, pr_number=pr_number)
        return None

    def _remote_branch_exists(self, branch: str) -> bool:
        """Cheap check: does origin still have this branch? Used by _open_pr
        to detect parent-merged-and-deleted before calling `gh pr create`."""
        rc, out = self._git_in_workspace(["ls-remote", "--heads", self.cfg.pr_remote, branch])
        return rc == 0 and bool(out.strip())

    def _handle_branch_divergence_if_needed(self) -> WorkerOutcome | None:
        """Worker-side checkpoint: detect + recover from upstream commits on
        the child's own branch (operator hand-edit, parallel quikode workspace,
        GitHub web UI commit, etc.).

        Three classes of upstream change, three actions:
        - Pure FF (we're behind, no local commits ahead): `git reset --hard
          origin/<branch>`. Worker continues from next pending subtask.
        - Force-push (history rewritten, our base sha no longer reachable
          from origin): BLOCK with a clear "manual recovery needed" note.
        - Diverged but mergeable (both have new commits): NOT YET HANDLED
          here — falls through to legacy push-fail path in `commit_subtask`.
          Future improvement: `git pull --rebase` + conflict-resolver.

        Best-effort: any failure in detection (network, git error) returns
        None so downstream push surfaces it as before. This can never make
        things worse than the legacy behavior.

        Returns None on no-op or successful recovery; WorkerOutcome(BLOCKED)
        on unrecoverable divergence (force-push only, today).
        """
        row = self._row()
        branch = row.get("branch")
        if not branch or self.handle is None:
            return None
        # Skip on review-response cycles where the doer pushes 4-5 mini-commits
        # in a 5-min window — polling fetch every subtask boundary wastes time.
        # The pre-push divergence check still catches non-FF in those cases.
        # Heuristic: if any subtask has kind starting with 'fixup-review' AND
        # is currently in DOING/CHECKING/TRIAGING, we're in a fast cycle.
        active_fixup_review = self.store.conn.execute(
            "SELECT 1 FROM subtasks WHERE task_id = ? AND kind LIKE 'fixup-review%' "
            "AND state IN ('doing','checking','triaging') LIMIT 1",
            (self.node.id,),
        ).fetchone()
        if active_fixup_review:
            return None

        # 1. fetch origin/<branch> (just our branch — no full origin fetch).
        rc_fetch, _out = self._git_in_workspace(["fetch", self.cfg.pr_remote, branch])
        if rc_fetch != 0:
            # Network blip or non-existent remote (haven't pushed yet) — not
            # a divergence, just nothing to compare against. Skip.
            return None

        # 2. Compare HEAD vs origin/<branch>.
        rc_b, out_b = self._git_in_workspace(
            ["rev-list", "--count", "--left-right", f"HEAD...{self.cfg.pr_remote}/{branch}"]
        )
        if rc_b != 0:
            return None
        # `--left-right` returns "<ahead>\t<behind>" — local ahead vs remote.
        try:
            parts = out_b.strip().splitlines()[-1].split()
            ahead = int(parts[0]) if len(parts) >= 1 else 0
            behind = int(parts[1]) if len(parts) >= 2 else 0
        except (ValueError, IndexError):
            return None

        if behind == 0:
            return None  # remote has nothing new — common case, fast path

        # We're behind. Determine if it's safe to fast-forward.
        if ahead == 0:
            # Pure FF — operator pushed commits, we have no local-only work.
            # `git reset --hard origin/<branch>` brings us in line.
            log.info(
                "task %s: detected upstream FF on %s (behind=%d). Resetting --hard to origin/%s.",
                self.node.id,
                branch,
                behind,
                branch,
            )
            rc_r, _ = self._git_in_workspace(["reset", "--hard", f"{self.cfg.pr_remote}/{branch}"])
            if rc_r != 0:
                self.store.transition(
                    self.node.id,
                    State.BLOCKED,
                    note=f"upstream FF detected on {branch} but `git reset --hard` failed",
                )
                return WorkerOutcome(State.BLOCKED, "upstream FF reset failed")
            return None

        # ahead > 0 AND behind > 0 — diverged. Check force-push.
        base_ref_sha = row.get("base_ref_sha") or ""
        if base_ref_sha:
            rc_anc, _ = self._git_in_workspace(
                ["merge-base", "--is-ancestor", base_ref_sha, f"{self.cfg.pr_remote}/{branch}"]
            )
            if rc_anc != 0:
                # Our recorded base sha is no longer reachable from origin —
                # a force-push rewrote history. Cannot safely auto-recover.
                msg = (
                    f"branch {branch} was force-pushed (history rewritten); "
                    f"the work in this container does not match what's on the remote. "
                    f"Use `quikode unblock {self.node.id}` to inspect, then "
                    f"`quikode retry {self.node.id}` to start fresh."
                )
                log.error("task %s: %s", self.node.id, msg)
                self.store.transition(
                    self.node.id,
                    State.BLOCKED,
                    note=msg[:300],
                    last_error=msg[:1000],
                )
                return WorkerOutcome(State.BLOCKED, "force-push detected on branch")

        # Diverged but not force-pushed. Try a `git rebase origin/<branch>`
        # to replay our local commits on top of the upstream changes.
        # On conflict, fall back to the existing conflict-resolver agent.
        log.info(
            "task %s: branch %s diverged (ahead=%d, behind=%d); attempting auto-rebase onto origin/%s",
            self.node.id,
            branch,
            ahead,
            behind,
            branch,
        )
        return self._rebase_diverged_branch(branch)

    def _rebase_diverged_branch(self, branch: str) -> WorkerOutcome | None:
        """Rebase the local branch onto the remote tip after an upstream
        push diverged it. Used by `_handle_branch_divergence_if_needed`
        when ahead>0 + behind>0 but no force-push was detected.

        Distinct from `_handle_parent_rebase_if_needed` (parent-merged onto
        main) and `_rebase_to_base_branch` (target=cfg.base_branch). Here
        we rebase our branch onto its OWN remote tip — the upstream commits
        came from the same branch.

        On clean rebase: returns None, caller continues.
        On conflict: invokes the conflict-resolver agent for up to N
        iterations. On success returns None; on failure BLOCKs the task.
        On any non-conflict rebase failure: BLOCKs.
        """
        # `core.editor=true` so `git rebase --continue` doesn't try to open
        # a TTY editor that doesn't exist in the container.
        rc, out = self._git_in_workspace(
            ["-c", "core.editor=true", "rebase", f"{self.cfg.pr_remote}/{branch}"]
        )
        if rc == 0:
            log.info("task %s: clean rebase onto origin/%s succeeded", self.node.id, branch)
            # Force-push so the remote reflects the rebased local commits.
            # Without this, the next regular push (e.g. from commit_subtask)
            # would fail non-fast-forward because our commit hashes changed
            # under the rebase.
            self._git_in_workspace(["push", "--force-with-lease", self.cfg.pr_remote, branch])
            return None
        # Non-zero rc: either conflict (rebase still in progress) or hard fail.
        if not self._rebase_in_progress():
            # Hard fail: rebase aborted itself (e.g. detached HEAD, missing
            # commits, ref invalid). Cannot safely continue.
            self.store.transition(
                self.node.id,
                State.BLOCKED,
                note=f"diverged-branch rebase failed (no rebase state dir): {out[:200]}",
                last_error=f"rebase {self.cfg.pr_remote}/{branch} failed: {out[:500]}",
            )
            return WorkerOutcome(State.BLOCKED, "diverged-branch rebase hard-failed")
        # Conflict — invoke the resolver agent (existing iterative flow).
        log.info(
            "task %s: rebase onto origin/%s hit conflicts; invoking resolver agent",
            self.node.id,
            branch,
        )
        outcome = self._spawn_conflict_resolver()
        # `_spawn_conflict_resolver` re-runs the final checker at the end.
        # For our case (mid-subtask divergence), that's overkill but harmless
        # — if the rebase didn't break anything, the check will pass and
        # the worker continues.
        if outcome and outcome.final_state == State.BLOCKED:
            return outcome
        # Force-push the rebased branch so the remote reflects the merge.
        # `--force-with-lease` is the safe form: refuses if remote was
        # advanced again between our fetch and our push.
        rc_p, push_out = self._git_in_workspace(["push", "--force-with-lease", self.cfg.pr_remote, branch])
        if rc_p != 0:
            log.warning(
                "task %s: force-with-lease push after rebase failed: %s",
                self.node.id,
                push_out[:300],
            )
            # Don't BLOCK — the rebase work is local; the next subtask's
            # commit/push will re-attempt. Worst case, ahead/behind delta
            # surfaces again next checkpoint and we re-try.
        return None

    def _handle_parent_rebase_if_needed(self) -> WorkerOutcome | None:
        """Worker-side checkpoint: if the orchestrator set
        `needs_parent_rebase=1` while we were mid-flight, rebase onto main
        (using --onto to skip the parent's now-squashed commits), retarget
        the PR if one exists, and clear the flag. Returns None on success
        or when the flag isn't set; returns BLOCKED WorkerOutcome on a
        failure that should abort the worker.

        Called at the top of every safe checkpoint in the worker FSM so
        a parent merge during a multi-minute doer/checker iteration is
        picked up at the next granular boundary.
        """
        row = self._row()
        if not row.get("needs_parent_rebase"):
            return None
        log.info(
            "task %s: needs_parent_rebase set; running inline rebase + retarget",
            self.node.id,
        )
        # The container must already exist for any checkpoint to fire — the
        # worker is past _provision. If somehow we're called pre-provision,
        # bail (the next checkpoint will retry).
        if self.handle is None:
            return None
        ok = self._rebase_to_base_branch()
        if not ok:
            self.store.transition(
                self.node.id,
                State.BLOCKED,
                note="needs_parent_rebase: inline rebase failed",
            )
            return WorkerOutcome(State.BLOCKED, "parent-merge rebase failed")
        # Retarget PR if one exists.
        row = self._row()
        pr_number = row.get("pr_number")
        if pr_number:
            self._retarget_pr_to_main(int(pr_number))
        # Clear stacking metadata + flag (clear_parent_branch zeroes the flag too).
        self.store.clear_parent_branch(self.node.id)
        return None

    def _rebase_to_base_branch(self) -> bool:
        """Rebase the current branch onto cfg.base_branch and force-push.

        When the row carries a parent linkage and the parent's local ref
        still resolves, uses `git rebase --onto <base> <parent_sha>` so
        the parent's commits (now squash-merged into base) are dropped
        from the replay. Falls back to a plain rebase when no parent
        context exists. Returns True on success; on conflict, invokes the
        conflict resolver and returns whatever it produced. Best-effort —
        caller decides what to do on failure.
        """
        row = self._row()
        branch = str(row["branch"])
        parent_branches = self.store.get_parent_branches(self.node.id)
        # 1. capture parent_sha before fetch (local ref, persists post-deletion).
        # Single-parent: use the parent's branch tip. Multi-parent: use the
        # stored merge-base sha (set by the picker / worker on provision).
        parent_sha = ""
        if len(parent_branches) == 1:
            rc_ps, ps_out = self._git_in_workspace(["rev-parse", "--verify", parent_branches[0]])
            if rc_ps == 0:
                parent_sha = ps_out.strip().splitlines()[-1] if ps_out.strip() else ""
        elif len(parent_branches) > 1:
            parent_sha = str(row.get("parent_merge_base_sha") or "")
        # 2. fetch base
        self._git_in_workspace(["fetch", self.cfg.pr_remote, self.cfg.base_branch])
        # 3. rebase: use --onto when we have a parent sha to skip
        if parent_sha:
            rc, _out = self._git_in_workspace(
                [
                    "-c",
                    "core.editor=true",
                    "rebase",
                    "--onto",
                    f"{self.cfg.pr_remote}/{self.cfg.base_branch}",
                    parent_sha,
                ]
            )
        else:
            rc, _out = self._git_in_workspace(
                ["-c", "core.editor=true", "rebase", f"{self.cfg.pr_remote}/{self.cfg.base_branch}"]
            )
        if rc != 0:
            # Conflict — try the resolver
            resolver_outcome = self._spawn_conflict_resolver()
            if resolver_outcome and resolver_outcome.final_state == State.BLOCKED:
                return False
        # Defensive: refuse to push an empty branch (see run_rebase_to_main).
        ahead = self._git_ahead_count(branch)
        if ahead == 0:
            row_now = self._row()
            worktree_path = row_now.get("worktree_path") or ""
            note = (
                f"post-rebase branch is 0 commits ahead of {self.cfg.base_branch} — "
                f"the rebase likely dropped task changes. Inspect the worktree at "
                f"{worktree_path} before retrying."
            )
            self.store.transition(
                self.node.id,
                State.BLOCKED,
                note=note,
                last_error=note[:1000],
            )
            return False
        # 4. force-push
        push_rc, _push_out = self._git_in_workspace(
            ["push", "--force-with-lease", self.cfg.pr_remote, branch]
        )
        return push_rc == 0

    def _pr_body(self) -> str:
        return (
            f"## Task\n\n"
            f"**{self.node.id}** — {self.node.title}\n\n"
            f"## Scope\n\n{self.node.scope}\n\n"
            f"## Plan\n\n{self.plan_text}\n\n"
            f"---\n\n"
            f"_Generated by quikode. Acceptance criteria above were verified by the automated checker before PR open._\n"
        )

    def _poll_pr_loop(self) -> WorkerOutcome:
        self.store.transition(self.node.id, State.POLLING_CI)
        budget = self.cfg.triage_budget_per_phase
        ci_attempts = 0
        while True:
            time.sleep(20)
            row = self._row()
            if row.get("state") in (State.ABORTED.value, State.MERGED.value):
                return WorkerOutcome(State(row["state"]))

            # v3 stacked-diffs fix: parent may merge mid-poll. Rebase + retarget
            # before re-querying PR status so the next poll reflects the new base.
            rebase_outcome = self._handle_parent_rebase_if_needed()
            if rebase_outcome:
                return rebase_outcome
            row = self._row()

            # v2 Phase B: orchestrator may have flagged us for intent review
            # after another task merged. Check at this safe point.
            if row.get("needs_intent_review"):
                intent_outcome = self._run_intent_review()
                if intent_outcome:
                    return intent_outcome
                # Re-read row after the review action
                row = self._row()

            status = github.poll_pr(self.cfg.repo_path, int(row["pr_number"]) if row.get("pr_number") else 0)
            if status.state == "MERGED":
                self.store.transition(self.node.id, State.MERGED)
                return WorkerOutcome(State.MERGED)
            if status.state == "CLOSED":
                # Same Bug-6 race protection as orchestrator._poll_review_threads:
                # github auto-closes a stacked child's PR when its base branch
                # is deleted on parent merge. That's not a real abort — it's
                # github cleanup. Detect it by checking if the PR's base ref
                # is gone from the remote, then trigger rebase-to-main.
                row_now = self._row()
                parent_branch_field = row_now.get("parent_pr_branch") or row_now.get("parent_branch")
                pr_base_ref = status.base_ref_name or ""
                if (
                    parent_branch_field
                    and pr_base_ref
                    and pr_base_ref != self.cfg.base_branch
                    and not self._remote_branch_exists(pr_base_ref)
                ):
                    log.info(
                        "task %s: PR #%s auto-closed — base %s deleted by parent merge; "
                        "rebasing onto main + creating fresh PR via run_rebase_to_main",
                        self.node.id,
                        row_now.get("pr_number"),
                        pr_base_ref,
                    )
                    return self.run_rebase_to_main()
                self.store.transition(self.node.id, State.ABORTED, note="PR was closed")
                return WorkerOutcome(State.ABORTED)

            # v2 Phase A: if PR is conflicting with main, rebase and (if needed)
            # spawn conflict resolver. This runs ahead of CI/review checks so a
            # rebased branch's CI re-runs with the right base.
            if (
                status.mergeable == "CONFLICTING"
                and self.cfg.conflict_auto_resolve
                and (row.get("conflict_resolve_retries") or 0) < self.cfg.conflict_max_resolve_attempts
            ):
                rebase_outcome = self._rebase_or_resolve()
                if rebase_outcome:
                    return rebase_outcome
                # Re-enter polling with fresh PR state
                self.store.transition(self.node.id, State.POLLING_CI, note="back to polling after rebase")
                continue

            if status.checks_status == "failure" and ci_attempts < budget:
                ci_attempts += 1
                ci_log = github.fetch_failed_check_logs(
                    self.cfg.repo_path, int(row["pr_number"]) if row.get("pr_number") else 0
                )
                ci_excerpt = _last_lines(ci_log, 80)
                # v3 fixup decomposition: instead of a monolithic
                # `_do(attempt=200+i)` covering the full CI failure (which
                # at tanren scale routinely runs 1-2h with shaky
                # convergence on glm-5.1), invoke the fixup planner to
                # break the fix into 1-5 mini-subtasks. Each lands as its
                # own commit on the PR branch; the per-subtask commit
                # gate runs the pre-commit hook + push for each. After
                # all settle, polling continues — GitHub re-runs CI on
                # the new commits naturally.
                outcome = self._run_fixup_round(
                    kind="fixup-ci",
                    round_no=ci_attempts,
                    trigger="ci",
                    ci_excerpt=ci_excerpt,
                )
                self.store.increment(self.node.id, "ci_triage_retries")
                if outcome and outcome.final_state == State.BLOCKED:
                    return outcome
                # The per-subtask commit gate already pushed the slices
                # individually. We don't need a final commit/push here.
                continue

            # v3 Phase B: review-thread polling lives on the daemon now
            # (orchestrator._poll_review_threads). When CI is green and the
            # PR is mergeable this loop exits to AWAITING_MERGE and the
            # worker tears down — the daemon takes over polling for both
            # MERGED detection and unresolved review threads.

            if ci_attempts >= budget:
                self.store.transition(self.node.id, State.BLOCKED, note="exhausted PR triage budget")
                return WorkerOutcome(State.BLOCKED)

            # "none" = repo has no CI configured at all (e.g. the fixture repo).
            # Treat that as passing — there's nothing to wait for.
            if status.checks_status in ("success", "none") and status.mergeable != "CONFLICTING":
                self.store.transition(self.node.id, State.PENDING_CI, note="green; awaiting merge")
                sound.ding()
                return WorkerOutcome(State.PENDING_CI)

    # ----- intent gap detection -----

    def _run_intent_review(self) -> WorkerOutcome | None:
        """A dependency merged. Check if main has shifted under us in a way
        that breaks intent. Per verdict: continue / rebase / block."""
        row = self._row()
        budget = self.cfg.intent_max_reviews_per_task
        if (row.get("intent_review_count") or 0) >= budget:
            log.warning("task %s exhausted intent review budget; clearing flag and proceeding", self.node.id)
            self.store.clear_intent_review_flag(self.node.id)
            return None

        self.store.transition(self.node.id, State.INTENT_REVIEWING, note="dep merged; checking intent gap")
        # Refresh main + compute diffs since last sync
        self._git_in_workspace(["fetch", self.cfg.pr_remote, self.cfg.base_branch])
        base = row.get("last_synced_main_sha") or row.get("base_ref_sha")
        if not base:
            self.store.clear_intent_review_flag(self.node.id)
            self.store.transition(self.node.id, State.POLLING_CI, note="no base sha; skipping intent review")
            return None
        _, current_main = self._git_in_workspace(
            ["rev-parse", f"{self.cfg.pr_remote}/{self.cfg.base_branch}"]
        )
        current_main = current_main.strip()
        if current_main == base:
            # No new commits to review against
            self.store.clear_intent_review_flag(self.node.id)
            self.store.transition(self.node.id, State.POLLING_CI, note="main unchanged; no review needed")
            return None

        _, task_diff = self._git_in_workspace(["diff", f"{base}...HEAD", "--no-color"])
        _, main_log = self._git_in_workspace(["log", "--oneline", f"{base}..{current_main}"])
        _, main_diff = self._git_in_workspace(["diff", f"{base}..{current_main}", "--no-color"])

        agent = build_agent(self.cfg.intent_reviewer)
        prompt = prompts.intent_reviewer_prompt(
            self.cfg,
            self.node,
            task_diff_excerpt=task_diff,
            main_log_excerpt=main_log,
            main_diff_excerpt=main_diff,
        )
        self._write_log_header("INTENT REVIEW", prompt)
        result = agent.run(prompt, handle=self._h, log_path=self.log_path, timeout=600)
        self.store.record_agent_call(
            self.node.id,
            phase="intent_reviewer",
            cli=self.cfg.intent_reviewer.cli,
            model=self.cfg.intent_reviewer.model,
            rc=result.rc,
            duration_s=result.duration_s or 0,
            tokens_used=result.tokens_used,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            tokens_cached_read=result.tokens_cached_read,
            tokens_cached_creation=result.tokens_cached_creation,
            cost_usd=result.cost_usd,
        )
        outcome = _parse_intent_verdict(result.stdout)
        self.store.record_intent_review(
            self.node.id,
            triggered_by_merge_of=None,
            main_sha_before=base,
            main_sha_after=current_main,
            verdict=outcome.verdict.value,
            explanation=outcome.explanation,
            affected_areas=outcome.affected_areas,
            raw_output=result.stdout,
        )
        self.store.clear_intent_review_flag(self.node.id)
        self.store.set_field(self.node.id, last_synced_main_sha=current_main)

        if outcome.verdict is IntentVerdict.NO_DRIFT:
            self.store.transition(self.node.id, State.POLLING_CI, note="intent review: NO_DRIFT")
            return None

        if outcome.verdict is IntentVerdict.MINOR_DRIFT:
            log.info("task %s intent review: MINOR_DRIFT — %s", self.node.id, outcome.explanation[:200])
            return self._rebase_or_resolve()

        # INTENT_CONFLICT — try Phase B.5 automatic replan if budget allows.
        log.warning("task %s intent review: INTENT_CONFLICT — %s", self.node.id, outcome.explanation[:200])
        replan_count = (self.store.get(self.node.id) or {}).get("replan_count") or 0
        if replan_count >= self.cfg.intent_max_replans:
            self.store.transition(
                self.node.id,
                State.BLOCKED,
                note=f"intent conflict + replan budget exhausted: {outcome.explanation[:300]}",
                last_error=f"INTENT_CONFLICT: {outcome.explanation[:500]}",
            )
            return WorkerOutcome(State.BLOCKED, "intent conflict; replan budget exhausted")
        return self._replan_and_resume(outcome.explanation, outcome.affected_areas)

    def _replan_and_resume(self, prior_explanation: str, affected: str) -> WorkerOutcome | None:
        """Phase B.5: re-run the planner with augmented context (prior plan +
        what landed on main), update subtasks, then restart the subtask loop
        from any pending/blocked subtasks."""
        self.store.transition(
            self.node.id, State.REPLANNING, note=f"replan triggered by intent conflict: {affected[:120]}"
        )
        agent = build_agent(self.cfg.planner)
        prompt = prompts.planner_prompt(self.cfg, self.dag, self.node)
        # Augment with replan context
        existing_subtasks = self.store.list_subtasks(self.node.id)
        done_summary = "\n".join(
            f"- {s['subtask_id']} ({s['state']}): {s.get('title') or ''}" for s in existing_subtasks
        )
        prompt += (
            "\n\n## REPLAN CONTEXT\n\n"
            "An earlier plan was emitted and partially executed; the world has shifted "
            f"on main since then. The intent reviewer flagged: **{prior_explanation}**\n\n"
            "### Previously emitted subtasks\n"
            f"{done_summary}\n\n"
            "Re-emit a structured plan that:\n"
            "1. **Preserves DONE subtasks** — list them again with their existing IDs and acceptance, so we don't redo them.\n"
            "2. **Replaces or removes** subtasks that are no longer needed because main supplied them.\n"
            "3. **Adds new subtasks** for anything the world-shift now requires (e.g., main added a new instance of a pattern; this task must apply the new pattern there too).\n\n"
            "Use the same JSON schema as before. Return only the fenced ```json``` block."
        )
        self._write_log_header("PLANNER (replan)", prompt)
        result = agent.run(prompt, handle=self._h, log_path=self.log_path, timeout=1800)
        self.store.record_agent_call(
            self.node.id,
            phase="planner_replan",
            cli=self.cfg.planner.cli,
            model=self.cfg.planner.model,
            rc=result.rc,
            duration_s=result.duration_s or 0,
            tokens_used=result.tokens_used,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            tokens_cached_read=result.tokens_cached_read,
            tokens_cached_creation=result.tokens_cached_creation,
            cost_usd=result.cost_usd,
        )
        if not result.ok:
            self.store.transition(
                self.node.id,
                State.BLOCKED,
                note=f"replan agent exited {result.rc}: {result.stderr[:300]}",
            )
            return WorkerOutcome(State.BLOCKED, "replan failed")
        self.plan_text = result.stdout
        self.store.add_artifact(self.node.id, "planner_replan_output", result.stdout)
        try:
            new_plan = self._parse_or_retry_plan(result.stdout)
        except Exception as e:
            self.store.transition(self.node.id, State.BLOCKED, note=f"replan parse failed: {e}")
            return WorkerOutcome(State.BLOCKED, "replan output unparseable")

        # Persist new subtasks; merge state from prior DONE subtasks where ids match.
        prior_done = {s["subtask_id"]: s for s in existing_subtasks if s["state"] == SubtaskState.DONE.value}
        new_rows = []
        for s in new_plan.subtasks:
            row = {
                "subtask_id": s.id,
                "title": s.title,
                "depends_on": list(s.depends_on),
                "files_to_touch": list(s.files_to_touch),
                "boundary": s.boundary,
                "acceptance": list(s.acceptance),
                "notes": s.notes,
            }
            new_rows.append(row)
        self.store.upsert_subtasks(self.node.id, new_rows)
        # Restore DONE state on subtasks whose ids carried over from the old plan
        for sid in prior_done:
            if sid in {s.id for s in new_plan.subtasks}:
                self.store.update_subtask(self.node.id, sid, state=SubtaskState.DONE.value)
        self.plan = new_plan
        # bump replan counter
        with self.store.tx() as c:
            c.execute(
                "UPDATE tasks SET replan_count = COALESCE(replan_count, 0) + 1, updated_at = ? WHERE id = ?",
                (time.time(), self.node.id),
            )
        log.info(
            "task %s replanned: %d subtasks (%d carried-over DONE)",
            self.node.id,
            len(new_plan.subtasks),
            len(prior_done),
        )
        # Resume the subtask loop — it skips DONE subtasks implicitly and
        # drives any remaining ones through the same per-subtask flow as
        # the initial run, including progress-check + retry classification.
        outcome = self._subtask_loop()
        if outcome:
            return outcome
        # Resumed fine — fall back into the polling loop.
        self.store.transition(self.node.id, State.POLLING_CI, note="post-replan; back to polling")
        return None

    # ----- rebase + conflict resolution -----

    def _rebase_or_resolve(self) -> WorkerOutcome | None:
        """Rebase the current task's branch onto fresh main. On clean rebase,
        force-push and return None (caller resumes polling). On conflict,
        spawn the resolver agent, verify with checker, push if pass.
        Returns a WorkerOutcome only on terminal failure (BLOCKED)."""
        self.store.transition(self.node.id, State.REBASING)
        rc, out = self._git_in_workspace(["fetch", self.cfg.pr_remote, self.cfg.base_branch])
        if rc != 0:
            self.store.transition(self.node.id, State.BLOCKED, note=f"git fetch failed: {out[:300]}")
            return WorkerOutcome(State.BLOCKED, "git fetch failed before rebase")

        rc, out = self._git_in_workspace(["rebase", f"{self.cfg.pr_remote}/{self.cfg.base_branch}"])
        if rc == 0:
            # Clean rebase. Force-push.
            self.store.set_field(
                self.node.id,
                last_synced_main_sha=self._git_in_workspace(
                    ["rev-parse", f"{self.cfg.pr_remote}/{self.cfg.base_branch}"]
                )[1].strip()
                or None,
            )
            # Defensive: refuse to push an empty branch. A clean rebase that
            # results in 0 commits ahead of base means our branch had no
            # exclusive work — pushing would land an empty PR.
            branch_str = str(self._row()["branch"])
            ahead = self._git_ahead_count(branch_str)
            if ahead == 0:
                row_now = self._row()
                worktree_path = row_now.get("worktree_path") or ""
                note = (
                    f"post-rebase branch is 0 commits ahead of {self.cfg.base_branch} — "
                    f"the rebase likely dropped task changes. Inspect the worktree at "
                    f"{worktree_path} before retrying."
                )
                self.store.transition(
                    self.node.id,
                    State.BLOCKED,
                    note=note,
                    last_error=note[:1000],
                )
                return WorkerOutcome(State.BLOCKED, "post-rebase empty branch")
            push_rc, _push_out = github.push(
                self._h,
                str(self._row()["branch"]),
                remote=self.cfg.pr_remote,
                log_path=self.log_path,
            )
            if push_rc != 0:
                # Try with --force-with-lease since we rebased
                rc2, out2 = self._git_in_workspace(
                    [
                        "push",
                        "--force-with-lease",
                        "-u",
                        self.cfg.pr_remote,
                        str(self._row()["branch"]),
                    ]
                )
                if rc2 != 0:
                    self.store.transition(
                        self.node.id,
                        State.BLOCKED,
                        note=f"force-push after rebase failed: {out2[:300]}",
                    )
                    return WorkerOutcome(State.BLOCKED, "rebase push failed")
            return None  # caller resumes polling

        # Rebase conflicted. Spawn resolver.
        self.store.increment(self.node.id, "conflict_resolve_retries")
        return self._spawn_conflict_resolver()

    def _spawn_conflict_resolver(self) -> WorkerOutcome | None:
        """Resolve all conflicts encountered during the in-progress rebase.

        A single rebase may trip multiple conflicts (one per commit being
        replayed). After the agent fixes one and we run `rebase --continue`,
        git applies the next commit and may surface another. We loop until
        either the rebase completes (no `.git/rebase-merge/` or
        `rebase-apply/` directory) or we hit `_MAX_CONFLICT_ITERATIONS` /
        the resolver gives up.
        """
        self.store.transition(self.node.id, State.CONFLICT_RESOLVING)
        max_iterations = 6
        for iteration in range(1, max_iterations + 1):
            outcome = self._resolve_one_conflict_step(iteration=iteration)
            if outcome is not None:
                # Resolver gave up, no conflicted files surfaced, or
                # `rebase --continue` failed in a non-conflict way.
                return outcome
            # If git's rebase state dir is gone, we're done.
            if not self._rebase_in_progress():
                break
        else:
            # Hit the iteration cap without completing
            self._git_in_workspace(["rebase", "--abort"])
            self._ensure_on_branch()
            self.store.transition(
                self.node.id,
                State.BLOCKED,
                note=f"conflict resolver exceeded {max_iterations} iterations; aborting",
            )
            return WorkerOutcome(State.BLOCKED, "conflict iteration cap")
        # ↳ for-loop completed without break OR via break = we're past it.

        # Verify by re-running `just ci` inside the dev container. Don't
        # re-invoke an agent verifier — the audit gauntlet downstream will
        # do the rubric/standards/behavior pass; here we just need an
        # objective "did the resolver break the build" signal.
        rc, out, err = exec_in(
            self._h,
            ["bash", "-lc", "cd /workspace && just ci 2>&1"],
            log_path=self.log_path,
            timeout=1800,
        )
        if rc != 0:
            ci_log = (out or "") + "\n" + (err or "")
            self.store.add_artifact(self.node.id, "post_rebase_ci_log", ci_log)
            self.store.transition(
                self.node.id,
                State.BLOCKED,
                note="post-rebase `just ci` FAILed; conflict resolution broke build",
                last_error=_last_lines(ci_log, 30)[:1000],
            )
            return WorkerOutcome(State.BLOCKED, "rebase verify failed")

        # Defensive: a conflict resolver that accepts main's version of every
        # conflict can produce an empty branch (0 commits ahead of base).
        # Refuse to force-push that — github would auto-close the resulting
        # empty PR. BLOCK with a clear note so a human can inspect.
        branch_str = str(self._row()["branch"])
        ahead = self._git_ahead_count(branch_str)
        if ahead == 0:
            row_now = self._row()
            worktree_path = row_now.get("worktree_path") or ""
            note = (
                f"post-rebase branch is 0 commits ahead of {self.cfg.base_branch} — "
                f"the rebase likely dropped task changes. Inspect the worktree at "
                f"{worktree_path} before retrying."
            )
            self.store.transition(
                self.node.id,
                State.BLOCKED,
                note=note,
                last_error=note[:1000],
            )
            return WorkerOutcome(State.BLOCKED, "post-rebase empty branch")
        # Force-push the rebased branch
        rc, out = self._git_in_workspace(
            [
                "push",
                "--force-with-lease",
                "-u",
                self.cfg.pr_remote,
                str(self._row()["branch"]),
            ]
        )
        if rc != 0:
            self.store.transition(
                self.node.id,
                State.BLOCKED,
                note=f"force-push after resolution failed: {out[:200]}",
            )
            return WorkerOutcome(State.BLOCKED, "rebase resolved but push failed")
        return None  # caller resumes polling

    def _resolve_one_conflict_step(self, *, iteration: int) -> WorkerOutcome | None:
        """Run the conflict-resolver agent once on the currently-conflicted
        files, then `git rebase --continue`. Returns None on success (caller
        loops to check whether the rebase is fully done), or a WorkerOutcome
        on a terminal failure (resolver gave up, no conflicts surfaced, etc).
        """
        row = self._row()
        base_sha = row.get("base_ref_sha") or "HEAD~1"
        _, task_diff = self._git_in_workspace(["diff", f"{base_sha}...HEAD", "--no-color"])
        _, main_log = self._git_in_workspace(
            ["log", "--oneline", f"{base_sha}..{self.cfg.pr_remote}/{self.cfg.base_branch}"]
        )
        _, main_diff = self._git_in_workspace(
            ["diff", f"{base_sha}..{self.cfg.pr_remote}/{self.cfg.base_branch}", "--no-color"]
        )
        _, status_out = self._git_in_workspace(["diff", "--name-only", "--diff-filter=U"])
        conflicted: list[dict] = []
        for path in status_out.splitlines():
            path = path.strip()
            if not path:
                continue
            _rc, marked, _err = exec_in(
                self._h,
                ["bash", "-lc", f"cat /workspace/{path}"],
                log_path=self.log_path,
                timeout=30,
            )
            conflicted.append({"path": path, "content": marked[:3000]})
        if not conflicted:
            # Mid-rebase but git surfaced no UD files. Common when the
            # previous --continue advanced past one commit cleanly and the
            # state dir is between commits. Try a no-agent --continue first
            # before bailing.
            rc_cont, out_cont = self._git_in_workspace(["-c", "core.editor=true", "rebase", "--continue"])
            if rc_cont == 0:
                # Caller's loop will re-check _rebase_in_progress.
                return None
            # Still failing and no files to hand to the agent — abort.
            self._git_in_workspace(["rebase", "--abort"])
            self._ensure_on_branch()
            self.store.transition(
                self.node.id,
                State.BLOCKED,
                note=f"rebase iter {iteration} failed; no conflicts surfaced and --continue rc={rc_cont}: {out_cont[:200]}",
            )
            return WorkerOutcome(State.BLOCKED, "rebase abort")

        agent = build_agent(self.cfg.conflict_resolver)
        prompt = prompts.conflict_resolver_prompt(
            self.cfg,
            self.node,
            task_diff_excerpt=task_diff,
            main_log_excerpt=main_log,
            main_diff_excerpt=main_diff,
            conflicted_files=conflicted,
        )
        self._write_log_header(f"CONFLICT RESOLVER (iter {iteration})", prompt)
        result = agent.run(prompt, handle=self._h, log_path=self.log_path, timeout=1800)
        self.store.record_agent_call(
            self.node.id,
            phase="conflict_resolver",
            cli=self.cfg.conflict_resolver.cli,
            model=self.cfg.conflict_resolver.model,
            rc=result.rc,
            duration_s=result.duration_s or 0,
            tokens_used=result.tokens_used,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            tokens_cached_read=result.tokens_cached_read,
            tokens_cached_creation=result.tokens_cached_creation,
            cost_usd=result.cost_usd,
        )
        self.store.add_artifact(self.node.id, f"conflict_resolver_output_iter{iteration}", result.stdout)

        if "GIVE_UP:" in result.stdout:
            self._git_in_workspace(["rebase", "--abort"])
            self.store.transition(
                self.node.id,
                State.BLOCKED,
                note=f"conflict resolver gave up at iter {iteration}; needs human resolution",
            )
            return WorkerOutcome(State.BLOCKED, "conflict resolver gave up")

        # Stage + continue. core.editor=true skips the editor prompt for
        # the resolved-commit message (containers have no TTY).
        self._git_in_workspace(["add", "-A"])
        rc, out = self._git_in_workspace(["-c", "core.editor=true", "rebase", "--continue"])
        # rc != 0 here means git hit a NEW conflict on the next commit being
        # replayed (or some other failure). The caller's loop will detect
        # `_rebase_in_progress() == True` and re-enter this method. If the
        # rebase has truly gone off the rails (no rebase state dir but
        # rc != 0), the loop will fall through to the post-resolver checker
        # path, which will FAIL the verdict and block.
        if rc != 0 and not self._rebase_in_progress():
            self._git_in_workspace(["rebase", "--abort"])
            self.store.transition(
                self.node.id,
                State.BLOCKED,
                note=f"rebase --continue at iter {iteration} failed: {out[:200]}",
            )
            return WorkerOutcome(State.BLOCKED, "rebase --continue failed")
        return None  # caller checks _rebase_in_progress to decide loop continuation

    def _rebase_in_progress(self) -> bool:
        """True if git is mid-rebase (either merge-style or apply-style).

        Uses `git rev-parse --git-path` to resolve the per-rebase state
        directory, then `test -d` to check for its presence. More reliable
        than checking `REBASE_HEAD`, which only exists during specific
        rebase phases (e.g. merge-style mid-conflict but not apply-style
        mid-3-way).
        """
        for kind in ("rebase-merge", "rebase-apply"):
            rc, out = self._git_in_workspace(["rev-parse", "--git-path", kind])
            if rc != 0:
                continue
            path = (out.strip().splitlines() or [""])[-1].strip()
            if not path:
                continue
            # Use exec_in directly so we can `test -d` without going through
            # _git_in_workspace's `git` prefix.
            rc2, _out, _err = exec_in(
                self._h,
                ["bash", "-lc", f"cd /workspace && test -d {shlex.quote(path)}"],
                log_path=self.log_path,
                timeout=15,
            )
            if rc2 == 0:
                return True
        return False

    def _ensure_on_branch(self, branch: str | None = None) -> None:
        """If HEAD is detached, point it back at the worker's branch.

        Some failure paths (rebase --abort after an iteration cap, an
        orphaned worker resumed after SIGTERM) can leave the worktree on
        detached HEAD. `gh pr create` and `git push -u` choke on that.
        This is a defensive cleanup; safe to call when already on a
        branch — `symbolic-ref` then no-ops at the OS level.
        """
        rc, _out = self._git_in_workspace(["symbolic-ref", "--short", "-q", "HEAD"])
        if rc == 0:
            return  # already on a branch
        if branch is None:
            row = self.store.get(self.node.id) or {}
            branch = str(row.get("branch") or "")
        if not branch:
            return
        self._git_in_workspace(["symbolic-ref", "HEAD", f"refs/heads/{branch}"])

    def _git_in_workspace(self, args: list[str]) -> tuple[int, str]:
        """Run a git command in the dev container's /workspace."""
        rc, out, err = exec_in(
            self._h,
            ["bash", "-lc", "cd /workspace && git " + " ".join(args)],
            log_path=self.log_path,
            timeout=300,
        )
        return rc, (out + err)

    def _git_ahead_count(self, branch: str, base: str | None = None) -> int:
        """How many commits is `branch` ahead of `<remote>/<base>`?

        Returns 0 when the count is genuinely zero AND when the rev-list
        invocation fails (missing refs, parse errors). Defensive callers
        treat 0 as "rebase produced an empty branch" and BLOCK; transient
        rev-list failures will be re-evaluated on the next attempt.
        """
        base_branch = base or self.cfg.base_branch
        rc, out = self._git_in_workspace(
            ["rev-list", "--count", f"{self.cfg.pr_remote}/{base_branch}..{branch}"]
        )
        if rc != 0:
            return 0
        try:
            return int(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return 0

    # ----- helpers -----

    def _teardown(self) -> None:
        if self.handle is not None:
            docker_env.teardown(self._h)
        wt = self.store.get(self.node.id)
        if wt and wt.get("worktree_path"):
            row = self._row()
            # Keep the worktree if the user might want to inspect — that's
            # AWAITING_MERGE (success path waiting on review/merge), BLOCKED (we gave
            # up but the work-in-progress may be salvageable), and FAILED (a
            # crash mid-flight; the partial state is debugging gold). Clean it
            # only on MERGED (work has been merged upstream so the diff is
            # captured there) and ABORTED (user cancelled).
            cleanup_states = (State.MERGED.value, State.ABORTED.value)
            if row["state"] in cleanup_states:
                worktree.remove_worktree(self.cfg.repo_path, Path(row["worktree_path"]), force=True)

    def _write_log_header(self, phase: str, prompt: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(f"\n\n========== {phase} @ {time.strftime('%Y-%m-%d %H:%M:%S')} ==========\n")
            f.write("--- PROMPT ---\n")
            f.write(prompt)
            f.write("\n--- RESPONSE ---\n")


def _parse_verdict(checker_text: str) -> Verdict:
    """Pull `VERDICT: PASS|FAIL` from a checker agent's output. Defaults to
    FAIL when the line is missing — better to retry than to merge under a
    parse failure."""
    m = re.search(r"VERDICT:\s*(PASS|FAIL)", checker_text, re.IGNORECASE)
    if not m:
        return Verdict.FAIL
    return Verdict.PASS if m.group(1).upper() == "PASS" else Verdict.FAIL


def _parse_intent_verdict(text: str) -> IntentReviewOutcome:
    """Pull the intent-reviewer's structured output. Defaults to NO_DRIFT on
    parse failure since that's the safe no-op choice."""
    m = re.search(r"VERDICT:\s*(NO_DRIFT|MINOR_DRIFT|INTENT_CONFLICT)", text, re.IGNORECASE)
    verdict = IntentVerdict(m.group(1).upper()) if m else IntentVerdict.NO_DRIFT
    am = re.search(r"AFFECTED_AREAS:\s*(.+)", text, re.IGNORECASE)
    affected = am.group(1).strip() if am else ""
    em = re.search(r"EXPLANATION:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    explanation = em.group(1).strip()[:1000] if em else ""
    return IntentReviewOutcome(
        verdict=verdict,
        affected_areas=affected,
        explanation=explanation,
    )


def _last_lines(s: str, n: int) -> str:
    lines = s.splitlines()
    return "\n".join(lines[-n:])


def _extract_root_cause(checker_output: str) -> str:
    """Pull the `ROOT_CAUSE:` line(s) out of a checker / triage agent
    output. Falls back to the first ~400 chars when no marker is present
    so the progress agent still sees *something* useful.

    The subtask-checker prompt is asked to emit `VERDICT: ...` and a
    `ROOT_CAUSE: ...` block — same convention as the whole-spec checker.
    """
    if not checker_output:
        return ""
    m = re.search(r"ROOT_CAUSE:\s*(.+?)(?:\n[A-Z_]+:|\Z)", checker_output, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()[:600]
    return checker_output.strip()[:400]
