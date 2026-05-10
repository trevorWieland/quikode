"""Unblock-context rendering helpers extracted from `cli_lifecycle`.

The `qk unblock` command prints worktree/branch/PR + block forensics for
a BLOCKED task so the operator can investigate. The rendering is purely
read-only and lives here to keep `cli_lifecycle.py` within its
architecture line budget; it has no other callers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .cli_context import (
    Store,
    console,
    os,
    subprocess,
)


def print_unblock_context(store: Store, task_id: str, row: Mapping[str, Any]) -> None:
    sub_blocked = ""
    for s in store.list_subtasks(task_id):
        if s["state"] == "blocked":
            sub_blocked = s["subtask_id"]
            break
    worktree_path = row.get("worktree_path") or "(none — task never provisioned)"
    branch = row.get("branch") or "(none)"
    pr_url = row.get("pr_url") or "(none)"
    console.print(
        f"[bold]Task {task_id} is BLOCKED[/]" + (f" at [cyan]{sub_blocked}[/]" if sub_blocked else "")
    )
    console.print(f"  Worktree: [cyan]{worktree_path}[/]")
    console.print(f"  Branch:   [cyan]{branch}[/]")
    console.print(f"  PR:       [cyan]{pr_url}[/]")
    last_err = row.get("last_error") or ""
    if last_err:
        console.print(f"\n[bold]Reason:[/] {str(last_err)[:400]}")
    _print_block_forensics(store, task_id)
    console.print("\n[bold]To unblock:[/]")
    console.print(f"  - cd {worktree_path}")
    console.print("  - investigate; commit fixes")
    console.print(f"  - run [b]quikode resume {task_id}[/] from the workspace dir to continue")


def _print_block_forensics(store: Store, task_id: str) -> None:
    forensics = store.get_block_forensics(task_id)
    if not forensics:
        return
    console.print("\n[bold]Forensics:[/]")
    _print_retry_categories(forensics)
    _print_subtask_forensics(forensics)
    last_co = (forensics.get("last_checker_outputs") or [])[:1]
    if last_co:
        excerpt = (last_co[0].get("excerpt") or "")[:300]
        console.print(f"\n  [dim]last checker output excerpt:[/]\n  {excerpt}")
    peak = forensics.get("peak_mem_bytes")
    if peak:
        console.print(f"\n  peak rss: [dim]{peak / (1024**3):.1f} GB[/]")


def _print_retry_categories(forensics: dict) -> None:
    cats = forensics.get("retry_categories_total") or {}
    if cats:
        cats_str = " ".join(f"{c}={n}" for c, n in sorted(cats.items(), key=lambda kv: -kv[1]))
        console.print(f"  retry categories: [dim]{cats_str}[/]")


def _print_subtask_forensics(forensics: dict) -> None:
    for ps in forensics.get("per_subtask") or []:
        r = ps.get("retries") or 0
        tr = ps.get("transient_retries") or 0
        fl = ps.get("flatline_count") or 0
        if r or tr or fl:
            console.print(f"  [cyan]{ps.get('subtask_id')}[/]: retries={r} transient={tr} flatline={fl}")


def launch_unblock_editor(row: Mapping[str, Any]) -> None:
    editor = os.environ.get("EDITOR") or "vi"
    wt = row.get("worktree_path")
    if not wt:
        console.print("[yellow]--edit requested but no worktree path set; skipping editor launch[/]")
        return
    try:
        subprocess.run([editor, str(wt)], check=False)
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        console.print(f"[yellow]could not launch editor {editor!r}: {e}[/]")
