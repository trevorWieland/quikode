"""Plan 55: fresh dev container + bootstrap command per pre-PR audit cycle.

The pre-PR audit cycle is the canonical "should match GitHub" moment.
Plan 53 catches env-drift reactively at K=2 (`failure_layer="cannot_reproduce"`)
but the failure mode still happens. Plan 55 preempts: re-provision a fresh
dev container before each audit cycle, then run a project-configured
bootstrap command inside it (e.g. `pnpm install --frozen-lockfile && just
regenerate-all`). If the bootstrap produces a worktree diff (regenerated
artifacts, lockfile updates, etc.), auto-commit + push it before the
gauntlet runs — the proactive drift-preempt that ships clean state to the
PR branch.

The helpers in this module live behind `cfg.audit_fresh_container=True`;
when off, callers run the existing audit path against the existing
container (back-compat).
"""

from __future__ import annotations

import sys
from typing import Any

from quikode.state import State
from quikode.workers.outcomes import WorkerOutcome


class _TaskWorkerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.workers.task_worker"], name)


_tw = _TaskWorkerGlobals()


def reprovision_fresh_container(worker: Any) -> None:
    """Discard the current dev container and re-provision a fresh one.

    Reuses the existing PROVISIONING-state code path
    (`_provision_container` on the worker, which calls
    `execution_backend.provision`). Tears down the old sandbox first so
    the new container starts from `cfg.image_tag` with empty caches and
    fresh `node_modules` / `target/` mounts — i.e. the state a fresh
    GitHub Actions runner would see.

    Side effects: reassigns `worker.handle` and updates the persisted
    `container_id` on the task row. Subsequent audit-stage runs use the
    new sandbox transparently.
    """
    wt_path = worker._existing_worktree_path()
    if worker.handle is not None:
        try:
            worker.execution_backend.teardown(worker._h)
        except Exception as exc:
            _tw.log.warning(
                "task %s: fresh-container teardown raised %s; continuing to re-provision",
                worker.node.id,
                exc,
            )
        worker.handle = None
    worker._provision_container(wt_path)


def run_audit_bootstrap(worker: Any, *, cycle: int) -> WorkerOutcome | None:
    """Run `cfg.audit_bootstrap_command` inside the fresh container.

    Returns:
        None on success (bootstrap rc=0 or empty command).
        WorkerOutcome(BLOCKED) on bootstrap rc != 0 — distinct
            "audit_bootstrap_failed" reason so the operator can
            distinguish env-bootstrap failures from audit-content
            failures. The task is BLOCKED via `block_current` with the
            stdout/stderr excerpt as the last_error.

    If the bootstrap produces a non-empty worktree diff, the diff is
    committed + pushed with message `audit-bootstrap: cycle <N>` BEFORE
    the audit gauntlet runs. This is the proactive drift-preempt: the
    regenerated artifacts ship to the PR branch ahead of the audit so
    GitHub's fresh-runner state and the local state agree.
    """
    cmd = worker.cfg.audit_bootstrap_command
    if not cmd:
        return None
    _tw.log.info(
        "task %s: audit cycle %d bootstrap: running `%s`",
        worker.node.id,
        cycle,
        cmd,
    )
    rc, out, err = _tw.exec_in(
        worker._h,
        ["bash", "-lc", f"cd /workspace && {cmd}"],
        log_path=worker.log_path,
        timeout=900,
    )
    if rc != 0:
        # Combine the most recent ~1500 chars of stdout/stderr as the
        # operator-visible excerpt; the full output is already in the
        # task log via exec_in's tee.
        excerpt = ((out or "") + ("\n[stderr]\n" + err if err else ""))[-2000:]
        note = (
            f"audit_bootstrap_failed: cycle {cycle} bootstrap command "
            f"`{cmd}` exited rc={rc}; aborting audit cycle"
        )
        _tw.log.warning("task %s: %s", worker.node.id, note)
        _tw.fsm_runtime.block_current(
            worker.store,
            worker.node.id,
            note=note,
            last_error=(note + "\n\n" + excerpt)[:2000],
        )
        return WorkerOutcome(State.BLOCKED, note)

    # Auto-commit + push any drift the bootstrap produced.
    rc_status, status = worker._git_in_workspace(["status", "--porcelain"])
    if rc_status == 0 and status.strip():
        _tw.log.info(
            "task %s: audit-bootstrap cycle %d produced worktree drift; auto-committing",
            worker.node.id,
            cycle,
        )
        worker._git_in_workspace(["add", "-A"])
        commit_msg = f"audit-bootstrap: cycle {cycle}"
        rc_commit, commit_out = worker._git_in_workspace(["commit", "-m", commit_msg])
        if rc_commit != 0 and "nothing to commit" not in commit_out:
            note = (
                f"audit_bootstrap_failed: cycle {cycle} produced drift but "
                f"commit failed rc={rc_commit}: {commit_out[:500]}"
            )
            _tw.log.warning("task %s: %s", worker.node.id, note)
            _tw.fsm_runtime.block_current(
                worker.store,
                worker.node.id,
                note=note,
                last_error=note[:2000],
            )
            return WorkerOutcome(State.BLOCKED, note)
        branch = str(worker._row()["branch"])
        rc_push, push_out = _tw.github.push(
            worker._h, branch, remote=worker.cfg.pr_remote, log_path=worker.log_path
        )
        if rc_push != 0:
            note = f"audit_bootstrap_failed: cycle {cycle} drift commit pushed rc={rc_push}: {push_out[:500]}"
            _tw.log.warning("task %s: %s", worker.node.id, note)
            _tw.fsm_runtime.block_current(
                worker.store,
                worker.node.id,
                note=note,
                last_error=note[:2000],
            )
            return WorkerOutcome(State.BLOCKED, note)
    return None


def prepare_audit_cycle(worker: Any, *, cycle: int) -> WorkerOutcome | None:
    """Run the plan-55 fresh-container + bootstrap pipeline before a cycle.

    Called from `_run_pre_pr_pipeline` at the top of each audit cycle,
    BEFORE the worker enters `LOCAL_CI_CHECKING` / `PRE_PR_AUDITING`.
    No-op when `cfg.audit_fresh_container` is False (back-compat).
    """
    if not worker.cfg.audit_fresh_container:
        return None
    reprovision_fresh_container(worker)
    return run_audit_bootstrap(worker, cycle=cycle)
