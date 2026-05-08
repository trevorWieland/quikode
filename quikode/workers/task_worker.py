"""Per-task worker driver."""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, cast

from quikode import (
    docker_env,
    evaluation_contract,
    fsm_runtime,
    github,
    github_graphql,
    manual_probe,
    merge_node,
    planner_validators,
    pre_pr_audit,
    prompts,
    retry_classify,
    sound,
    worktree,
)
from quikode.agents.progress import (
    ProgressAttempt,
    ProgressVerdict,
    build_progress_agent,
)
from quikode.config import Config
from quikode.dag import Node
from quikode.execution import ExecutionBackend, build_execution_backend, exec_in
from quikode.fsm import TERMINAL_STATES
from quikode.orchestration import scheduler
from quikode.state import State, Store, TaskRow
from quikode.subtask_schema import Plan
from quikode.types import Verdict
from quikode.workers.feedback import FeedbackWorkerMixin
from quikode.workers.outcomes import (
    CheckerOutcome,
    SubtaskPassOutcome,
    WorkerOutcome,
)
from quikode.workers.pr_lifecycle import PrLifecycleWorkerMixin
from quikode.workers.pre_pr import PrePrWorkerMixin
from quikode.workers.rebases import RebaseWorkerMixin
from quikode.workers.subtasks import SubtaskWorkerMixin

log = logging.getLogger("quikode.worker")

_CheckerOutcome = CheckerOutcome
_SubtaskPassOutcome = SubtaskPassOutcome

_PATCH_EXPORTS = (
    json,
    re,
    shlex,
    subprocess,
    time,
    cast,
    docker_env,
    evaluation_contract,
    github,
    github_graphql,
    manual_probe,
    merge_node,
    planner_validators,
    pre_pr_audit,
    prompts,
    retry_classify,
    sound,
    worktree,
    ProgressAttempt,
    ProgressVerdict,
    build_progress_agent,
    exec_in,
    build_execution_backend,
    scheduler,
)


