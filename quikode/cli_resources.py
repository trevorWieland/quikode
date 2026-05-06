"""Typer command group."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .cli_context import (
    DAG,
    Config,
    Live,
    Path,
    State,
    Store,
    Table,
    _build_status_table,
    _compute_max_parallel,
    _dir_size,
    _humanize_bytes,
    _open_store,
    app,
    console,
    docker_env,
    json,
    load_config,
    shutil,
    time,
    typer,
    worktree,
)


@app.command()
def watch(
    refresh: float = typer.Option(2.0, "--refresh", help="Seconds between refreshes"),
    active_only: bool = typer.Option(False, "--active", help="Hide pending/merged/aborted tasks"),
):
    """Live-updating status view. Ctrl-C to exit."""
    cfg = load_config()
    store = _open_store(cfg)
    try:
        with Live(
            _build_status_table(store, show_terminal=not active_only), refresh_per_second=4, console=console
        ) as live:
            while True:
                live.update(_build_status_table(store, show_terminal=not active_only))
                time.sleep(refresh)
    except KeyboardInterrupt:
        return


@app.command()
def ready(
    output_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Print nodes whose deps are merged and which are not yet active."""
    cfg = load_config()
    store = _open_store(cfg)
    dag = DAG.load(cfg.dag_path)
    completed = store.completed_ids()
    active = store.active_ids()
    rs = dag.ready_nodes(completed_ids=completed, in_progress_ids=active)
    if output_json:
        print(
            json.dumps(
                {
                    "ready": [{"id": n.id, "title": n.title, "milestone": n.milestone} for n in rs],
                },
                indent=2,
            )
        )
        return
    if not rs:
        console.print("[yellow]nothing ready[/]")
        return
    for n in rs:
        console.print(f"[cyan]{n.id}[/]  {n.title}  (milestone {n.milestone})")


