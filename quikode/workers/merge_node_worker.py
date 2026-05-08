"""Plan 32 PR-A 3/3: minimal merge-node worker.

A merge-node is a synthetic `kind="merge"` task that integrates N source
spec parents into one stable branch. Multiple downstream children sharing
the same parent set fork off this branch — multi-parent dependency reduces
to single-parent dependency on the merge-node.

This worker drives the deterministic-merge slice of the lifecycle:

    PENDING → PROVISIONING (worktree off cfg.base_branch) →
    PLANNING (synthesize a single integrate subtask, no agent involvement) →
    DOING_SUBTASK (octopus merge first; on octopus failure, sequential by
    sorted parent_task_ids; on sequential failure, BLOCK pointing at PR-B's
    doer-subloop fallback) → CHECKING_SUBTASK → COMMITTING → PUSHING
    (force-with-lease so the stable merge-node branch refreshes across
    re-merges) → LOCAL_CI_CHECKING (cfg.local_ci_command on the merged
    tree) → PRE_PR_AUDITING (deferred to PR-B; for PR-A we fast-forward
    via MERGE_NODE_BUILT) → MERGE_NODE_READY.

PR-A 3/3 deliberately skips the audit gauntlet (rubric/standards/behavior).
PR-B will plug in:

  - the `merge-planner` prompt for non-trivial integrations,
  - the conflict-resolver subloop for unresolvable textual conflicts, and
  - the audit gauntlet (local-CI + behavior always; rubric/standards when
    integration subtasks ran).

Until then, semantic conflicts BLOCK with a forensic note. The user has
accepted this tradeoff: PR-A unblocks plan 31's clean BLOCK on multi-parent
children for the trivial-merge case (which is the majority); PR-B handles
the long tail.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from quikode import fsm_runtime, pre_pr_audit, worktree
from quikode.execution import exec_in
from quikode.fsm import Event, State
from quikode.workers.outcomes import WorkerOutcome
from quikode.workers.task_worker import TaskWorker

log = logging.getLogger("quikode.merge_node_worker")


class MergeNodeWorker(TaskWorker):
    """Subclass that overrides `run()` for the deterministic merge-node lifecycle.

    Inherits provisioning helpers (`_provision_container`, `_existing_worktree_path`,
    `_teardown`, `_safe_crash_current`) and the `WorkerOutcome` contract from
    `TaskWorker`, but skips the spec-task plumbing (planner agent, doer agent,
    checker agent, PR opening). Provisioning of the worktree is bespoke: we
    branch off `cfg.base_branch` (NOT off any parent), then merge the parents
    in deterministically.
    """

    def run(self) -> WorkerOutcome:
        """Drive the merge-node from PENDING to MERGE_NODE_READY (or BLOCKED)."""
        try:
            self._provision_merge_node_worktree()
            self._provision_container(Path(str(self._row()["worktree_path"])))
            outcome = self._integrate_parents()
            if outcome:
                return outcome
            outcome = self._push_merge_branch()
            if outcome:
                return outcome
            outcome = self._run_local_ci_gate()
            if outcome:
                return outcome
            # PR-A 3/3: skip the audit gauntlet (PR-B adds it). Fire
            # MERGE_NODE_BUILT directly so multi-parent children unblock now.
            # TODO(plan 32 PR-B): wire `_run_pre_pr_pipeline` with
            # `merge_node_mode=True` (local_ci + behavior gauntlet, rubric +
            # standards only when integration subtasks ran). Replace this
            # direct event with the gauntlet's pass/fail handoff.
            fsm_runtime.merge_node_built(
                self.store,
                self.node.id,
                note="PR-A 3/3: deterministic merge integrated; audit gauntlet deferred to PR-B",
            )
            return WorkerOutcome(State.MERGE_NODE_READY, "merge-node integrated")
        except Exception as e:
            log.exception("merge-node %s crashed", self.node.id)
            self._safe_crash_current(str(e))
            return WorkerOutcome(State.FAILED, str(e))
        finally:
            self._teardown()

    # ----- provisioning -----

    def _provision_merge_node_worktree(self) -> None:
        """Stand up a worktree branched off `cfg.base_branch`.

        Unlike the spec worker, we use the deterministic merge-node branch
        name (already stamped on the row by `lookup_or_create_merge_node`)
        rather than generating a fresh hex suffix per run. The branch is
        force-pushed across re-merges so children's PR base remains stable.
        """
        if fsm_runtime.current_state(self.store, self.node.id) is State.PENDING:
            fsm_runtime.start_task(
                self.store,
                self.node.id,
                note="merge-node provisioning worktree off base branch",
            )
        row = self._row()
        branch = str(row.get("branch") or "")
        if not branch:
            raise RuntimeError(
                f"merge-node {self.node.id} has no branch on row; create_merge_node should have stamped one"
            )
        existing_path = row.get("worktree_path")
        if existing_path and Path(str(existing_path)).exists():
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
                return
        wt_dir = self.node.id.replace("/", "-").lower()
        wt_path = (self.cfg.worktree_root / wt_dir).resolve()
        worktree.fetch_base(self.cfg.repo_path, self.cfg.pr_remote, self.cfg.base_branch)
        worktree.add_worktree(
            self.cfg.repo_path,
            wt_path,
            branch,
            self.cfg.base_branch,
            self.cfg.pr_remote,
        )
        self.store.set_field(
            self.node.id,
            branch=branch,
            worktree_path=str(wt_path),
        )

    # ----- integration: deterministic merge -----

    def _integrate_parents(self) -> WorkerOutcome | None:
        """Run the deterministic merge: octopus first, sequential fallback.

        Sequence (per plan 32 PR-A):

          1. Fetch each parent branch from `cfg.pr_remote`.
          2. Hard-reset the merge-node branch to `<remote>/<base_branch>`
             so re-runs start from a clean slate.
          3. Try `git merge --no-ff --no-edit <p1> <p2> ...` (octopus).
          4. On octopus failure: `git merge --abort`, reset, then sequential —
             `git merge --no-ff --no-edit <p_i>` per parent in
             `parent_task_ids` order. On any sequential conflict, BLOCK
             pointing at PR-B for the doer-subloop.

        State transitions: PROVISIONING → PLANNING → DOING_SUBTASK →
        CHECKING_SUBTASK → COMMITTING. The merge commit is the
        deterministic-merge result; nothing else lands on the branch.
        """
        parent_ids = self.store.get_parent_task_ids(self.node.id)
        parent_branches = self.store.get_parent_branches(self.node.id)
        if not parent_branches:
            return self._block(
                "no parent branches",
                f"merge-node {self.node.id} has no parent branches; "
                "cannot integrate. BLOCKING for operator inspection.",
            )
        self._enter_planning_and_seed_subtask(parent_ids, parent_branches)
        prep_outcome = self._fetch_and_reset(parent_branches)
        if prep_outcome is not None:
            return prep_outcome
        return self._merge_octopus_or_sequential(parent_ids, parent_branches)

    def _fetch_and_reset(self, parent_branches: list[str]) -> WorkerOutcome | None:
        """Combined: fetch every parent branch, then reset HEAD to origin/<base>."""
        fetch_outcome = self._fetch_parent_branches(parent_branches)
        if fetch_outcome is not None:
            return fetch_outcome
        return self._reset_to_base("initial reset before merge")

    def _merge_octopus_or_sequential(
        self, parent_ids: list[str], parent_branches: list[str]
    ) -> WorkerOutcome | None:
        """Try octopus first; on failure, abort+reset+sequential."""
        if self._try_octopus(parent_branches):
            log.info("merge-node %s: octopus merge succeeded", self.node.id)
            return self._finish_integration_subtask("octopus")
        self._git_in_workspace(["merge", "--abort"])
        reset_outcome = self._reset_to_base("reset before sequential merge")
        if reset_outcome is not None:
            return reset_outcome
        seq_outcome = self._try_sequential(parent_ids, parent_branches)
        if seq_outcome is not None:
            return seq_outcome
        log.info("merge-node %s: sequential merge succeeded", self.node.id)
        return self._finish_integration_subtask("sequential")

    def _block(self, short: str, note: str) -> WorkerOutcome:
        """Helper: stamp BLOCKED with a forensic note + return the outcome."""
        fsm_runtime.block_current(self.store, self.node.id, note=note, last_error=note[:1000])
        return WorkerOutcome(State.BLOCKED, short)

    def _enter_planning_and_seed_subtask(self, parent_ids: list[str], parent_branches: list[str]) -> None:
        """Move PROVISIONING → PLANNING → DOING_SUBTASK and seed the
        single integrate subtask. PR-B replaces this with merge-planner
        agent output for non-trivial integrations."""
        fsm_runtime.environment_ready(
            self.store, self.node.id, note="merge-node provisioned; entering planning"
        )
        self.store.upsert_subtasks(
            self.node.id,
            [
                {
                    "subtask_id": "S-01-integrate",
                    "title": "Integrate parents via octopus or sequential merge",
                    "depends_on": [],
                    "files_to_touch": [],
                    "boundary": "git merge only; no semantic edits",
                    "acceptance": ["all parent branches merged into merge-node branch"],
                    "notes": f"parents={parent_ids}",
                    "kind": "merge-integration",
                }
            ],
        )
        fsm_runtime.enter_doing_subtask(
            self.store,
            self.node.id,
            note=f"merge-node integrating {len(parent_branches)} parent(s)",
        )

    def _fetch_parent_branches(self, parent_branches: list[str]) -> WorkerOutcome | None:
        """Fetch every parent branch from the remote into the dev container's
        git store. Returns BLOCKED outcome on first failure."""
        for pb in parent_branches:
            rc, out, err = exec_in(
                self._h,
                ["bash", "-lc", f"cd /workspace && git fetch {self.cfg.pr_remote} {pb}"],
                log_path=self.log_path,
                timeout=120,
            )
            if rc != 0:
                return self._block(
                    "parent fetch failed",
                    f"merge-node {self.node.id}: failed to fetch parent branch {pb!r}: {(out + err)[:200]}",
                )
        return None

    def _reset_to_base(self, label: str) -> WorkerOutcome | None:
        rc, out = self._git_in_workspace(["reset", "--hard", f"{self.cfg.pr_remote}/{self.cfg.base_branch}"])
        if rc != 0:
            return self._block(
                "reset failed",
                f"merge-node {label} ({self.cfg.base_branch}) failed: {out[:200]}",
            )
        return None

    def _try_octopus(self, parent_branches: list[str]) -> bool:
        """Attempt the octopus merge. Returns True on clean merge."""
        remote_parents = [f"{self.cfg.pr_remote}/{pb}" for pb in parent_branches]
        rc, _ = self._git_in_workspace(
            [
                "-c",
                "core.editor=true",
                "merge",
                "--no-ff",
                "--no-edit",
                *remote_parents,
            ]
        )
        return rc == 0

    def _try_sequential(self, parent_ids: list[str], parent_branches: list[str]) -> WorkerOutcome | None:
        """Sequential merge — `git merge` each parent in `parent_task_ids` order.
        Returns a BLOCKED outcome on the first conflict."""
        for parent_id, parent_branch in zip(parent_ids, parent_branches, strict=False):
            remote_ref = f"{self.cfg.pr_remote}/{parent_branch}"
            rc, _ = self._git_in_workspace(
                ["-c", "core.editor=true", "merge", "--no-ff", "--no-edit", remote_ref]
            )
            if rc != 0:
                self._git_in_workspace(["merge", "--abort"])
                return self._block(
                    "sequential merge conflict",
                    f"merge-node sequential merge conflict on parent "
                    f"{parent_id} ({parent_branch}); PR-B will add the "
                    f"doer-subloop fallback",
                )
        return None

    def _finish_integration_subtask(self, strategy: str) -> WorkerOutcome | None:
        """Move the integration subtask through CHECKING → COMMITTING. The
        merge commit was created by `git merge`; we just need to advance
        the FSM so the lifecycle reaches PUSHING for the force-push.
        """
        # The merge commit already exists in the worktree HEAD. We don't
        # need a separate `git commit` — the merge is the commit.
        fsm_runtime.enter_checking_subtask(
            self.store, self.node.id, note=f"deterministic merge ({strategy}) complete"
        )
        fsm_runtime.enter_committing(self.store, self.node.id, note="merge commit already on HEAD")
        return None

    # ----- push -----

    def _push_merge_branch(self) -> WorkerOutcome | None:
        """Force-with-lease push the merge-node branch to the remote.

        The branch name is stable across re-merges (deterministic from
        parent_task_ids), so children's PR base stays valid even when the
        merge-node re-runs after a parent advances. `--force-with-lease`
        catches the case where a parallel merge-node attempt raced ahead
        of us — better to surface than silently overwrite.
        """
        fsm_runtime.enter_pushing(self.store, self.node.id)
        branch = str(self._row()["branch"])
        rc, out = self._git_in_workspace(["push", "--force-with-lease", "-u", self.cfg.pr_remote, branch])
        if rc != 0:
            note = f"merge-node force-push failed: {out[:200]}"
            fsm_runtime.block_current(self.store, self.node.id, note=note, last_error=note[:1000])
            return WorkerOutcome(State.BLOCKED, "merge-node push failed")
        return None

    # ----- local CI gate -----

    def _run_local_ci_gate(self) -> WorkerOutcome | None:
        """Run `cfg.local_ci_command` against the merged worktree. The full
        audit gauntlet is deferred to PR-B; this is the only gate before
        firing MERGE_NODE_BUILT.
        """
        # Subtask loop is conceptually done — the integration subtask landed.
        # Transition to LOCAL_CI_CHECKING via ALL_SUBTASKS_DONE.
        # Transition PUSHING → LOCAL_CI_CHECKING via ALL_SUBTASKS_DONE; the
        # merge integration subtask is conceptually done at this point.
        self.store.apply_event(
            self.node.id,
            Event.ALL_SUBTASKS_DONE,
            note="merge integration committed; entering local CI",
        )
        outcome = pre_pr_audit.run_local_ci_gate(cfg=self.cfg, handle=self._h, log_path=self.log_path)
        if not outcome.passed:
            note = (
                f"merge-node local CI failed: {outcome.summary[:200]}; "
                "the merge integrates but the merged tree doesn't build/test clean"
            )
            self.store.add_artifact(self.node.id, "merge_node_local_ci_failure", outcome.raw_output or "")
            fsm_runtime.block_current(self.store, self.node.id, note=note, last_error=note[:1000])
            return WorkerOutcome(State.BLOCKED, "merge-node local CI failed")
        # Move to PRE_PR_AUDITING so the canonical (PRE_PR_AUDITING,
        # MERGE_NODE_BUILT) → MERGE_NODE_READY transition can fire.
        fsm_runtime.enter_pre_pr_auditing(
            self.store,
            self.node.id,
            note="merge-node local CI passed; PR-B will add audit gauntlet here",
        )
        return None

    # ----- helpers (override row narrowing for type happiness) -----

    def _row(self) -> Any:
        row = self.store.get(self.node.id)
        assert row is not None, f"merge-node {self.node.id!r} should exist in store"
        return row
