"""Rebases worker mixin (plan 31).

Two named worker entries, picked by the orchestrator based on trigger:

- `run_rebase_to_parent_tip` — fires on `parent_tip_advanced` (parent's PR
  branch advanced via fixup commit / review-feedback push). Child rebases
  onto the parent's NEW tip, PR base stays = parent's branch. Stack
  identity preserved.
- `run_rebase_to_main` — fires on `parent_merged` / `sibling_conflict` /
  `manual` (parent merged → branch gone; or this task's own PR is
  CONFLICTING against main). Child rebases onto main, PR base retargets
  to main. Used to be the only entry; pre-plan-31 it ran on every parent
  push, destroying stacked identity on the first parent fixup.

Multi-parent (plan 32 territory): when `len(parent_branches) > 1` AND
target_kind = parent_tip, the worker BLOCKs cleanly with a note pointing
at plan 32 (merge-node first-class entity). Single-parent and L1
(no-parent / parent-merged-and-gone) cases work fully at plan 31.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Literal

from quikode import fsm_runtime
from quikode.state import State
from quikode.workers.outcomes import WorkerOutcome
from quikode.workers.rebase_branch import RebaseBranchMixin
from quikode.workers.rebase_conflicts import RebaseConflictMixin


class _TaskWorkerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.workers.task_worker"], name)


_tw = _TaskWorkerGlobals()


TargetKind = Literal["parent_tip", "main"]


@dataclass
class _RebasePlan:
    pre_state: State
    onto_sha: str
    rebase_target: str
    target_kind: TargetKind
    parent_branch: str  # populated when target_kind="parent_tip"


class RebaseWorkerMixin(RebaseBranchMixin, RebaseConflictMixin):
    def run_rebase_to_parent_tip(self: Any) -> WorkerOutcome:
        """Plan 31: parent's PR branch advanced (push, not merge). Rebase the
        child onto the parent's new tip, force-push, leave PR base as parent.

        Returns the WorkerOutcome carrying the resumed state. Multi-parent
        cases are deferred to plan 32 (merge-node first-class entity); this
        entry BLOCKs cleanly with a forensic note when called on a child
        with > 1 parent branch.
        """
        return self._run_rebase("parent_tip")

    def run_rebase_to_main(self: Any) -> WorkerOutcome:
        """Plan 31: parent merged (branch gone) OR own PR is CONFLICTING.
        Rebase the child onto main, retarget the PR base to main, clear
        parent metadata.
        """
        return self._run_rebase("main")

    def _run_rebase(self: Any, target_kind: TargetKind) -> WorkerOutcome:
        row = self.store.get(self.node.id) or {}
        pre_state = self._pre_rebase_state(row)

        try:
            self._provision(provision_worktree=False)
            plan = self._prepare_rebase_plan(row, pre_state, target_kind)
            if isinstance(plan, WorkerOutcome):
                return plan
            fetch_outcome = self._fetch_base_for_rebase(plan, pre_state)
            if fetch_outcome is not None:
                return fetch_outcome
            push_done = self._run_rebase_plan(plan)
            if isinstance(push_done, WorkerOutcome):
                return push_done
            push_outcome = self._push_rebased_branch(row, push_done)
            if push_outcome is not None:
                return push_outcome
            return self._finish_rebase(row, plan)
        except Exception as e:
            _tw.log.exception("rebase (%s) for task %s crashed", target_kind, self.node.id)
            fsm_runtime.enter_pending_ci(
                self.store,
                self.node.id,
                note=f"rebase ({target_kind}) crashed: {e}; restoring {pre_state.value}",
                last_error=str(e)[:1000],
            )
            return WorkerOutcome(pre_state, f"rebase ({target_kind}) crashed: {e}")
        finally:
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

    def _prepare_rebase_plan(
        self: Any, row: dict[str, Any], pre_state: State, target_kind: TargetKind
    ) -> _RebasePlan | WorkerOutcome:
        parent_branches = self.store.get_parent_branches(self.node.id)
        # Plan 31 multi-parent: deferred to plan 32. BLOCK with a clear note.
        if target_kind == "parent_tip" and len(parent_branches) > 1:
            note = (
                f"multi-parent stack-on-parent rebase requested for {self.node.id} "
                f"with parents={parent_branches}; deferred to plan 32 "
                f"(merge-node first-class entity). BLOCKING."
            )
            fsm_runtime.block_current(self.store, self.node.id, note=note, last_error=note[:1000])
            return WorkerOutcome(State.BLOCKED, "multi-parent rebase deferred to plan 32")
        if target_kind == "parent_tip":
            return self._prepare_parent_tip_plan(parent_branches, pre_state)
        return self._prepare_main_plan(row, parent_branches, pre_state)

    def _prepare_parent_tip_plan(
        self: Any, parent_branches: list[str], pre_state: State
    ) -> _RebasePlan | WorkerOutcome:
        if not parent_branches:
            note = (
                f"parent_tip rebase requested for {self.node.id} but no parent branches "
                f"recorded. This shouldn't happen — orchestrator schedules parent_tip only "
                f"on cascade-on-push from a parent. BLOCKING for inspection."
            )
            fsm_runtime.block_current(self.store, self.node.id, note=note, last_error=note[:1000])
            return WorkerOutcome(State.BLOCKED, "parent_tip rebase with no parent")
        parent_branch = parent_branches[0]
        rc_fetch, _ = self._git_in_workspace(["fetch", self.cfg.pr_remote, parent_branch])
        if rc_fetch != 0:
            note = f"fetch of parent branch {parent_branch} failed for parent_tip rebase"
            fsm_runtime.enter_pending_ci(self.store, self.node.id, note=note, last_error=note[:1000])
            return WorkerOutcome(pre_state, note)
        return _RebasePlan(
            pre_state=pre_state,
            onto_sha="",
            rebase_target=f"{self.cfg.pr_remote}/{parent_branch}",
            target_kind="parent_tip",
            parent_branch=parent_branch,
        )

    def _prepare_main_plan(
        self: Any,
        row: dict[str, Any],
        parent_branches: list[str],
        pre_state: State,
    ) -> _RebasePlan | WorkerOutcome:
        # The `--onto` form moves child commits OFF parent_branch and ONTO main.
        # When there's no parent (true L1), we just rebase the branch onto main.
        onto_sha = self._verified_prior_merge_base(row)
        if not onto_sha and parent_branches:
            rc_ps, ps_out = self._git_in_workspace(["rev-parse", "--verify", parent_branches[0]])
            if rc_ps == 0 and ps_out.strip():
                onto_sha = ps_out.strip().splitlines()[-1]
        return _RebasePlan(
            pre_state=pre_state,
            onto_sha=onto_sha,
            rebase_target=f"{self.cfg.pr_remote}/{self.cfg.base_branch}",
            target_kind="main",
            parent_branch="",
        )

    def _verified_prior_merge_base(self: Any, row: dict[str, Any]) -> str:
        prior_merge_base_sha = str(row.get("parent_merge_base_sha") or "")
        if not prior_merge_base_sha:
            return ""
        rc_ps, _ = self._git_in_workspace(["rev-parse", "--verify", prior_merge_base_sha])
        return prior_merge_base_sha if rc_ps == 0 else ""

    def _fetch_base_for_rebase(self: Any, plan: _RebasePlan, pre_state: State) -> WorkerOutcome | None:
        if plan.target_kind == "main":
            rc, fetch_out = self._git_in_workspace(["fetch", self.cfg.pr_remote, self.cfg.base_branch])
        else:
            rc, fetch_out = self._git_in_workspace(["fetch", self.cfg.pr_remote, plan.parent_branch])
        if rc == 0:
            return None
        fsm_runtime.enter_pending_ci(
            self.store,
            self.node.id,
            note=f"rebase fetch failed ({fetch_out[:200]}); restoring {pre_state.value}",
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
        resolver_outcome = self._spawn_conflict_resolver(
            rebase_target_kind=plan.target_kind, parent_branch=plan.parent_branch
        )
        if resolver_outcome is None:
            return True
        _tw.log.warning("rebase (%s): conflict resolver gave up for task %s", plan.target_kind, self.node.id)
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
            note=f"rebase: force-push failed: {push_out[:200]}",
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

    def _finish_rebase(self: Any, row: dict[str, Any], plan: _RebasePlan) -> WorkerOutcome:
        _, new_main_sha = self._git_in_workspace(
            ["rev-parse", f"{self.cfg.pr_remote}/{self.cfg.base_branch}"]
        )
        pr_number = row.get("pr_number") or self._row().get("pr_number")
        if plan.target_kind == "main":
            # Reattach: retarget PR to main + clear parent metadata.
            if pr_number:
                self._safe_retarget_or_recreate(int(_tw.cast(Any, pr_number)))
            self.store.clear_parent_branch(self.node.id)
            note = f"rebased onto main; restored {plan.pre_state.value}"
        else:
            # Stay-stacked: PR stays on parent's branch; parent metadata
            # preserved so future cascades still find this child.
            note = f"rebased onto parent tip ({plan.parent_branch}); restored {plan.pre_state.value}"
        self.store.set_field(
            self.node.id,
            last_synced_main_sha=new_main_sha.strip() or None,
            pre_rebase_state=None,
        )
        fsm_runtime.enter_pending_ci(self.store, self.node.id, note=note)
        return WorkerOutcome(plan.pre_state, note)

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