class TaskWorker(
    FeedbackWorkerMixin,
    RebaseWorkerMixin,
    SubtaskWorkerMixin,
    PrePrWorkerMixin,
    PrLifecycleWorkerMixin,
):
    def __init__(self, cfg: Config, dag: Any, store: Store, node: Node):
        self.cfg = cfg
        self.execution_backend: ExecutionBackend = build_execution_backend(cfg)
        self.dag = dag
        self.store = store
        self.node = node
        self.handle: Any | None = None
        self.log_path = cfg.log_dir / f"{node.id}.log"
        self.plan_text: str = ""  # raw planner stdout (kept for artifact + PR body)
        self.plan: Plan | None = None  # parsed structured plan
        self.last_doer_summary: str = ""
        self.last_triage_notes: str = ""
        # Plan 33: per-task EvaluationContract; built once at PROVISIONING →
        # PLANNING in _build_and_persist_contract, cached on the worker after
        # first load. Every prompt-render call reads this same instance so
        # the planner / doer / checker / triage / fixup / merge-planner
        # all see the same four-stage rubric.
        self._contract: evaluation_contract.EvaluationContract | None = None
        # Plan 33 PR-B: per-attempt cache populated by `_do_subtask`,
        # consumed by `_check_subtask` and `_triage_subtask`. See
        # `quikode.workers.subtask_execution.SubtaskExecutionMixin`.
        self._last_witness_results: dict[str, dict[str, Any]] = {}

    @property
    def _h(self) -> Any:
        """Narrow `self.handle` for type-checker happiness; asserts at call.
        Once provision runs, self.handle is set and stays set for the worker's
        lifetime. Methods called after _provision can use self._h instead of
        repeating the assert."""
        assert self.handle is not None, "_provision() must run before this method"
        return self.handle

    def _row(self) -> TaskRow:
        """Narrow `Store.get(self.node.id)` away from `dict | None`. Used after
        _provision when the row is guaranteed present."""
        row = self.store.get(self.node.id)
        assert row is not None, f"task {self.node.id!r} should be in store but isn't"
        return row

    # ----- Plan 33: EvaluationContract lifecycle -----

    def _evaluation_contract(self) -> evaluation_contract.EvaluationContract:
        """Return the per-task EvaluationContract, building+persisting it
        on first call (Plan 33 D1 lifecycle). Subsequent calls reuse the
        cached instance — same `(node, cfg)` always produces the same
        contract per the build_for invariant.

        Resilience: if a prior worker pass already persisted the contract
        (e.g. a daemon restart mid-run), we load it from disk rather than
        rebuilding — keeps the on-disk artifact authoritative.
        """
        if self._contract is not None:
            return self._contract
        try:
            self._contract = evaluation_contract.EvaluationContract.load(self.cfg.state_dir, self.node.id)
        except FileNotFoundError:
            self._contract = evaluation_contract.build_for(self.node, self.cfg)
            self._contract.persist(self.cfg.state_dir, self.node.id)
        return self._contract

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
            self._safe_crash_current(str(e))
            return WorkerOutcome(State.FAILED, str(e))
        finally:
            self._teardown()

    def _safe_crash_current(self, err: str) -> None:
        """Fire CRASH event ONLY if the task is still in an active state.

        Without this guard, an exception during cleanup or after an earlier
        failure path already moved the task to FAILED would re-fire CRASH from
        FAILED — there's no (FAILED, CRASH) transition, so apply_event would
        raise InvalidTransition, masking the original error and leaving the
        task in a confusing state. See plan 20 / 2026-05-07 incident.
        """
        try:
            cur = fsm_runtime.current_state(self.store, self.node.id)
        except Exception as exc:
            log.warning("task %s: cannot read current_state in crash guard: %s", self.node.id, exc)
            cur = None
        if cur in TERMINAL_STATES:
            log.warning(
                "task %s already in terminal %s; skipping crash_current (orig err: %s)",
                self.node.id,
                cur,
                err[:200],
            )
            return
        try:
            fsm_runtime.crash_current(self.store, self.node.id, note=err, last_error=err[:1000])
        except Exception as exc:
            log.warning("task %s: crash_current itself raised %s; suppressing", self.node.id, exc)

    # ----- phase: provision -----

    def _provision(self, *, provision_worktree: bool = True) -> None:
        """Stand up the worktree (optional) and dev container for a task.

        With `provision_worktree=True` (default), creates a fresh worktree
        and branch as the run() entry-point flow expects. With `=False`,
        skips worktree creation and reuses whatever the task row already has
        — used by `run_review_response()` so the response cycle inherits the
        existing branch + PR.
        """
        if fsm_runtime.current_state(self.store, self.node.id) is State.PENDING:
            fsm_runtime.start_task(self.store, self.node.id, note="creating worktree + container")
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

        # Plan 32: multi-parent children resolve to a merge-node. The
        # orchestrator stamps `parent_task_ids` / `parent_branches` from the
        # source DAG parents; at provisioning time, when `len(parent_task_ids)
        # > 1` we look up the merge-node, assert it's MERGE_NODE_READY (the
        # scheduler refuses to schedule the child until then), and rewrite
        # parent_branches/parent_pr_branches to a single-element list of the
        # merge-node's branch. From this point the child sees a single
        # effective parent — all single-parent code paths apply.
        source_parent_ids = self.store.get_parent_task_ids(self.node.id)
        if len(source_parent_ids) > 1:
            self._reduce_multi_parent_to_merge_node(source_parent_ids)
        parent_branches = self.store.get_parent_branches(self.node.id)
        parent_branch: str | None = parent_branches[0] if parent_branches else None
        worktree.fetch_base(self.cfg.repo_path, self.cfg.pr_remote, self.cfg.base_branch)
        # Capture the base SHA at branch creation. Used by Phase A's
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
        # If stacking, branch off parent_branch; else the configured base.
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

    def _reduce_multi_parent_to_merge_node(self, source_parent_ids: list[str]) -> None:
        """Plan 32: rewrite a multi-parent child's parent_branches to a
        single-element list pointing at its merge-node.

        Pre-condition: the scheduler refuses to schedule a multi-parent
        child until its merge-node is `MERGE_NODE_READY`. If we encounter
        a non-ready merge-node here, that's a scheduler bug — crash so the
        operator sees the misordering rather than silently provisioning
        against the wrong base.

        Post-condition: `parent_branches` and `parent_pr_branches` on the
        child's row contain exactly one entry — the merge-node's branch.
        Single-parent code paths (cascade-on-push, rebase-to-parent-tip)
        operate on this single effective parent. The merge-node itself
        carries the original multi-parent JSON for forensics + propagation.
        """
        sorted_ids = sorted(source_parent_ids)
        mn_id = merge_node.compute_merge_node_id(sorted_ids)
        mn_row = self.store.get(mn_id)
        if mn_row is None:
            note = (
                f"task {self.node.id} has {len(sorted_ids)} parents but no "
                f"merge-node {mn_id} exists; scheduler should have created it. "
                f"Refusing to provision against an undefined effective base."
            )
            self._safe_crash_current(note)
            raise RuntimeError(note)
        mn_state = str(mn_row.get("state") or "")
        if mn_state != State.MERGE_NODE_READY.value:
            note = (
                f"task {self.node.id} merge-node {mn_id} is {mn_state!r}, "
                f"not {State.MERGE_NODE_READY.value!r}; scheduler should have "
                f"deferred provisioning until ready."
            )
            self._safe_crash_current(note)
            raise RuntimeError(note)
        mn_branch = str(mn_row.get("branch") or "")
        if not mn_branch:
            note = f"merge-node {mn_id} is READY but has no branch; data corruption?"
            self._safe_crash_current(note)
            raise RuntimeError(note)
        log.info(
            "task %s: reducing multi-parent (%s) to merge-node %s (branch=%s)",
            self.node.id,
            ",".join(sorted_ids),
            mn_id,
            mn_branch,
        )
        self.store.set_parent_chain(
            self.node.id,
            parent_task_ids=[mn_id],
            parent_branches=[mn_branch],
            parent_pr_branches=[mn_branch],
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
        sandbox = self.execution_backend.provision(self.node.id, wt_path)
        # Database setup inside the project is the doer's responsibility.
        # Whether local CI needs migrations depends on the target repo.

        self.handle = sandbox
        self.store.set_field(
            self.node.id, container_id=str(sandbox.metadata.get("container_id") or sandbox.unit_id)
        )

    # ----- phase: plan -----

    # ----- phase: subtask loop (v2 Phase 0) -----

    # ----- subtask-level doer / checker / triage -----

    # ----- v3 Phase A: per-subtask commit + pre-commit gate -----

    # ----- final monolithic check -----

    # ----- fixup decomposition (used by audit-gauntlet failures + CI-fix dispatch) -----

    # ----- phase: commit + push -----

    # ----- v3.6 phase: pre-PR pipeline (local-CI + 3 audits) -----

    # ----- phase: open PR + poll -----

    # ----- intent gap detection -----

    # ----- rebase + conflict resolution -----

    # ----- helpers -----

    def _teardown(self) -> None:
        if self.handle is not None:
            self.execution_backend.teardown(self._h)
        wt = self.store.get(self.node.id)
        if wt and wt.get("worktree_path"):
            row = self._row()
            # Keep the worktree if the user might want to inspect — that's
            # PENDING_CI (success path waiting on review/merge), BLOCKED (we gave
            # up but the work-in-progress may be salvageable), and FAILED (a
            # crash mid-flight; the partial state is debugging gold). Clean it
            # only on MERGED (work has been merged upstream so the diff is
            # captured there) and ABORTED (user cancelled).
            cleanup_states = (State.MERGED.value, State.ABORTED.value)
            if row["state"] in cleanup_states:
                worktree.remove_worktree(self.cfg.repo_path, Path(str(row["worktree_path"])), force=True)

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


def _last_lines(s: str, n: int) -> str:
    lines = s.splitlines()
    return "\n".join(lines[-n:])


def synthesize_node_for_runtime_task(store: Store, task_id: str) -> Node:
    """Build a minimal `Node` object for a runtime-created task (merge-node)
    that has no DAG entry. Most fields are blank; the worker only really
    reads `id` and `title`. `expected_evidence` and `depends_on` come from
    the store row's parent_task_ids (sorted) so audit/scheduler stays sane.
    """
    row = store.get(task_id) or {}
    parent_ids = store.get_parent_task_ids(task_id) if row else []
    return Node(
        id=task_id,
        kind=str(row.get("kind") or "merge"),
        milestone="",
        title=f"merge-node integrating {','.join(parent_ids)}",
        scope="",
        depends_on=tuple(parent_ids),
        completes_behaviors=(),
        supports_behaviors=(),
        boundary_with_neighbors="",
        expected_evidence=(),
        playbook=(),
        rationale="",
        risks=(),
        raw={},
    )


def _extract_root_cause(checker_output: str) -> str:
    """Pull the `ROOT_CAUSE:` line(s) out of a checker / triage agent
    output. Falls back to the first ~400 chars when no marker is present
    so the progress agent still sees *something* useful.

    The subtask-checker prompt is asked to emit `VERDICT: ...` and a
    `ROOT_CAUSE: ...` block — same convention as the monolithic checker.
    """
    if not checker_output:
        return ""
    m = re.search(r"ROOT_CAUSE:\s*(.+?)(?:\n[A-Z_]+:|\Z)", checker_output, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()[:600]
    return checker_output.strip()[:400]
