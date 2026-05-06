"""Typer command group."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .cli_context import (
    DAG,
    Config,
    State,
    Store,
    _open_store,
    app,
    console,
    docker_env,
    json,
    load_config,
    subprocess,
    typer,
    worktree,
)


@app.command()
def reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    keep_db: bool = typer.Option(False, "--keep-db", help="Don't drop the SQLite store"),
    close_prs: bool = typer.Option(False, "--close-prs", help="Close any open PRs from quikode/* branches"),
):
    """Reset the workspace to a clean slate: tear down containers, drop SQLite state,
    remove worktrees, delete quikode/* branches (local + remote). Use between smoke-test
    runs."""
    cfg = load_config()
    _confirm_reset(cfg, yes=yes, keep_db=keep_db, close_prs=close_prs)
    n = docker_env.cleanup_all_quikode(cfg)
    console.print(f"[green]✓[/] removed {n} containers")
    _reset_worktrees_and_branches(cfg)
    if close_prs:
        _close_quikode_prs(cfg)
    _reset_db_and_logs(cfg, keep_db=keep_db)
    console.print("[bold green]reset complete[/]")


def _confirm_reset(cfg: Config, *, yes: bool, keep_db: bool, close_prs: bool) -> None:
    if yes:
        return
    console.print("[yellow]This will:[/]")
    console.print("  - kill all qk-* containers + networks")
    console.print(f"  - delete worktrees under {cfg.worktree_root}")
    console.print(f"  - delete every quikode/* branch in {cfg.repo_path} (local + remote)")
    if close_prs:
        console.print("  - close any open PRs from quikode/* branches")
    if not keep_db:
        console.print(f"  - delete the SQLite store at {cfg.state_dir / 'quikode.db'}")
    if not typer.confirm("Continue?"):
        raise typer.Exit(1)


def _reset_worktrees_and_branches(cfg: Config) -> None:
    if cfg.repo_path.exists():
        for sub in cfg.worktree_root.glob("*"):
            if sub.is_dir():
                worktree.remove_worktree(cfg.repo_path, sub, force=True)
        worktree.prune(cfg.repo_path)
        killed = _delete_local_quikode_branches(cfg)
        _delete_remote_quikode_branches(cfg)
        console.print(f"[green]✓[/] removed {killed} local quikode/* branches + their remote refs")


def _delete_local_quikode_branches(cfg: Config) -> int:
    r = subprocess.run(
        ["git", "branch", "--list", "quikode/*"], cwd=cfg.repo_path, capture_output=True, text=True
    )
    local_branches = [b.strip().lstrip("* ") for b in r.stdout.splitlines() if b.strip()]
    for branch in local_branches:
        subprocess.run(["git", "branch", "-D", branch], cwd=cfg.repo_path, capture_output=True, text=True)
    return len(local_branches)


def _delete_remote_quikode_branches(cfg: Config) -> None:
    r = subprocess.run(
        ["git", "ls-remote", "--heads", cfg.pr_remote, "quikode/*"],
        cwd=cfg.repo_path,
        capture_output=True,
        text=True,
    )
    for line in r.stdout.splitlines():
        if "refs/heads/" in line:
            ref = line.split("refs/heads/")[1].strip()
            subprocess.run(
                ["git", "push", cfg.pr_remote, "--delete", ref],
                cwd=cfg.repo_path,
                capture_output=True,
                text=True,
            )


def _close_quikode_prs(cfg: Config) -> None:
    r = subprocess.run(
        ["gh", "pr", "list", "--state", "open", "--head", "", "--json", "number,headRefName"],
        cwd=cfg.repo_path,
        capture_output=True,
        text=True,
    )
    try:
        prs = json.loads(r.stdout) if r.stdout else []
    except json.JSONDecodeError:
        prs = []
    for pr in prs:
        if pr.get("headRefName", "").startswith("quikode/"):
            subprocess.run(
                ["gh", "pr", "close", str(pr["number"]), "--delete-branch"],
                cwd=cfg.repo_path,
                capture_output=True,
                text=True,
            )
            console.print(f"[green]✓[/] closed PR #{pr['number']}")


def _reset_db_and_logs(cfg: Config, *, keep_db: bool) -> None:
    db = cfg.state_dir / "quikode.db"
    if not keep_db:
        for p in [db, db.with_suffix(".db-wal"), db.with_suffix(".db-shm"), db.with_suffix(".db-journal")]:
            if p.exists():
                p.unlink()
        console.print("[green]✓[/] dropped state db")
    if cfg.log_dir.exists():
        for f in cfg.log_dir.glob("*.log"):
            f.unlink()
        console.print("[green]✓[/] cleared logs")


@app.command()
def plan(
    only: list[str] = typer.Option(None, "--only"),
    milestone: str = typer.Option(None, "--milestone"),
    show_layers: bool = typer.Option(False, "--layers", help="Group by dependency depth"),
):
    """Preview what `quikode run` would schedule, without launching anything."""
    cfg = load_config()
    dag = DAG.load(cfg.dag_path)
    store = _open_store(cfg)
    scope = dag.filter(ids=only, milestone=milestone) if (only or milestone) else set(dag.nodes)
    completed = store.completed_ids() & scope
    active = store.active_ids() & scope
    ready_nodes = [
        n for n in dag.ready_nodes(completed_ids=completed, in_progress_ids=active) if n.id in scope
    ]

    by_state: dict[str, int] = {}
    for nid in scope:
        row = store.get(nid)
        s = row["state"] if row else "pending"
        by_state[s] = by_state.get(s, 0) + 1

    console.print(
        f"[bold]Scope:[/] {len(scope)} nodes"
        + (f"  ([dim]filtered from {len(dag.nodes)}[/])" if len(scope) != len(dag.nodes) else "")
    )
    console.print("  ".join(f"{s}={n}" for s, n in sorted(by_state.items())))
    console.print(f"[bold cyan]Ready right now:[/] {len(ready_nodes)} (max-parallel = {cfg.max_parallel})")
    for n in ready_nodes[:20]:
        console.print(f"  [cyan]{n.id}[/]  {n.title}  [dim]({n.milestone})[/]")
    if len(ready_nodes) > 20:
        console.print(f"  [dim]... and {len(ready_nodes) - 20} more[/]")

    if show_layers:
        console.print("\n[bold]Dependency layers:[/]")
        all_layers = dag.topo_layers()
        for i, layer in enumerate(all_layers):
            in_scope = [nid for nid in layer if nid in scope]
            if not in_scope:
                continue
            done = sum(1 for nid in in_scope if nid in completed)
            console.print(f"  [bold]layer {i:2}[/] ({done}/{len(in_scope)} merged)  width={len(in_scope)}")


@app.command()
def explain(
    task_id: str,
    output_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Show why a task is in its current state — deps, blockers, dependents."""
    cfg = load_config()
    dag = DAG.load(cfg.dag_path)
    store = _open_store(cfg)
    if task_id not in dag.nodes:
        console.print(f"[red]no such node: {task_id}[/]")
        raise typer.Exit(1)
    node = dag.nodes[task_id]
    row = store.get(task_id)
    completed = store.completed_ids()
    if output_json:
        print(json.dumps(_explain_payload(dag, store, task_id, row, completed), indent=2, default=str))
        return

    _print_explain_header(node)
    _print_explain_state(row)
    _print_explain_dependencies(dag, store, node, completed)
    _print_explain_descendants(dag, task_id)


def _explain_payload(
    dag: DAG, store: Store, task_id: str, row: Mapping[str, Any] | None, completed: set[str]
) -> dict:
    node = dag.nodes[task_id]
    deps = [
        {
            "id": dep,
            "merged": dep in completed,
            "state": (store.get(dep) or {}).get("state", State.PENDING.value),
        }
        for dep in node.depends_on
    ]
    return {
        "id": node.id,
        "title": node.title,
        "milestone": node.milestone,
        "kind": node.kind,
        "state": (row or {}).get("state"),
        "branch": (row or {}).get("branch"),
        "pr_url": (row or {}).get("pr_url"),
        "depends_on": deps,
        "blocked_by": [dep["id"] for dep in deps if not dep["merged"]],
        "descendants": sorted(dag.descendants_of(task_id)),
    }


def _print_explain_header(node) -> None:
    console.print(f"[bold cyan]{node.id}[/]  {node.title}")
    console.print(f"[dim]milestone {node.milestone}  ·  kind {node.kind}[/]")
    console.print()


def _print_explain_state(row: Mapping[str, Any] | None) -> None:
    if row:
        console.print(f"[bold]State:[/] [yellow]{row['state']}[/]")
        for k in (
            "branch",
            "pr_url",
            "ci_triage_retries",
            "last_error",
            "container_id",
        ):
            v = row.get(k)
            if v not in (None, "", 0):
                console.print(f"  {k} = {v}")
    else:
        console.print("[bold]State:[/] [dim]not yet seeded (run [cyan]quikode run[/] to schedule)[/]")
    console.print()


def _print_explain_dependencies(dag: DAG, store: Store, node, completed: set[str]) -> None:
    if node.depends_on:
        console.print("[bold]Depends on:[/]")
        for d in node.depends_on:
            dep_row = store.get(d)
            dep_state = dep_row["state"] if dep_row else "pending"
            symbol = "[green]✓[/]" if d in completed else "[yellow]·[/]"
            t = dag.nodes.get(d)
            title = t.title if t else "(missing)"
            console.print(f"  {symbol} [cyan]{d}[/]  {title}  [dim]({dep_state})[/]")
        unmet = [d for d in node.depends_on if d not in completed]
        if unmet:
            console.print(f"\n[red]Blocked by:[/] {', '.join(unmet)}")
        else:
            console.print("\n[green]All dependencies merged.[/]")
    else:
        console.print("[bold]Depends on:[/] [dim]nothing — top of DAG[/]")
    console.print()


def _print_explain_descendants(dag: DAG, task_id: str) -> None:
    descendants = dag.descendants_of(task_id)
    if descendants:
        console.print(f"[bold]This task unblocks {len(descendants)} downstream node(s)[/]")
        sample = sorted(descendants)[:8]
        for d in sample:
            t = dag.nodes.get(d)
            console.print(f"  [cyan]{d}[/]  {t.title if t else ''}")
        if len(descendants) > 8:
            console.print(f"  [dim]... and {len(descendants) - 8} more[/]")
