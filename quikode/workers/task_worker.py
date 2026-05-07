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
    fsm_runtime,
    github,
    github_graphql,
    manual_probe,
    pre_pr_audit,
    prompts,
    retry_classify,
    scope_review,
    sound,
    stacking,
    worktree,
)
from quikode.agents import build_agent
from quikode.agents.progress import (
    ProgressAttempt,
    ProgressVerdict,
    build_progress_agent,
)
from quikode.config import Config
from quikode.dag import Node
from quikode.docker_env import TaskContainer, exec_in
from quikode.fsm import TERMINAL_STATES
from quikode.orchestration import scheduler
from quikode.state import State, Store, TaskRow
from quikode.subtask_schema import (
    Plan,
    parse_fixup_planner_output,
    parse_planner_output,
)
from quikode.types import IntentReviewOutcome, IntentVerdict, Verdict
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
    github,
    github_graphql,
    manual_probe,
    pre_pr_audit,
    prompts,
    retry_classify,
    scope_review,
    sound,
    stacking,
    worktree,
    build_agent,
    ProgressAttempt,
    ProgressVerdict,
    build_progress_agent,
    exec_in,
    scheduler,
    parse_fixup_planner_output,
    parse_planner_output,
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

    def _row(self) -> TaskRow:
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
                self._safe_crash_current(note)
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
        # 240s, not 60s: when the orchestrator brings up many containers in
        # parallel on a cold cluster, the entrypoint's auth-file copy contends
        # for I/O and routinely takes 60–120s. The probe itself is a cheap
        # `test -f /tmp/qk-ready` every 500ms, so waiting longer costs nothing
        # when the entrypoint succeeds — but a too-tight ceiling marks live,
        # working containers as FAILED and orphans them holding the budget.
        docker_env.wait_dev_ready(handle, timeout_s=240)

        # Postgres is up; database setup inside the project is the doer's responsibility (tanren
        # ships them via tanren-cli migrate up; whether `just ci` needs them
        # depends on the task — leaving this to the doer keeps provisioning fast).

        self.handle = handle
        self.store.set_field(self.node.id, container_id=cid)

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
            docker_env.teardown(self._h)
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
    `ROOT_CAUSE: ...` block — same convention as the monolithic checker.
    """
    if not checker_output:
        return ""
    m = re.search(r"ROOT_CAUSE:\s*(.+?)(?:\n[A-Z_]+:|\Z)", checker_output, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()[:600]
    return checker_output.strip()[:400]