@app.command()
def resources(
    output_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Show host resources, configured per-task caps, computed max_parallel,
    and live container usage."""
    cfg = load_config()
    store = _open_store(cfg)
    host = docker_env.host_resources()
    cap, expl = _compute_max_parallel(cfg, host)
    cpus = host.get("cpus") or "?"
    mem_gb = (host.get("mem_bytes") or 0) // (1024**3)
    if output_json:
        active_states = (
            State.PROVISIONING,
            State.PLANNING,
            State.DOING_SUBTASK,
            State.CHECKING_SUBTASK,
            State.TRIAGING_SUBTASK,
            State.COMMITTING,
            State.PUSHING,
            State.PR_OPENING,
            State.PENDING_CI,
        )
        live = [
            {
                "id": r["id"],
                "state": r["state"],
                "max_rss_bytes": store.task_max_rss(r["id"]),
                "latest_stats": store.latest_container_stats(r["id"]),
            }
            for r in store.in_state(*active_states)
        ]
        print(
            json.dumps(
                {
                    "host": {"cpus": cpus, "mem_bytes": host.get("mem_bytes")},
                    "reserved": {"cpu": cfg.host_reserved_cpu, "mem_gb": cfg.host_reserved_mem_gb},
                    "per_task": {"cpu": cfg.cpu_per_task, "mem_gb": cfg.mem_per_task_gb},
                    "max_parallel_auto": cap,
                    "auto_enabled": cfg.max_parallel_auto,
                    "live": live,
                },
                indent=2,
                default=str,
            )
        )
        return

    console.print(f"[bold]Host[/]  {cpus} cpus, {mem_gb} GB RAM (per `docker info`)")
    console.print(f"[bold]Reserved for host[/]  {cfg.host_reserved_cpu} cpus, {cfg.host_reserved_mem_gb} GB")
    console.print(
        f"[bold]Per-task cap[/]  {cfg.cpu_per_task} cpus, {cfg.mem_per_task_gb} GB "
        f"({'enforced' if cfg.cpu_per_task > 0 else 'unlimited'})"
    )
    console.print(f"[bold]Max parallel (auto)[/]  [cyan]{cap}[/]")
    console.print(f"  [dim]{expl}[/]")
    if cfg.max_parallel_auto:
        console.print(
            f"[green]auto enabled[/] — `quikode run` will use {cap} unless overridden by --max-parallel"
        )
    else:
        console.print(
            f"[dim]auto disabled[/] — `quikode run` will use max_parallel = {cfg.max_parallel} from config "
            "unless overridden"
        )

    # Live in-flight container usage
    actives = store.in_state(
        *[
            State.PROVISIONING,
            State.PLANNING,
            State.DOING_SUBTASK,
            State.CHECKING_SUBTASK,
            State.TRIAGING_SUBTASK,
            State.COMMITTING,
            State.PUSHING,
            State.PR_OPENING,
            State.PENDING_CI,
        ]
    )
    if actives:
        console.print("\n[bold]Live containers[/]")
        for r in actives:
            stats = store.latest_container_stats(r["id"])
            max_rss = store.task_max_rss(r["id"])
            if stats:
                used_gb = (stats["mem_bytes"] or 0) / (1024**3)
                cap_gb = cfg.mem_per_task_gb or 0
                console.print(
                    f"  [cyan]{r['id']}[/]  cpu={stats['cpu_pct']:.1f}%  "
                    f"mem={used_gb:.1f}GB/{cap_gb}GB  "
                    f"({(stats['mem_pct'] or 0):.0f}%)  "
                    f"max_rss={(max_rss or 0) / (1024**3):.1f}GB"
                )
            else:
                console.print(f"  [cyan]{r['id']}[/]  no samples yet")


@app.command()
def prune(
    sccache_max_gb: int = typer.Option(20, "--sccache-max-gb"),
    worktrees: bool = typer.Option(
        True, "--worktrees/--no-worktrees", help="Remove worktrees for terminal tasks"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
):
    """Reclaim disk: trim sccache to size cap, remove worktrees for tasks in
    terminal state (merged/blocked/aborted). Run periodically on long DAG runs."""
    cfg = load_config()
    store = _open_store(cfg)
    cache_bytes = _dir_size(cfg.sccache_dir)
    cap_bytes = sccache_max_gb * 1024 * 1024 * 1024
    terminal_rows = _terminal_worktree_rows(store) if worktrees else []
    actions = _prune_actions(cache_bytes, cap_bytes, sccache_max_gb, terminal_rows)
    if not actions:
        console.print("[green]nothing to prune[/]")
        return
    for a in actions:
        console.print(f"  · {a}")
    if not yes and not typer.confirm("Proceed?"):
        raise typer.Exit(1)
    if cache_bytes > cap_bytes:
        _clear_sccache(cfg.sccache_dir)
    if worktrees:
        _remove_terminal_worktrees(cfg, store, terminal_rows)
    worktree.prune(cfg.repo_path)


def _terminal_worktree_rows(store: Store) -> Sequence[Mapping[str, Any]]:
    terminal = (State.MERGED.value, State.BLOCKED.value, State.FAILED.value, State.ABORTED.value)
    return [
        r
        for r in store.all_tasks()
        if r.get("worktree_path") and r["state"] in terminal and Path(str(r["worktree_path"])).exists()
    ]


def _prune_actions(
    cache_bytes: int, cap_bytes: int, sccache_max_gb: int, terminal_rows: Sequence[Mapping[str, Any]]
) -> list[str]:
    actions: list[str] = []
    if cache_bytes > cap_bytes:
        actions.append(f"clear sccache ({_humanize_bytes(cache_bytes)} > {sccache_max_gb}GB cap)")
    actions.extend(f"remove worktree {r['worktree_path']} ({r['state']})" for r in terminal_rows)
    return actions


def _clear_sccache(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
            continue
        try:
            child.unlink()
        except OSError:
            pass
    console.print("[green]✓[/] cleared sccache")


def _remove_terminal_worktrees(cfg: Config, store: Store, rows: Sequence[Mapping[str, Any]]) -> None:
    for row in rows:
        worktree.remove_worktree(cfg.repo_path, Path(str(row["worktree_path"])), force=True)
        store.set_field(row["id"], worktree_path=None)
        console.print(f"[green]✓[/] removed worktree for {row['id']}")


@app.command("disk-usage")
def disk_usage():
    """Show how much disk quikode is using."""
    cfg = load_config()
    locations = [
        ("state.db", cfg.state_dir / "quikode.db"),
        ("sccache", cfg.sccache_dir),
        ("worktrees", cfg.worktree_root),
        ("logs", cfg.log_dir),
    ]
    table = Table()
    table.add_column("location")
    table.add_column("size", justify="right")
    table.add_column("path", overflow="fold")
    for name, p in locations:
        sz = _dir_size(p) if p.is_dir() else (p.stat().st_size if p.exists() else 0)
        table.add_row(name, _humanize_bytes(sz), str(p))
    console.print(table)
