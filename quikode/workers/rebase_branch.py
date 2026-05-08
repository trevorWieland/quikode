"""Branch divergence and parent-rebase worker mixin."""

from __future__ import annotations

import sys
from typing import Any

from quikode import fsm_runtime
from quikode.state import State
from quikode.workers.outcomes import WorkerOutcome


class _TaskWorkerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.workers.task_worker"], name)


_tw = _TaskWorkerGlobals()


class RebaseBranchMixin:
    def _latest_commit_sha_on_branch(self: Any) -> str:
        try:
            rc, out, _err = _tw.exec_in(
                self._h,
                ["bash", "-lc", "cd /workspace && git rev-parse HEAD"],
                log_path=self.log_path,
                timeout=30,
            )
        except Exception as e:
            _tw.log.warning("latest_commit_sha lookup failed: %s", e)
            return ""
        if rc != 0:
            return ""
        return (out or "").strip()

    def _handle_branch_divergence_if_needed(self: Any) -> WorkerOutcome | None:
        row = self._row()
        branch = self._branch_for_divergence(row)
        if branch is None:
            return None
        counts = self._branch_ahead_behind(branch)
        if counts is None:
            return None
        ahead, behind = counts
        if behind == 0:
            return None
        if ahead == 0:
            return self._handle_upstream_fast_forward(branch, behind)
        force_push_outcome = self._block_if_remote_history_rewritten(row, branch)
        if force_push_outcome is not None:
            return force_push_outcome
        _tw.log.info(
            "task %s: branch %s diverged (ahead=%d, behind=%d); attempting auto-rebase onto origin/%s",
            self.node.id,
            branch,
            ahead,
            behind,
            branch,
        )
        return self._rebase_diverged_branch(branch)

    def _branch_for_divergence(self: Any, row: dict[str, Any]) -> str | None:
        branch = row.get("branch")
        if not branch or self.handle is None:
            return None
        active_fixup_review = self.store.conn.execute(
            "SELECT 1 FROM subtasks WHERE task_id = ? AND kind LIKE 'fixup-review%' "
            "AND state IN ('doing','checking','triaging') LIMIT 1",
            (self.node.id,),
        ).fetchone()
        return None if active_fixup_review else str(branch)

    def _branch_ahead_behind(self: Any, branch: str) -> tuple[int, int] | None:
        rc_fetch, _out = self._git_in_workspace(["fetch", self.cfg.pr_remote, branch])
        if rc_fetch != 0:
            return None
        rc_b, out_b = self._git_in_workspace(
            ["rev-list", "--count", "--left-right", f"HEAD...{self.cfg.pr_remote}/{branch}"]
        )
        if rc_b != 0:
            return None
        try:
            parts = out_b.strip().splitlines()[-1].split()
            ahead = int(parts[0]) if len(parts) >= 1 else 0
            behind = int(parts[1]) if len(parts) >= 2 else 0
        except (ValueError, IndexError):
            return None
        return ahead, behind

    def _handle_upstream_fast_forward(self: Any, branch: str, behind: int) -> WorkerOutcome | None:
        _tw.log.info(
            "task %s: detected upstream FF on %s (behind=%d). Resetting --hard to origin/%s.",
            self.node.id,
            branch,
            behind,
            branch,
        )
        rc_r, _ = self._git_in_workspace(["reset", "--hard", f"{self.cfg.pr_remote}/{branch}"])
        if rc_r == 0:
            return None
        fsm_runtime.block_current(
            self.store,
            self.node.id,
            note=f"upstream FF detected on {branch} but `git reset --hard` failed",
        )
        return WorkerOutcome(State.BLOCKED, "upstream FF reset failed")

    def _block_if_remote_history_rewritten(
        self: Any, row: dict[str, Any], branch: str
    ) -> WorkerOutcome | None:
        base_ref_sha = str(row.get("base_ref_sha") or "")
        if not base_ref_sha:
            return None
        rc_anc, _ = self._git_in_workspace(
            ["merge-base", "--is-ancestor", base_ref_sha, f"{self.cfg.pr_remote}/{branch}"]
        )
        if rc_anc == 0:
            return None
        msg = (
            f"branch {branch} was force-pushed (history rewritten); "
            f"the work in this container does not match what's on the remote. "
            f"Use `quikode unblock {self.node.id}` to inspect, then "
            f"`quikode retry {self.node.id}` to start fresh."
        )
        _tw.log.error("task %s: %s", self.node.id, msg)
        fsm_runtime.block_current(self.store, self.node.id, note=msg[:300], last_error=msg[:1000])
        return WorkerOutcome(State.BLOCKED, "force-push detected on branch")

    def _rebase_diverged_branch(self: Any, branch: str) -> WorkerOutcome | None:
        rc, out = self._git_in_workspace(
            ["-c", "core.editor=true", "rebase", f"{self.cfg.pr_remote}/{branch}"]
        )
        if rc == 0:
            _tw.log.info("task %s: clean rebase onto origin/%s succeeded", self.node.id, branch)
            self._git_in_workspace(["push", "--force-with-lease", self.cfg.pr_remote, branch])
            return None
        if not self._rebase_in_progress():
            fsm_runtime.block_current(
                self.store,
                self.node.id,
                note=f"diverged-branch rebase failed (no rebase state dir): {out[:200]}",
                last_error=f"rebase {self.cfg.pr_remote}/{branch} failed: {out[:500]}",
            )
            return WorkerOutcome(State.BLOCKED, "diverged-branch rebase hard-failed")
        _tw.log.info(
            "task %s: rebase onto origin/%s hit conflicts; invoking resolver agent", self.node.id, branch
        )
        outcome = self._spawn_conflict_resolver()
        if outcome and outcome.final_state == State.BLOCKED:
            return outcome
        rc_p, push_out = self._git_in_workspace(["push", "--force-with-lease", self.cfg.pr_remote, branch])
        if rc_p != 0:
            _tw.log.warning(
                "task %s: force-with-lease push after rebase failed: %s", self.node.id, push_out[:300]
            )
        return None

    def _handle_parent_rebase_if_needed(self: Any) -> WorkerOutcome | None:
        """Plan 31 inline-checkpoint path. Drained by the active worker at
        safe checkpoints when the orchestrator stamped `needs_parent_rebase`.

        Picks the rebase target by inspecting the CURRENT state of the
        parents (vs the trigger-reason path the schedulers use):

        - All parents MERGED (or no parents) → reattach to main + retarget PR.
        - At least one parent still un-merged → stay stacked on parent's tip;
          keep PR base = parent's branch.
        """
        row = self._row()
        if not row.get("needs_parent_rebase"):
            return None
        _tw.log.info("task %s: needs_parent_rebase set; running inline rebase", self.node.id)
        if self.handle is None:
            return None
        target_kind = "main" if self._inline_rebase_target_is_main() else "parent_tip"
        ok = self._rebase_inline(target_kind)
        if not ok:
            fsm_runtime.block_current(
                self.store, self.node.id, note=f"needs_parent_rebase: inline rebase ({target_kind}) failed"
            )
            return WorkerOutcome(State.BLOCKED, f"parent-rebase ({target_kind}) failed")
        row = self._row()
        pr_number = row.get("pr_number")
        if target_kind == "main":
            if pr_number:
                self._retarget_pr_to_main(int(_tw.cast(Any, pr_number)))
            self.store.clear_parent_branch(self.node.id)
        # parent_tip: PR base + parent metadata stay as-is. Future cascades
        # still need this child rebased when the parent advances again.
        self.store.clear_needs_parent_rebase(self.node.id)
        return None

    def _inline_rebase_target_is_main(self: Any) -> bool:
        """Plan 31: derive inline rebase target. True (main) iff:
        - the task has no recorded parents, OR
        - every parent is in MERGED state (their branches are gone).

        Otherwise (at least one parent still un-merged), stay stacked on
        the parent's evolving tip.
        """
        parent_branches = self.store.get_parent_branches(self.node.id)
        if not parent_branches:
            return True
        return self.store.all_parents_merged(self.node.id)

    def _rebase_inline(self: Any, target_kind: str) -> bool:
        """Plan 31 inline rebase. Refactor of pre-plan-31 `_rebase_to_base_branch`
        which always targeted main; now picks `--onto main parent.sha` (target=main,
        un-stack semantic) vs `git rebase origin/parent.branch` (target=parent_tip,
        stay-stacked semantic).
        """
        row = self._row()
        branch = str(row["branch"])
        parent_branches = self.store.get_parent_branches(self.node.id)
        # Plan 31: multi-parent rebase deferred to plan 32 (merge-node).
        if target_kind == "parent_tip" and len(parent_branches) > 1:
            note = (
                f"inline parent_tip rebase requested for {self.node.id} with "
                f"parents={parent_branches}; deferred to plan 32 (merge-node "
                f"first-class entity)"
            )
            fsm_runtime.block_current(self.store, self.node.id, note=note, last_error=note[:1000])
            return False
        if target_kind == "parent_tip":
            return self._rebase_inline_to_parent_tip(branch, parent_branches[0])
        return self._rebase_inline_to_main(branch, parent_branches)

    def _rebase_inline_to_parent_tip(self: Any, branch: str, parent_branch: str) -> bool:
        rc_fetch, _ = self._git_in_workspace(["fetch", self.cfg.pr_remote, parent_branch])
        if rc_fetch != 0:
            return False
        rc, _out = self._git_in_workspace(
            ["-c", "core.editor=true", "rebase", f"{self.cfg.pr_remote}/{parent_branch}"]
        )
        if rc != 0:
            resolver_outcome = self._spawn_conflict_resolver(
                rebase_target_kind="parent_tip", parent_branch=parent_branch
            )
            if resolver_outcome and resolver_outcome.final_state == State.BLOCKED:
                return False
        push_rc, _push_out = self._git_in_workspace(
            ["push", "--force-with-lease", self.cfg.pr_remote, branch]
        )
        return push_rc == 0

    def _rebase_inline_to_main(self: Any, branch: str, parent_branches: list[str]) -> bool:
        row = self._row()
        parent_sha = ""
        if len(parent_branches) == 1:
            rc_ps, ps_out = self._git_in_workspace(["rev-parse", "--verify", parent_branches[0]])
            if rc_ps == 0:
                parent_sha = ps_out.strip().splitlines()[-1] if ps_out.strip() else ""
        elif len(parent_branches) > 1:
            parent_sha = str(row.get("parent_merge_base_sha") or "")
        self._git_in_workspace(["fetch", self.cfg.pr_remote, self.cfg.base_branch])
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
            resolver_outcome = self._spawn_conflict_resolver(rebase_target_kind="main")
            if resolver_outcome and resolver_outcome.final_state == State.BLOCKED:
                return False
        ahead = self._git_ahead_count(branch)
        if ahead == 0:
            row_now = self._row()
            worktree_path = row_now.get("worktree_path") or ""
            note = (
                f"post-rebase branch is 0 commits ahead of {self.cfg.base_branch} - "
                f"the rebase likely dropped task changes. Inspect the worktree at {worktree_path} before retrying."
            )
            fsm_runtime.block_current(self.store, self.node.id, note=note, last_error=note[:1000])
            return False
        push_rc, _push_out = self._git_in_workspace(
            ["push", "--force-with-lease", self.cfg.pr_remote, branch]
        )
        return push_rc == 0
