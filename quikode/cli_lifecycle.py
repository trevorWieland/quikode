"""Typer command group."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import cli_replan_cycle, cli_rewind
from .cli_context import (
    Path,
    State,
    Store,
    _open_store,
    app,
    console,
    docker_env,
    fsm_runtime,
    load_config,
    subprocess,
    typer,
    worktree,
)
from .cli_unblock_context import launch_unblock_editor, print_unblock_context


@app.command()
def abort(
    task_id: str,
    reason: str | None = typer.Option(
        None, "--reason", "-r", help="Reason for the abort, recorded in state_log."
    ),
):
    """Mark a task ABORTED and tear down ONLY its container.

    The previous "heavy-handed but reliable" path called
    `docker_env.cleanup_all_quikode(cfg)` which killed every qk-* container
    in the workspace — observed live on 2026-05-04 to crash 4 unrelated
    in-flight task containers when aborting one stuck task. This version
    targets only the aborted task's container by deriving the docker
    handle from the per-task naming convention. Idempotent: if no
    container exists for the task, the teardown is a no-op.
    """
    cfg = load_config()
    store = _open_store(cfg)
    row = store.get(task_id)
    if not row:
        console.print(f"[red]no such task: {task_id}[/]")
        raise typer.Exit(1)
    note = f"aborted by user: {reason}" if reason else "aborted by user"
    fsm_runtime.abort_pending(store, task_id, note=note)
    # Per-task teardown: stop just this task's containers + network. Iterate
    # all qk-* containers matching the task's name prefix (the workspace_id
    # suffix isn't stored, so we match "qk-<task-id-slug>-...").
    slug = docker_env.slugify(task_id)
    prefix = f"qk-{slug}-"
    for c in docker_env.list_quikode_containers(label=docker_env.workspace_label(cfg)):
        if c["name"].startswith(prefix):
            subprocess.run(["docker", "rm", "-f", c["name"]], capture_output=True, text=True)
    console.print(f"[yellow]aborted {task_id}[/]")


@app.command()
def retry(
    task_id: str | None = typer.Argument(
        None,
        help="Specific task to retry. Omit and pass --all-non-merged to retry every non-merged task.",
    ),
    keep_worktree: bool = typer.Option(False, "--keep-worktree", help="Don't delete the prior worktree dir"),
    reason: str | None = typer.Option(
        None, "--reason", "-r", help="Reason for the retry, recorded in state_log."
    ),
    all_non_merged: bool = typer.Option(
        False,
        "--all-non-merged",
        help=(
            "Plan 38 §6 deploy: retry EVERY task whose state is not "
            "merged/merge_node_retired. Used at deploy time when a contract "
            "change requires re-running every in-flight task under the new "
            "schema."
        ),
    ),
):
    """Reset a BLOCKED/FAILED task back to PENDING and clean up its prior worktree.

    With `--all-non-merged`, iterates every task whose state is not
    `merged` / `merge_node_retired` and applies the same per-task
    retry. Used at deploy boundaries when a contract change requires
    re-planning across the whole workspace (Plan 38 §6).
    """
    cfg = load_config()
    store = _open_store(cfg)
    if all_non_merged:
        if task_id:
            console.print("[red]--all-non-merged is mutually exclusive with a positional task_id[/]")
            raise typer.Exit(2)
        _retry_all_non_merged(
            cfg=cfg,
            store=store,
            keep_worktree=keep_worktree,
            reason=reason,
        )
        return
    if not task_id:
        console.print("[red]retry: pass a task_id or use --all-non-merged[/]")
        raise typer.Exit(2)
    row = store.get(task_id)
    if not row:
        raise typer.Exit(1)
    _retry_one(cfg=cfg, store=store, row=row, keep_worktree=keep_worktree, reason=reason)
    console.print(f"[green]reset {task_id} → pending[/]")


# Plan 38 PR-A: states that can transition via the FSM's `retry_task` event.
# `merged` / `merge_node_retired` excluded (terminal). PENDING excluded
# (nothing planned yet). Post-PR states (PENDING_CI / AWAITING_REVIEW) require
# the operator to drain the daemon first, after which abandoned PRs land in
# BLOCKED/FAILED — caught here.
_NON_MERGED_RETRY_STATES: tuple[str, ...] = (
    State.BLOCKED.value,
    State.FAILED.value,
    State.ABORTED.value,
)


def _retry_one(
    *,
    cfg: Any,
    store: Store,
    row: Mapping[str, Any],
    keep_worktree: bool,
    reason: str | None,
) -> None:
    """Reset one task → pending. Shared by single-task retry and bulk retry."""
    task_id = row["id"]
    if not keep_worktree:
        wt_path = row.get("worktree_path")
        if wt_path and Path(wt_path).exists():
            worktree.remove_worktree(cfg.repo_path, Path(wt_path), force=True)
        branch = row.get("branch")
        if branch:
            subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=cfg.repo_path,
                capture_output=True,
                text=True,
            )
        worktree.prune(cfg.repo_path)
    note = f"manual retry: {reason}" if reason else "manual retry"
    fsm_runtime.retry_task(
        store,
        task_id,
        note=note,
        ci_triage_retries=0,
        last_error=None,
        failure_reason=None,
        branch=None,
        worktree_path=None,
        container_id=None,
        pr_url=None,
        pr_number=None,
    )


def _retry_all_non_merged(
    *,
    cfg: Any,
    store: Store,
    keep_worktree: bool,
    reason: str | None,
) -> None:
    """Bulk reset — every task whose state is not `merged` / `merge_node_retired`.

    Currently-active workers (PROVISIONING, DOING_SUBTASK, ...) are NOT
    reset by this path; the operator drains the daemon first (per Plan
    38 §6 deploy procedure).
    """
    rows = store.all_tasks()
    targets = [r for r in rows if r["state"] in _NON_MERGED_RETRY_STATES]
    if not targets:
        console.print("[yellow]no non-merged retryable tasks found[/]")
        return
    note_reason = reason or "bulk retry: --all-non-merged"
    for row in targets:
        _retry_one(
            cfg=cfg,
            store=store,
            row=row,
            keep_worktree=keep_worktree,
            reason=note_reason,
        )
    console.print(f"[green]reset {len(targets)} task(s) → pending (--all-non-merged)[/]")


@app.command()
def resume(
    task_id: str,
    reason: str | None = typer.Option(
        None, "--reason", "-r", help="Reason for the resume, recorded in state_log."
    ),
):
    """Resume a BLOCKED/FAILED task from its existing subtask state.

    Unlike `retry`, this does NOT clear the prior worktree, branch, or
    subtask rows. The worker reuses the existing worktree (preserving any
    uncommitted edits from the prior attempt), skips the planner agent,
    parses the previously stored plan_text, and the subtask loop picks up
    at the first non-DONE subtask.

    Plan 48: any subtask whose state was `blocked` at the moment of
    resume also has its retry counters and `retry_reasons` history
    cleared, so the resumed attempt does not count against the
    same-signature stop-loss that just fired. Done subtasks keep their
    audit trail untouched; pending subtasks unblocked-by-association
    have nothing to clear.

    Use this when a transient failure (network hang, timeout) crashed a
    task that had already completed real work. Use `retry` if you want a
    full fresh start (different doer model, scope change, etc.).
    """
    cfg = load_config()
    store = _open_store(cfg)
    row = store.get(task_id)
    if not row:
        console.print(f"[red]no task {task_id} in store[/]")
        raise typer.Exit(1)
    if not row.get("plan_text"):
        console.print(
            f"[red]task {task_id} has no stored plan_text — can't resume without a plan. "
            "use `quikode retry {task_id}` for a fresh attempt.[/]"
        )
        raise typer.Exit(1)
    # Sanity check: there should be subtasks rows from the prior planning.
    subs = store.list_subtasks(task_id)
    if not subs:
        console.print(
            f"[red]task {task_id} has no subtasks rows — nothing to resume from. "
            "use `quikode retry {task_id}`.[/]"
        )
        raise typer.Exit(1)
    done = sum(1 for s in subs if s["state"] == "done")
    pending = len(subs) - done
    # Reset retry counters but PRESERVE branch + worktree_path so the next
    # provision reuses the in-place changes. Set the resume marker so the
    # worker's _plan() skips the planner agent.
    base_note = "manual resume — keep worktree + plan"
    note = f"{base_note}: {reason}" if reason else base_note
    fsm_runtime.resume_task(
        store,
        task_id,
        note=note,
        ci_triage_retries=0,
        last_error=None,
        failure_reason=None,
        container_id=None,  # container is gone; let provision spin up a fresh one
        resume_from_existing_subtasks=1,
    )
    # Capture the original blocked set BEFORE the re-pend pass mutates
    # rows; the second pass below uses this to clear retry state only on
    # subtasks that the operator's resume is explicitly forgiving.
    blocked_subtask_ids = [s["subtask_id"] for s in subs if s["state"] == "blocked"]
    # Re-pend every non-done subtask. Older workspaces may contain "skipped"
    # cascade markers; those were never user intent, so resume repairs them to
    # pending alongside active/blocked subtasks.
    for s in subs:
        if s["state"] != "done":
            store.update_subtask(task_id, s["subtask_id"], state="pending")
    # Plan 48: the operator is explicitly disregarding the prior block, so
    # the resumed attempt should not inherit the stop-loss history that
    # just fired. Clear retry counters and retry_reasons on rows whose
    # original state was `blocked`. Done subtasks keep their audit trail;
    # pending subtasks unblocked-by-association have nothing to clear.
    for sid in blocked_subtask_ids:
        store.update_subtask(
            task_id,
            sid,
            retry_reasons=None,
            retries=0,
            transient_retries=0,
            flatline_count=0,
            progress_check_count=0,
        )
    console.print(
        f"[green]resume {task_id} → pending[/]  "
        f"[dim]({done} done · {pending} to redo · planner will be skipped)[/]"
    )


@app.command("reset-retries")
def reset_retries(
    task_id: str,
    subtask_id: str | None = typer.Argument(
        None,
        help="Specific subtask id; if omitted, all blocked subtasks of the task are reset.",
    ),
) -> None:
    """Zero retry counters on BLOCKED subtasks of a BLOCKED/FAILED task.

    Designed for the container-vanished cascade scenario (plan 20 /
    2026-05-07 incident): when infrastructure noise (a SIGKILL'd dev
    container, an out-of-band cleanup, etc.) burned the per-subtask
    50-attempt hard ceiling without any real doer/checker work, this
    command rolls the counters back to zero so a follow-up `qk resume`
    gives the subtask a clean budget.

    - Refuses (exit 2) on any task not currently in BLOCKED or FAILED.
    - Without `subtask_id`: targets every subtask whose state is `blocked`.
    - With `subtask_id`: targets that subtask exactly (must exist).
    - Per target, clears retry counters/history; blocked targets go pending.
    - Does NOT fire FSM events on the task row itself; follow up with
      `qk resume <task_id>` to drive the task back to PENDING.
    """
    cfg = load_config()
    store = _open_store(cfg)
    row = store.get(task_id)
    if not row:
        console.print(f"[red]no such task: {task_id}[/]")
        raise typer.Exit(1)
    state = row.get("state")
    allowed = {State.BLOCKED.value, State.FAILED.value}
    if state not in allowed:
        console.print(
            f"[red]task {task_id} is in state {state!r}; reset-retries only allowed "
            f"on BLOCKED or FAILED tasks. use `qk abort` first if needed.[/]"
        )
        raise typer.Exit(2)
    subs = store.list_subtasks(task_id)
    if subtask_id is not None:
        targets = [s for s in subs if s["subtask_id"] == subtask_id]
        if not targets:
            console.print(f"[red]no subtask {subtask_id!r} on task {task_id}[/]")
            raise typer.Exit(1)
    else:
        targets = [s for s in subs if s["state"] == "blocked"]
        if not targets:
            console.print(f"[yellow]no blocked subtasks on {task_id}; nothing to reset[/]")
            raise typer.Exit(0)
    for s in targets:
        was_blocked = s["state"] == "blocked"
        new_state = "pending" if was_blocked else s["state"]
        store.update_subtask(
            task_id,
            s["subtask_id"],
            retries=0,
            transient_retries=0,
            flatline_count=0,
            retry_reasons=None,
            progress_check_count=0,
            last_error=None,
            state=new_state,
        )
        prior_retries = s.get("retries") or 0
        console.print(
            f"[green]reset {task_id}/{s['subtask_id']}[/]  "
            f"[dim](retries {prior_retries} → 0; state {s['state']} → {new_state})[/]"
        )
    console.print(f"[cyan]done. follow up with `qk resume {task_id}` to put the task back in queue.[/]")


@app.command()
def rewind(
    task_id: str,
    subtask_id: str = typer.Argument(
        ...,
        help="Subtask to rewind to. This subtask + every subtask topologically/chronologically after it is reset to PENDING; the worktree is reset to the commit immediately before this subtask.",
    ),
    keep_remote: bool = typer.Option(
        False,
        "--keep-remote",
        help="Skip force-pushing the local rewind to the remote branch. The next subtask commit will then need to handle non-fast-forward divergence (auto-rebase).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the rewind plan without changing any state.",
    ),
) -> None:
    """Surgical recovery: rewind a BLOCKED/FAILED task to the state before a
    specific subtask started, preserving every prior subtask's commits.

    Plan 27. Replaces the older "wipe the worktree and start over" pattern
    for the toxic-subtask scenario: when one subtask's retries got burned
    on a fundamentally unrecoverable state but earlier subtasks landed
    cleanly, rewind reverts the toxic subtask only and lets the worker
    try fresh from the last known healthy commit.

    Behavior:
    - Refuses (exit 2) on tasks not in BLOCKED or FAILED. Use `qk abort`
      first if needed.
    - Refuses (exit 2) on tasks with no worktree_path on disk.
    - Reset target = the named subtask. Reset set = target plus every
      subtask whose `created_at` is at or after target's (covers spec
      successors AND any fixup subtasks added after target).
    - Worktree is reset to the commit-sha of the predecessor subtask in
      DONE state. If target itself was DONE/committed, target.commit_sha~1
      is used. If target never committed, HEAD is already the
      predecessor (uncommitted edits exist in the worktree from failed
      attempts; `git reset --hard` clears them — this is the explicit
      operator-invoked revert that the standing "never silently revert
      agent work" rule still permits).
    - `pre_pr_audit_summary` is cleared since prior cycle's findings
      were against a branch state that no longer exists.
    - Branch is force-pushed (`--force-with-lease`) unless --keep-remote.
    - Task state transitions to PENDING with `resume_from_existing_subtasks=1`
      so the worker resumes without re-running the planner.

    Examples:
      qk rewind R-0005 S-10-bdd-B-0044
      qk rewind R-0005 S-10-bdd-B-0044 --dry-run
      qk rewind R-0005 S-10-bdd-B-0044 --keep-remote
    """
    cfg = load_config()
    store = _open_store(cfg)
    plan = cli_rewind.validate_rewind_inputs(store, task_id, subtask_id, run_cmd=subprocess.run)
    cli_rewind.print_rewind_plan(task_id, subtask_id, plan)
    if dry_run:
        console.print("[dim]--dry-run: no changes made[/]")
        return
    cli_rewind.apply_rewind(
        store,
        task_id,
        subtask_id,
        plan,
        keep_remote=keep_remote,
        run_cmd=subprocess.run,
    )


@app.command("replan-cycle")
def replan_cycle(
    task_id: str,
    keep_remote: bool = typer.Option(
        False,
        "--keep-remote",
        help=(
            "Skip force-pushing the local rewind to the remote branch. "
            "The next subtask commit will then need to handle non-fast-forward "
            "divergence (auto-rebase)."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the replan-cycle plan without changing any state.",
    ),
) -> None:
    """Reset the most-recent planning cycle's subtasks and re-fire that planner.

    Plan 52. Sits between `qk rewind` (one subtask) and `qk retry` (whole
    task) in the escalation ladder. When a task has substantial earlier-
    cycle commits AND the failing subtask belongs to a fixup / replan /
    merge cycle (not the initial plan), this primitive re-decomposes
    just that cycle without torching the foundation.

    Behavior:
    - Refuses (exit 2) on tasks not in BLOCKED or FAILED.
    - Refuses (exit 2) on tasks with no worktree_path / branch on disk.
    - Refuses (exit 2) when the latest planning cycle is 1 ("initial");
      operator must opt into a full restart explicitly via `qk retry`.
    - Identifies the latest planning cycle from the new
      `subtasks.planning_cycle` column; subtasks at that cycle are the
      reset target.
    - `git reset --hard`s the worktree to the commit BEFORE the first
      cycle-N subtask landed, force-pushes the branch (unless
      --keep-remote), then DELETES all cycle-N rows so the worker's
      natural fixup / replan / merge flow re-emits them with the same
      cycle ordinal (next emission increments from N-1 back to N).
    - Sets a `replan_cycle_marker` JSON hint on the task row carrying
      `(cycle, kind, ts)` for observability.
    - Drops the task to PENDING with `resume_from_existing_subtasks=1`
      so the worker skips the initial planner and uses the preserved
      `plan_text` for cycle 1.

    Examples:
      qk replan-cycle R-0040
      qk replan-cycle R-0040 --dry-run
      qk replan-cycle R-0040 --keep-remote
    """
    cfg = load_config()
    store = _open_store(cfg)
    plan = cli_replan_cycle.validate_replan_cycle_inputs(store, task_id, run_cmd=subprocess.run)
    cli_replan_cycle.print_replan_cycle_plan(task_id, plan)
    if dry_run:
        console.print("[dim]--dry-run: no changes made[/]")
        return
    cli_replan_cycle.apply_replan_cycle(
        store,
        task_id,
        plan,
        keep_remote=keep_remote,
        run_cmd=subprocess.run,
    )


@app.command("unblock")
def unblock(
    task_id: str,
    edit: bool = typer.Option(False, "--edit", help="Launch $EDITOR on the worktree path"),
):
    """Print intervention info for a BLOCKED task: worktree, branch, PR, next steps.

    Companion to `quikode resume`: this command surfaces *where* the work is
    parked so the user can investigate / fix it locally; `quikode resume`
    then re-pends the task and the daemon picks it up. Does not mutate state.
    """
    cfg = load_config()
    store = _open_store(cfg)
    row = store.get(task_id)
    if not row:
        console.print(f"[red]no task {task_id} in store[/]")
        raise typer.Exit(1)
    state_val = row.get("state") or "?"
    if state_val != State.BLOCKED.value:
        console.print(
            f"[yellow]task {task_id} is in state '{state_val}', not 'blocked'. "
            f"unblock is a no-op for non-blocked tasks; printing context anyway.[/]"
        )
    print_unblock_context(store, task_id, row)
    if edit:
        launch_unblock_editor(row)


@app.command("mark-merged")
def mark_merged(task_ids: list[str] = typer.Argument(..., help="One or more task IDs to mark MERGED")):
    """Mark tasks as MERGED in quikode's state without running them. Useful when a node
    is already complete in the upstream repo and you want to unblock its dependents."""
    cfg = load_config()
    store = _open_store(cfg)
    for tid in task_ids:
        store.upsert_pending(tid)
        fsm_runtime.mark_merged(store, tid, note="manually marked merged via mark-merged")
        console.print(f"[green]✓[/] {tid} → merged")


@app.command("clean-containers")
def clean_containers():
    """Remove all qk-* docker containers + networks. Does not touch state."""
    cfg = load_config()
    n = docker_env.cleanup_all_quikode(cfg)
    console.print(f"[green]removed {n} containers[/]")
