"""Helpers for the `qk replan-cycle` lifecycle command (plan 52).

`qk replan-cycle <task>` is the missing primitive between `qk rewind`
(reset one subtask) and `qk retry` (torch the entire task). It targets
the most-recent planning cycle: deletes only those subtask rows,
force-pushes the branch back to before the first cycle-N commit, and
sets a hint so the worker re-fires the matching planner phase. Earlier
cycles' commits + retry counters survive.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from typing import Any

from .cli_context import Path, State, Store, console, typer

RunCommand = Callable[..., Any]


def validate_replan_cycle_inputs(
    store: Store,
    task_id: str,
    *,
    run_cmd: RunCommand,
) -> dict[str, Any]:
    """Resolve everything `replan-cycle` needs.

    Raises typer.Exit on any pre-condition that prevents the operation.
    Returns: worktree_path, branch, target_cycle, target_kind,
    cycle_subtasks, target_sha.
    """
    row = store.get(task_id)
    if not row:
        console.print(f"[red]no such task: {task_id}[/]")
        raise typer.Exit(1)
    state = row.get("state")
    allowed = {State.BLOCKED.value, State.FAILED.value}
    if state not in allowed:
        console.print(
            f"[red]task {task_id} is in state {state!r}; replan-cycle only allowed on "
            f"BLOCKED or FAILED tasks. use `qk abort` first if needed.[/]"
        )
        raise typer.Exit(2)
    worktree_path_str = row.get("worktree_path")
    if not worktree_path_str:
        console.print(f"[red]task {task_id} has no worktree_path; cannot replan-cycle[/]")
        raise typer.Exit(2)
    worktree_path = Path(str(worktree_path_str))
    if not worktree_path.exists():
        console.print(f"[red]worktree path {worktree_path} doesn't exist on disk[/]")
        raise typer.Exit(2)
    branch = row.get("branch")
    if not branch:
        console.print(f"[red]task {task_id} has no branch recorded; cannot replan-cycle[/]")
        raise typer.Exit(2)
    target_cycle, target_kind = store.latest_planning_cycle(task_id)
    if target_cycle <= 0 or target_kind is None:
        console.print(
            f"[red]task {task_id} has no subtask rows; nothing to replan. "
            f"use `qk retry` for a full restart.[/]"
        )
        raise typer.Exit(2)
    if target_cycle == 1:
        console.print(
            f"[yellow]task {task_id} has only the initial planning cycle (1); "
            f"there is no later cycle to replan. use `qk retry` for a full "
            f"restart from a fresh planner output.[/]"
        )
        raise typer.Exit(2)
    cycle_subtasks = store.subtasks_in_cycle(task_id, target_cycle)
    if not cycle_subtasks:
        console.print(
            f"[red]task {task_id}: latest planning cycle {target_cycle} has no rows. "
            f"data inconsistency — investigate before replanning.[/]"
        )
        raise typer.Exit(2)
    target_sha = _resolve_replan_target_sha(worktree_path, cycle_subtasks, run_cmd=run_cmd)
    if target_sha is None:
        console.print(
            f"[red]could not resolve a replan target sha for {task_id} cycle "
            f"{target_cycle}: no predecessor commit available[/]"
        )
        raise typer.Exit(2)
    return {
        "worktree_path": worktree_path,
        "branch": str(branch),
        "target_cycle": target_cycle,
        "target_kind": target_kind,
        "cycle_subtasks": cycle_subtasks,
        "target_sha": target_sha,
    }


def print_replan_cycle_plan(task_id: str, plan: Mapping[str, Any]) -> None:
    cycle = plan["target_cycle"]
    kind = plan["target_kind"]
    subs = plan["cycle_subtasks"]
    console.print(f"[bold]Replan-cycle plan for {task_id}[/]")
    console.print(f"  target cycle: [cyan]{cycle}[/]  kind: [cyan]{kind}[/]")
    console.print(f"  rewind to commit: [yellow]{plan['target_sha'][:12]}[/]")
    console.print(f"  worktree: {plan['worktree_path']}")
    console.print(f"  branch: {plan['branch']}")
    console.print(f"  subtasks to delete + re-emit ({len(subs)}):")
    for s in subs:
        retries = s.get("retries") or 0
        cur_state = s.get("state")
        console.print(f"    [cyan]{s['subtask_id']}[/]  [dim]state={cur_state} retries={retries}[/]")


def apply_replan_cycle(
    store: Store,
    task_id: str,
    plan: Mapping[str, Any],
    *,
    keep_remote: bool,
    run_cmd: RunCommand,
) -> None:
    """Execute the replan-cycle: delete cycle-N rows, git reset, optional
    force-push, set the marker, transition the task to PENDING."""
    worktree_path: Path = plan["worktree_path"]
    branch: str = plan["branch"]
    target_sha: str = plan["target_sha"]
    target_cycle: int = plan["target_cycle"]
    target_kind: str = plan["target_kind"]

    console.print(f"[cyan]→ git reset --hard {target_sha[:12]}[/]")
    rc = run_cmd(
        ["git", "-C", str(worktree_path), "reset", "--hard", target_sha],
        capture_output=True,
        text=True,
    )
    if rc.returncode != 0:
        console.print(f"[red]git reset failed:[/]\n{rc.stderr.strip()}")
        raise typer.Exit(3)
    if not keep_remote:
        console.print(f"[cyan]→ git push --no-verify --force-with-lease origin {branch}[/]")
        push_rc = run_cmd(
            [
                "git",
                "-C",
                str(worktree_path),
                "push",
                "--no-verify",
                "--force-with-lease",
                "origin",
                branch,
            ],
            capture_output=True,
            env={**os.environ, "HUSKY": "0", "LEFTHOOK": "0"},
            text=True,
        )
        if push_rc.returncode != 0:
            console.print(
                "[yellow]force-push failed (continuing; the next subtask "
                "commit will hit non-fast-forward and trigger auto-rebase):[/]\n"
                f"{push_rc.stderr.strip()}"
            )

    deleted = store.delete_subtasks_in_cycle(task_id, target_cycle)
    console.print(f"  deleted {deleted} subtask row(s) at cycle {target_cycle} (kind={target_kind})")

    marker_blob = json.dumps({"cycle": int(target_cycle), "kind": str(target_kind), "ts": time.time()})
    # Clear pre_pr_audit_summary so the next pipeline cycle re-runs every
    # stage cleanly — the prior cycle's findings were against a branch
    # state that no longer exists.
    store.set_field(task_id, pre_pr_audit_summary=None)
    store.set_field(
        task_id,
        state=State.PENDING.value,
        last_error=None,
        failure_reason=None,
        container_id=None,
        resume_from_existing_subtasks=1,
        replan_cycle_marker=marker_blob,
        block_forensics=None,
    )
    console.print(
        f"[green]✓ replan {task_id}: cycle {target_cycle} (kind={target_kind}) reset — "
        f"{deleted} subtasks zeroed, branch at {target_sha[:12]}; planner re-fires on "
        f"next scheduling tick[/]"
    )


def _resolve_replan_target_sha(
    worktree_path: Path,
    cycle_subtasks: list[Any],
    *,
    run_cmd: RunCommand,
) -> str | None:
    """Find the commit BEFORE the first cycle-N subtask was committed.

    Strategy: pick the earliest cycle-N row (by `id`, since insert order
    is creation order) that carries a `commit_sha`; resolve `<sha>~1`.
    Falls back to HEAD when no row in the cycle ever committed (the
    cycle decomposed into pending rows that never landed) — in that
    case HEAD is already the predecessor and `git reset --hard HEAD`
    just clears uncommitted edits, mirroring `qk rewind`'s behavior.
    """
    earliest_with_sha = next(
        (s for s in cycle_subtasks if (s.get("commit_sha") or "").strip()),
        None,
    )
    if earliest_with_sha is None:
        rc = run_cmd(
            ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        if rc.returncode != 0:
            return None
        return rc.stdout.strip() or None
    sha = str(earliest_with_sha["commit_sha"]).strip()
    rc = run_cmd(
        ["git", "-C", str(worktree_path), "rev-parse", f"{sha}~1"],
        capture_output=True,
        text=True,
    )
    if rc.returncode != 0:
        return None
    return rc.stdout.strip() or None
