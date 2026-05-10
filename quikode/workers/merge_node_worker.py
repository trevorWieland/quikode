"""Plan 32: merge-node worker (PR-A 3/3 + PR-B doer-subloop + audit gauntlet).

A merge-node is a synthetic `kind="merge"` task that integrates N source
spec parents into one stable branch. Multiple downstream children sharing
the same parent set fork off this branch — multi-parent dependency reduces
to single-parent dependency on the merge-node.

Lifecycle:

    PENDING → PROVISIONING (worktree off cfg.base_branch) →
    PLANNING (deterministic-merge first: octopus → sequential) →
    DOING_SUBTASK (clean merge: single seeded subtask;
                  conflict: merge-planner emits N integration subtasks,
                  doer/checker drives them through the standard loop) →
    CHECKING_SUBTASK → COMMITTING → PUSHING (force-with-lease) →
    LOCAL_CI_CHECKING + PRE_PR_AUDITING (gauntlet runs with
    merge_node_mode=True: local_ci + behavior always; rubric + standards
    only when `kind="merge-integration"` subtasks ran) →
    MERGE_NODE_READY.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from quikode import fsm_runtime, prompts, worktree
from quikode.agent_registry import make_agent
from quikode.agent_schemas import MergePlannerOutput, SubtaskSpec
from quikode.execution import exec_in
from quikode.fsm import Event, State
from quikode.subtask_schema import (
    Plan,
    PlanValidationError,
    validate_and_build_plan,
)
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
            outcome = self._run_audit_gauntlet()
            if outcome:
                return outcome
            fsm_runtime.merge_node_built(
                self.store,
                self.node.id,
                note="merge-node audit gauntlet passed; ready as integration base",
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
        """Try octopus first; on failure, abort+reset+sequential. On
        sequential conflict, drop into the merge-planner doer-subloop."""
        if self._try_octopus(parent_branches):
            log.info("merge-node %s: octopus merge succeeded", self.node.id)
            return self._finish_integration_subtask("octopus")
        self._git_in_workspace(["merge", "--abort"])
        reset_outcome = self._reset_to_base("reset before sequential merge")
        if reset_outcome is not None:
            return reset_outcome
        seq_clean, conflict_parent = self._try_sequential(parent_ids, parent_branches)
        if seq_clean:
            log.info("merge-node %s: sequential merge succeeded", self.node.id)
            return self._finish_integration_subtask("sequential")
        # Sequential merge stopped on a conflict. Worktree carries the
        # partial merge. Spawn the merge-planner doer-subloop to plan
        # integration subtasks; the existing per-subtask doer/checker
        # loop drives them and resolves the conflicts.
        log.info(
            "merge-node %s: sequential merge conflict on parent %s — invoking merge-planner subloop",
            self.node.id,
            conflict_parent,
        )
        return self._run_merge_planner_subloop(parent_ids, parent_branches)

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
        # Plan 52: merge-node seeded subtasks are cycle-1 / kind="merge".
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
            planning_cycle=1,
            planning_kind="merge",
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

    def _try_sequential(self, parent_ids: list[str], parent_branches: list[str]) -> tuple[bool, str | None]:
        """Sequential merge — `git merge` each parent in `parent_task_ids`
        order. Returns `(True, None)` on clean merge, `(False, conflict_parent_id)`
        on first conflict (worktree left with conflict markers; the caller
        runs the merge-planner subloop to resolve)."""
        for parent_id, parent_branch in zip(parent_ids, parent_branches, strict=False):
            remote_ref = f"{self.cfg.pr_remote}/{parent_branch}"
            rc, _ = self._git_in_workspace(
                ["-c", "core.editor=true", "merge", "--no-ff", "--no-edit", remote_ref]
            )
            if rc != 0:
                # Leave the worktree in its conflicted state — the
                # merge-planner subloop reads the conflict markers and
                # plans integration subtasks against them.
                return False, parent_id
        return True, None

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
        # Idempotent: when the per-subtask loop ran (planner subloop
        # path), it already transitioned through COMMITTING → PUSHING.
        if fsm_runtime.current_state(self.store, self.node.id) is not State.PUSHING:
            fsm_runtime.enter_pushing(self.store, self.node.id)
        branch = str(self._row()["branch"])
        rc, out = self._git_in_workspace(["push", "--force-with-lease", "-u", self.cfg.pr_remote, branch])
        if rc != 0:
            note = f"merge-node force-push failed: {out[:200]}"
            fsm_runtime.block_current(self.store, self.node.id, note=note, last_error=note[:1000])
            return WorkerOutcome(State.BLOCKED, "merge-node push failed")
        return None

    # ----- audit gauntlet (plan 32 PR-B) -----

    def _run_audit_gauntlet(self) -> WorkerOutcome | None:
        """Run the pre-PR pipeline in `merge_node_mode=True`. local_ci +
        behavior always run; rubric + standards run only when the cycle
        included `kind="merge-integration"` subtasks (the merge-doer
        emitted real new code). On failure the inherited pipeline runs
        the standard fixup-decomposition loop and may BLOCK after
        `cfg.pre_pr_audit_max_cycles` cycles."""
        self.store.apply_event(
            self.node.id,
            Event.ALL_SUBTASKS_DONE,
            note="merge integration pushed; entering merge-node audit gauntlet",
        )
        return self._run_pre_pr_pipeline(merge_node_mode=True)

    # ----- merge-planner doer-subloop -----

    def _run_merge_planner_subloop(
        self, parent_ids: list[str], parent_branches: list[str]
    ) -> WorkerOutcome | None:
        """When deterministic merge fails, plan integration subtasks via
        the merge-planner agent and run them through the standard doer/
        checker loop. The worktree is in a partial-merge state with
        conflict markers; the doer subtasks resolve them."""
        # Replace the seeded "S-01-integrate" placeholder with the
        # planner-emitted slices. Subtasks carry kind="merge-integration"
        # so the audit gauntlet can detect they ran (rubric + standards
        # re-enable in that case per plan 32 PR-B).
        plan = self._invoke_merge_planner(parent_ids, parent_branches)
        if plan is None:
            return self._block(
                "merge-planner failed",
                f"merge-node {self.node.id}: merge-planner agent did not produce a "
                f"valid plan after sequential conflict; BLOCKing for operator review",
            )
        # Plan 52: merge-planner subtasks share the merge-node's single
        # planning cycle (merge nodes have one cycle by design).
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
                    "kind": "merge-integration",
                }
                for s in plan.subtasks
            ],
            planning_cycle=1,
            planning_kind="merge",
        )
        # Make the parsed plan visible to the inherited subtask loop
        # helpers that read self.plan / self.plan_text.
        self.plan = plan
        self.plan_text = plan.model_dump_json()
        self.store.set_field(self.node.id, plan_text=self.plan_text)
        log.info(
            "merge-node %s: merge-planner emitted %d integration subtask(s): %s",
            self.node.id,
            len(plan.subtasks),
            ", ".join(s.id for s in plan.subtasks),
        )
        # Drive the standard subtask loop. This handles doer + checker +
        # triage + commit + push for each subtask. The doer's first move
        # will be `git status` showing the conflict markers — it edits
        # the files, runs `git add`, and the per-subtask commit lands the
        # resolution as a discrete commit on the merge-node branch.
        outcome = self._run_subtask_set(list(plan.topo_order()))
        if outcome is not None:
            return outcome
        # Loop completed cleanly. The integration subtask commits already
        # landed on the worktree branch (per-subtask commits via the doer
        # loop); fall through to push.
        return None

    def _invoke_merge_planner(self, parent_ids: list[str], parent_branches: list[str]) -> Plan | None:
        """Build per-parent context from the runtime DAG (when present)
        and the worktree git state, then run the merge-planner agent
        through the JsonAgent layer. Returns the runtime Plan or None
        on agent / parse / runtime-validation failure.

        Plan 33: merge nodes also get an EvaluationContract built/loaded
        at the same lifecycle point as spec tasks. The merge planner sees
        the same four-stage rubric the audit gauntlet will apply to the
        merged branch.

        Plan 38 PR-B.4: the agent is built via `make_agent("merge_planner",
        cfg)` and returns a validated `MergePlannerOutput`. The wire→runtime
        translator runs Z-99 stabilization injection (the merged branch
        runs the same spec gate as a spec task).
        """
        contract = self._evaluation_contract()
        parent_contexts = self._build_parent_contexts(parent_ids, parent_branches)
        prompt = prompts.merge_planner_prompt(self.cfg, self.node, parent_contexts, contract)
        self._write_log_header("MERGE PLANNER", prompt)
        agent = make_agent("merge_planner", self.cfg)
        call_id = self.store.record_agent_call_started(
            self.node.id,
            phase="merge_planner",
            cli="json_agent",
            model=self.cfg.merge_planner_model,
        )
        result = agent.invoke(
            prompt,
            handle=self._h,
            log_path=self.log_path,
            timeout=self.cfg.merge_planner_timeout_s,
        )
        self.store.record_agent_call_finished(
            call_id,
            rc=result.rc,
            duration_s=result.duration_s or 0,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            cost_usd=result.cost_usd,
        )
        artifact_text = result.raw_text or (
            result.structured.model_dump_json() if result.structured is not None else ""
        )
        if artifact_text:
            self.store.add_artifact(self.node.id, "merge_planner_output", artifact_text)
        if result.rc != 0:
            log.warning("merge-planner agent rc=%d for %s", result.rc, self.node.id)
            return None
        if result.parse_errors or result.structured is None:
            log.warning(
                "merge-planner output failed schema validation for %s: %s",
                self.node.id,
                "; ".join(result.parse_errors)[:300] if result.parse_errors else "no structured output",
            )
            return None
        if not isinstance(result.structured, MergePlannerOutput):
            log.warning(
                "merge-planner returned unexpected schema %s for %s",
                type(result.structured).__name__,
                self.node.id,
            )
            return None
        try:
            return _wire_to_runtime_merge_plan(
                result.structured,
                expected_node_id=self.node.id,
                spec_gate_command=self.cfg.local_ci_command,
                rubric_categories=list(self.cfg.pre_pr_rubric_categories or []),
                rubric_min_score=int(self.cfg.pre_pr_rubric_min_score),
            )
        except PlanValidationError as e:
            log.warning("merge-planner output failed runtime validation for %s: %s", self.node.id, e)
            return None

    def _build_parent_contexts(self, parent_ids: list[str], parent_branches: list[str]) -> list[dict]:
        """Per-parent dicts for the merge-planner prompt. Title + summary
        come from the runtime DAG (when the parent has a node); diff is
        computed from `<base>...<remote>/<parent_branch>` in the
        worktree."""
        out: list[dict] = []
        for pid, branch in zip(parent_ids, parent_branches, strict=False):
            node = self.dag.nodes.get(pid)
            title = node.title if node is not None else f"parent task {pid}"
            summary = (node.scope[:300] if node is not None else "") or ""
            _, diff = self._git_in_workspace(
                [
                    "diff",
                    f"{self.cfg.pr_remote}/{self.cfg.base_branch}...{self.cfg.pr_remote}/{branch}",
                    "--no-color",
                ]
            )
            out.append(
                {
                    "task_id": pid,
                    "branch": branch,
                    "title": title,
                    "summary": summary,
                    "diff_excerpt": diff,
                }
            )
        return out

    # ----- helpers (override row narrowing for type happiness) -----

    def _row(self) -> Any:
        row = self.store.get(self.node.id)
        assert row is not None, f"merge-node {self.node.id!r} should exist in store"
        return row


# ---------- wire ↔ runtime translation for merge-planner output ----------


def _wire_subtask_to_runtime_dict(spec: SubtaskSpec) -> dict[str, Any]:
    """Translate one wire `SubtaskSpec` to the runtime `Subtask` ingest dict.

    Mirrors the spec-planner driver's helper of the same name; kept
    duplicated rather than imported so a future divergence (e.g.
    merge-only `kind="merge-integration"` defaults) doesn't cause a
    circular import between the two modules.
    """
    return {
        "id": spec.id,
        "title": spec.title,
        "depends_on": list(spec.depends_on),
        "files_to_touch": list(spec.files_to_touch),
        "boundary": spec.boundary,
        "acceptance": list(spec.acceptance),
        "notes": spec.notes,
        "interfaces": list(spec.interfaces),
        "kind": spec.kind,
        "rubric_targets": [
            {"category": t.category, "predicted_score": t.predicted_score} for t in spec.rubric_targets
        ],
        "standards_referenced": [
            {"doc_path": r.doc_path, "section": r.section} for r in spec.standards_referenced
        ],
        "architecture_referenced": [
            {"doc_path": r.doc_path, "section": r.section} for r in spec.architecture_referenced
        ],
        "behavior_evidence_advanced": list(spec.behavior_evidence_advanced),
    }


def _wire_to_runtime_merge_plan(
    merge_output: MergePlannerOutput,
    *,
    expected_node_id: str | None,
    spec_gate_command: str | None,
    rubric_categories: list[str] | None,
    rubric_min_score: int | None,
) -> Plan:
    """Translate a wire `MergePlannerOutput` into a runtime `Plan`.

    Same shape as the spec-planner translator; the merge-planner output
    drops `merge_context_summary` (informational, not consumed by the
    runtime Plan model) and otherwise hands off to
    `validate_and_build_plan` for Z-99 injection + runtime validation.
    """
    raw_plan = {
        "node_id": merge_output.node_id,
        "summary": merge_output.summary,
        "gauntlet_strategy": merge_output.gauntlet_strategy,
        "subtasks": [_wire_subtask_to_runtime_dict(s) for s in merge_output.subtasks],
        "final_acceptance": list(merge_output.final_acceptance),
    }
    return validate_and_build_plan(
        raw_plan,
        expected_node_id=expected_node_id,
        spec_gate_command=spec_gate_command,
        rubric_categories=rubric_categories,
        rubric_min_score=rubric_min_score,
    )
