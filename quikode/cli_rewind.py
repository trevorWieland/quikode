"""Helpers for the `qk rewind` lifecycle command."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .cli_context import Path, State, Store, console, typer

RunCommand = Callable[..., Any]


def validate_rewind_inputs(
    store: Store,
    task_id: str,
    subtask_id: str,
    *,
    run_cmd: RunCommand,
) -> dict[str, Any]:
    """Resolve all inputs `rewind` needs.

    Raises typer.Exit on any pre-condition that prevents the operation.
    Returns worktree_path, branch, target, target_sha, and to_reset.
    """
    row = store.get(task_id)
    if not row:
        console.print(f"[red]no such task: {task_id}[/]")
        raise typer.Exit(1)
    state = row.get("state")
    allowed = {State.BLOCKED.value, State.FAILED.value}
    if state not in allowed:
        console.print(
            f"[red]task {task_id} is in state {state!r}; rewind only allowed on "
            f"BLOCKED or FAILED tasks. use `qk abort` first if needed.[/]"
        )
        raise typer.Exit(2)
    worktree_path_str = row.get("worktree_path")
    if not worktree_path_str:
        console.print(f"[red]task {task_id} has no worktree_path; cannot rewind[/]")
        raise typer.Exit(2)
    worktree_path = Path(str(worktree_path_str))
    if not worktree_path.exists():
        console.print(f"[red]worktree path {worktree_path} doesn't exist on disk[/]")
        raise typer.Exit(2)
    branch = row.get("branch")
    if not branch:
        console.print(f"[red]task {task_id} has no branch recorded; cannot rewind[/]")
        raise typer.Exit(2)
    subs = store.list_subtasks(task_id)
    target = next((s for s in subs if s["subtask_id"] == subtask_id), None)
    if target is None:
        console.print(f"[red]no subtask {subtask_id!r} on task {task_id}[/]")
        raise typer.Exit(1)
    target_sha = resolve_rewind_target_sha(worktree_path, target, run_cmd=run_cmd)
    if target_sha is None:
        console.print(
            f"[red]could not resolve a rewind target sha for {task_id}/{subtask_id}: "
            f"no predecessor commit available[/]"
        )
        raise typer.Exit(2)
    target_id = int(target.get("id") or 0)
    to_reset = [s for s in subs if int(s.get("id") or 0) >= target_id]
    return {
        "worktree_path": worktree_path,
        "branch": str(branch),
        "target": target,
        "target_sha": target_sha,
        "to_reset": to_reset,
    }


def print_rewind_plan(task_id: str, subtask_id: str, plan: Mapping[str, Any]) -> None:
    console.print(f"[bold]Rewind plan for {task_id}[/]")
    console.print(f"  target subtask: [cyan]{subtask_id}[/]")
    console.print(f"  rewind to commit: [yellow]{plan['target_sha'][:12]}[/]")
    console.print(f"  worktree: {plan['worktree_path']}")
    console.print(f"  branch: {plan['branch']}")
    console.print(f"  subtasks to reset to PENDING ({len(plan['to_reset'])}):")
    for s in plan["to_reset"]:
        retries = s.get("retries") or 0
        cur_state = s.get("state")
        console.print(f"    [cyan]{s['subtask_id']}[/]  [dim]state={cur_state} retries={retries}[/]")


def apply_rewind(
    store: Store,
    task_id: str,
    subtask_id: str,
    plan: Mapping[str, Any],
    *,
    keep_remote: bool,
    run_cmd: RunCommand,
) -> None:
    """Execute the rewind: git reset, optional force-push, DB resets, FSM state."""
    worktree_path: Path = plan["worktree_path"]
    branch: str = plan["branch"]
    target_sha: str = plan["target_sha"]
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
        console.print(f"[cyan]→ git push --force-with-lease origin {branch}[/]")
        push_rc = run_cmd(
            ["git", "-C", str(worktree_path), "push", "--force-with-lease", "origin", branch],
            capture_output=True,
            text=True,
        )
        if push_rc.returncode != 0:
            console.print(
                f"[yellow]force-push failed (continuing; the next subtask commit "
                f"will hit non-fast-forward and trigger auto-rebase):[/]\n"
                f"{push_rc.stderr.strip()}"
            )
    for s in plan["to_reset"]:
        store.reset_subtask_for_rewind(task_id, s["subtask_id"])
        console.print(f"  reset {task_id}/{s['subtask_id']}")
    store.set_field(task_id, pre_pr_audit_summary=None)
    store.set_field(
        task_id,
        state=State.PENDING.value,
        last_error=None,
        container_id=None,
        resume_from_existing_subtasks=1,
        block_forensics=None,
    )
    console.print(
        f"[green]✓ rewound {task_id} to before {subtask_id}; resumed[/]\n"
        f"[dim]worker will re-pick up at {subtask_id} on the next scheduling tick[/]"
    )


def resolve_rewind_target_sha(
    worktree_path: Path,
    target: Mapping[str, Any],
    *,
    run_cmd: RunCommand,
) -> str | None:
    """Resolve the commit sha the worktree should reset to."""
    target_commit = (target.get("commit_sha") or "").strip()
    if not target_commit:
        rc = run_cmd(
            ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        if rc.returncode != 0:
            return None
        return rc.stdout.strip() or None
    rc = run_cmd(
        ["git", "-C", str(worktree_path), "rev-parse", f"{target_commit}~1"],
        capture_output=True,
        text=True,
    )
    if rc.returncode != 0:
        return None
    return rc.stdout.strip() or None
