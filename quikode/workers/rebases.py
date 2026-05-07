"""Rebases worker mixin."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from quikode import fsm_runtime
from quikode.state import State
from quikode.workers.outcomes import WorkerOutcome
from quikode.workers.rebase_branch import RebaseBranchMixin
from quikode.workers.rebase_conflicts import RebaseConflictMixin


class _TaskWorkerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.workers.task_worker"], name)


_tw = _TaskWorkerGlobals()


@dataclass
class _RebasePlan:
    pre_state: State
    onto_sha: str
    rebase_target: str


class RebaseWorkerMixin(RebaseBranchMixin, RebaseConflictMixin):
    def run_rebase_to_main(self: Any) -> WorkerOutcome:
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
        pre_state = self._pre_rebase_state(row)

        try:
            self._provision(provision_worktree=False)
            plan = self._prepare_rebase_plan(row, pre_state)
            if isinstance(plan, WorkerOutcome):
                return plan
            fetch_outcome = self._fetch_base_for_rebase(pre_state)
            if fetch_outcome is not None:
                return fetch_outcome
            push_done = self._run_rebase_plan(plan)
            if isinstance(push_done, WorkerOutcome):
                return push_done
            push_outcome = self._push_rebased_branch(row, push_done)
            if push_outcome is not None:
                return push_outcome
            return self._finish_rebase_to_main(row, pre_state)
        except Exception as e:
            _tw.log.exception("rebase-to-main for task %s crashed", self.node.id)
            fsm_runtime.enter_pending_ci(
                self.store,
                self.node.id,
                note=f"rebase-to-main crashed: {e}; restoring {pre_state.value}",
                last_error=str(e)[:1000],
            )
            return WorkerOutcome(pre_state, f"rebase-to-main crashed: {e}")
        finally:
            # Tear down the container only — keep the _tw.worktree.
            if self.handle is not None:
                self.execution_backend.teardown(self._h)
                self.handle = None

    def _pre_rebase_state(self: Any, row: dict[str, Any]) -> State:
        pre_state_str = self.store.get_pre_rebase_state(self.node.id) or row.get("state") or ""
        try:
            pre_state = State(pre_state_str)
        except ValueError:
            return State.PENDING_CI
        return State.PENDING_CI if pre_state is State.REBASING_TO_MAIN else pre_state

    def _prepare_rebase_plan(self: Any, row: dict[str, Any], pre_state: State) -> _RebasePlan | WorkerOutcome:
        parent_branches = self.store.get_parent_branches(self.node.id)
        onto_sha = self._verified_prior_merge_base(row)
        new_merge_base_sha = ""
        if parent_branches:
            for parent_branch in parent_branches:
                self._git_in_workspace(["fetch", self.cfg.pr_remote, parent_branch])
            parent_result = self._parent_rebase_boundary(parent_branches, onto_sha)
            if isinstance(parent_result, WorkerOutcome):
                return parent_result
            onto_sha, new_merge_base_sha = parent_result
        rebase_target = new_merge_base_sha or f"{self.cfg.pr_remote}/{self.cfg.base_branch}"
        return _RebasePlan(pre_state=pre_state, onto_sha=onto_sha, rebase_target=rebase_target)

    def _verified_prior_merge_base(self: Any, row: dict[str, Any]) -> str:
        prior_merge_base_sha = str(row.get("parent_merge_base_sha") or "")
        if not prior_merge_base_sha:
            return ""
        rc_ps, _ = self._git_in_workspace(["rev-parse", "--verify", prior_merge_base_sha])
        return prior_merge_base_sha if rc_ps == 0 else ""

    def _parent_rebase_boundary(
        self: Any, parent_branches: list[str], onto_sha: str
    ) -> tuple[str, str] | WorkerOutcome:
        if len(parent_branches) > 1:
            return self._multi_parent_rebase_boundary(parent_branches, onto_sha)
        if onto_sha:
            return onto_sha, ""
        rc_ps, ps_out = self._git_in_workspace(["rev-parse", "--verify", parent_branches[0]])
        single_parent_sha = ps_out.strip().splitlines()[-1] if rc_ps == 0 and ps_out.strip() else ""
        return single_parent_sha, ""

    def _multi_parent_rebase_boundary(
        self: Any, parent_branches: list[str], onto_sha: str
    ) -> tuple[str, str] | WorkerOutcome:
        mb_name = _tw.stacking.compute_merge_base_branch_name(self.node.id, parent_branches)
        mb_sha = _tw.stacking.construct_merge_base(
            repo_path=self.cfg.repo_path,
            parent_branches=parent_branches,
            branch_name=mb_name,
            base_branch=self.cfg.base_branch,
        )
        if mb_sha:
            self.store.set_parent_merge_base(self.node.id, branch=mb_name, sha=mb_sha)
            return onto_sha, mb_sha
        note = f"multi-parent merge-base recompute failed for {parent_branches}; cannot rebase"
        fsm_runtime.block_current(self.store, self.node.id, note=note, last_error=note[:1000])
        return WorkerOutcome(State.BLOCKED, note)

    def _fetch_base_for_rebase(self: Any, pre_state: State) -> WorkerOutcome | None:
        rc, fetch_out = self._git_in_workspace(["fetch", self.cfg.pr_remote, self.cfg.base_branch])
        if rc == 0:
            return None
        fsm_runtime.enter_pending_ci(
            self.store,
            self.node.id,
            note=f"rebase-to-main: fetch failed ({fetch_out[:200]}); restoring {pre_state.value}",
            last_error=f"rebase fetch: {fetch_out[:500]}",
        )
        return WorkerOutcome(pre_state, "rebase fetch failed")

    def _run_rebase_plan(self: Any, plan: _RebasePlan) -> bool | WorkerOutcome:
        if plan.onto_sha:
            rc, _rebase_out = self._git_in_workspace(
                ["-c", "core.editor=true", "rebase", "--onto", plan.rebase_target, plan.onto_sha]
            )
        else:
            rc, _rebase_out = self._git_in_workspace(["-c", "core.editor=true", "rebase", plan.rebase_target])
        if rc == 0:
            return False
        resolver_outcome = self._spawn_conflict_resolver()
        if resolver_outcome is None:
            return True
        _tw.log.warning("rebase-to-main: conflict resolver gave up for task %s", self.node.id)
        return resolver_outcome

    def _push_rebased_branch(self: Any, row: dict[str, Any], push_already_done: bool) -> WorkerOutcome | None:
        branch = str(row.get("branch") or self._row().get("branch") or "")
        if not branch:
            return None
        empty_outcome = self._block_if_rebased_branch_empty(row, branch)
        if empty_outcome is not None or push_already_done:
            return empty_outcome
        rc, push_out = self._git_in_workspace(
            ["push", "--force-with-lease", "-u", self.cfg.pr_remote, branch]
        )
        if rc == 0:
            return None
        fsm_runtime.block_current(
            self.store,
            self.node.id,
            note=f"rebase-to-main: force-push failed: {push_out[:200]}",
            last_error=f"rebase push: {push_out[:500]}",
        )
        return WorkerOutcome(State.BLOCKED, "rebase push failed")

    def _block_if_rebased_branch_empty(self: Any, row: dict[str, Any], branch: str) -> WorkerOutcome | None:
        if self._git_ahead_count(branch) != 0:
            return None
        worktree_path = row.get("worktree_path") or self._row().get("worktree_path") or ""
        note = (
            f"post-rebase branch is 0 commits ahead of {self.cfg.base_branch} — "
            f"the rebase likely dropped task changes. Inspect the worktree at {worktree_path} before retrying."
        )
        fsm_runtime.block_current(self.store, self.node.id, note=note, last_error=note[:1000])
        return WorkerOutcome(State.BLOCKED, "post-rebase empty branch")

    def _finish_rebase_to_main(self: Any, row: dict[str, Any], pre_state: State) -> WorkerOutcome:
        _, new_main_sha = self._git_in_workspace(
            ["rev-parse", f"{self.cfg.pr_remote}/{self.cfg.base_branch}"]
        )
        pr_number = row.get("pr_number") or self._row().get("pr_number")
        if pr_number:
            self._safe_retarget_or_recreate(int(_tw.cast(Any, pr_number)))
        self.store.clear_parent_branch(self.node.id)
        self.store.set_field(
            self.node.id,
            last_synced_main_sha=new_main_sha.strip() or None,
            pre_rebase_state=None,
        )
        fsm_runtime.enter_pending_ci(
            self.store,
            self.node.id,
            note=f"rebased onto main; restored {pre_state.value}",
        )
        return WorkerOutcome(pre_state, "rebased onto main")

    def _retarget_pr_to_main(self: Any, pr_number: int) -> bool:
        """Retarget an open PR's base to `cfg.base_branch`. Returns True on
        success, False on any failure (including the github auto-close
        case when the original base branch was deleted)."""
        try:
            r = _tw.subprocess.run(
                ["gh", "pr", "edit", str(pr_number), "--base", self.cfg.base_branch],
                cwd=self.cfg.repo_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            if r.returncode != 0:
                _tw.log.warning(
                    "gh pr edit --base %s for PR #%d failed (rc=%d): %s",
                    self.cfg.base_branch,
                    pr_number,
                    r.returncode,
                    (r.stderr or r.stdout)[:300],
                )
                return False
            _tw.log.info("retargeted PR #%d base → %s", pr_number, self.cfg.base_branch)
            return True
        except (_tw.subprocess.TimeoutExpired, OSError) as e:
            _tw.log.warning("gh pr edit raised: %s", e)
            return False

    def _create_new_pr_for_rebased_branch(self: Any) -> tuple[str | None, int | None]:
        """Create a fresh PR (base=main) for the current rebased branch.

        Used when github auto-closed the original PR because its base was
        the parent's deleted branch. The branch + commits are intact; we
        just need a new PR pointing at main. Reuses the existing
        `_tw.github.open_pr` helper so the PR title/body match the worker's
        normal _open_pr output.
        """
        title = f"{self.node.id}: {self.node.title}"
        body = self._pr_body()
        rc, url, out = _tw.github.open_pr(
            self._h, title, body, base=self.cfg.base_branch, log_path=self.log_path
        )
        if rc != 0 or not url:
            _tw.log.warning(
                "task %s: failed to create replacement PR after rebase (rc=%d): %s",
                self.node.id,
                rc,
                out[:300],
            )
            return None, None
        m = _tw.re.search(r"/pull/(\d+)", url)
        new_pr_number = int(m.group(1)) if m else None
        return url, new_pr_number

    def _pr_state(self: Any, pr_number: int) -> str | None:
        """Query the actual lifecycle state of a PR via gh.

        Returns one of "OPEN", "MERGED", "CLOSED", or None when the
        query failed entirely (network/auth error). The caller decides
        what to do with None — see `_safe_retarget_or_recreate`.
        """
        try:
            r = _tw.subprocess.run(
                ["gh", "pr", "view", str(pr_number), "--json", "state"],
                cwd=self.cfg.repo_path,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (_tw.subprocess.TimeoutExpired, OSError) as e:
            _tw.log.warning("gh pr view --json state for #%d raised: %s", pr_number, e)
            return None
        if r.returncode != 0:
            _tw.log.warning(
                "gh pr view --json state for #%d failed (rc=%d): %s",
                pr_number,
                r.returncode,
                (r.stderr or r.stdout)[:200],
            )
            return None
        try:
            data = _tw.json.loads(r.stdout)
        except _tw.json.JSONDecodeError:
            return None
        s = data.get("state")
        return str(s) if isinstance(s, str) else None

    def _safe_retarget_or_recreate(self: Any, pr_number: int) -> None:
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
            _tw.time.sleep(2)
            if self._retarget_pr_to_main(pr_number):
                return
            _tw.log.warning(
                "task %s: PR #%d still OPEN but retarget failed twice; marking BLOCKED",
                self.node.id,
                pr_number,
            )
            fsm_runtime.block_current(
                self.store,
                self.node.id,
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
                _tw.log.info(
                    "task %s: stale PR #%d closed; created fresh PR #%d on main",
                    self.node.id,
                    pr_number,
                    new_pr_number,
                )
            return
        if state == "MERGED":
            _tw.log.warning(
                "task %s: PR #%d already MERGED but rebase ran — leaving state alone",
                self.node.id,
                pr_number,
            )
            return
        # state is None — couldn't even read it. Refuse to create a new
        # PR (might be transient), mark BLOCKED so a human eyeballs it.
        _tw.log.warning(
            "task %s: PR #%d state unreachable; refusing to recreate to avoid duplicate",
            self.node.id,
            pr_number,
        )
        fsm_runtime.block_current(
            self.store,
            self.node.id,
            note=f"rebase-to-main: PR #{pr_number} state unreachable",
            last_error=f"could not read state for PR #{pr_number}; refusing to recreate",
        )
