"""Typer command group."""

from __future__ import annotations

from .cli_context import (
    DAG,
    State,
    Table,
    _open_store,
    app,
    console,
    docker_env,
    load_config,
    subprocess,
    time,
    typer,
)


@app.command("warm-cache")
def warm_cache(
    timeout_s: int = typer.Option(
        1800,
        "--timeout",
        help="Hard timeout for the whole warm-cache run (seconds).",
    ),
    fetch: bool = typer.Option(
        True,
        "--fetch/--no-fetch",
        help="Run `git fetch origin <base_branch>` before checkout. Disable for offline runs.",
    ),
    branch: str | None = typer.Option(
        None,
        "--branch",
        help="Branch to build (defaults to cfg.base_branch).",
    ),
) -> None:
    """Pre-warm the shared sccache by building the workspace from a clean checkout.

    Spins up a transient container against `cfg.image_tag` with the repo
    + sccache mounted, runs `cargo build --workspace --locked` against
    the head of the base branch, prints `sccache --show-stats`, and
    tears the container down. Useful nightly: the first task of the
    next day inherits a hot cache instead of paying ~15 min of cold-cache
    cost.
    """
    cfg = load_config()
    target_branch = branch or cfg.base_branch
    container_name = docker_env.start_warm_cache_container(cfg)
    console.print(
        f"[cyan]warm-cache[/] container [bold]{container_name}[/]"
        f" → branch {target_branch} (timeout {timeout_s}s)"
    )
    overall_start = time.time()
    try:
        steps: list[tuple[str, str]] = []
        if fetch:
            steps.append(("git fetch", f"git fetch origin {target_branch}"))
        steps.extend(
            [
                ("git checkout", f"git checkout origin/{target_branch}"),
                ("cargo build", "cargo build --workspace --locked"),
                ("sccache stats", "sccache --show-stats"),
            ]
        )
        for label, shell_cmd in steps:
            elapsed = time.time() - overall_start
            remaining = max(60, timeout_s - int(elapsed))
            step_start = time.time()
            console.print(f"[cyan]→[/] {label}: [dim]{shell_cmd}[/]")
            r = subprocess.run(
                [
                    "docker",
                    "exec",
                    container_name,
                    "bash",
                    "-lc",
                    f"cd /workspace && {shell_cmd}",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=remaining,
            )
            took = time.time() - step_start
            if r.returncode != 0:
                console.print(
                    f"[red]✗ {label} failed (rc={r.returncode}, {took:.1f}s):[/]\n"
                    f"{(r.stderr or r.stdout)[-1500:]}"
                )
                raise typer.Exit(code=r.returncode)
            tail = r.stdout.rstrip().splitlines()[-15:] if r.stdout else []
            if tail:
                console.print("\n".join(tail))
            console.print(f"[green]✓[/] {label} ({took:.1f}s)")
    finally:
        docker_env.teardown_warm_cache_container(container_name)
        console.print(f"[cyan]warm-cache[/] done in {time.time() - overall_start:.1f}s")


@app.command("dag-stats")
def dag_stats(
    by: str = typer.Option("milestone", "--by", help="milestone | layer"),
):
    """Per-milestone or per-layer breakdown of DAG progress. Wake-up summary view."""
    cfg = load_config()
    dag = DAG.load(cfg.dag_path)
    store = _open_store(cfg)
    state_by_id = {r["id"]: r["state"] for r in store.all_tasks()}

    def state_of(nid: str) -> str:
        return state_by_id.get(nid, State.PENDING.value)

    if by == "milestone":
        groups: dict[str, list[str]] = {}
        for nid, n in dag.nodes.items():
            groups.setdefault(n.milestone, []).append(nid)
        ordered = sorted(groups, key=lambda m: m)

        def title_for(k):
            return f"{k}  {dag.milestones.get(k, {}).get('title', '')}"
    elif by == "layer":
        layers = dag.topo_layers()
        groups = {f"layer {i:2d}": layer for i, layer in enumerate(layers)}
        ordered = list(groups)

        def title_for(k):
            return k
    else:
        console.print(f"[red]unknown --by: {by} (expected milestone | layer)[/]")
        raise typer.Exit(2)

    table = Table(show_lines=False, expand=True)
    table.add_column("Group", overflow="fold")
    table.add_column("Total", justify="right", no_wrap=True)
    table.add_column("Merged", justify="right", no_wrap=True, style="green")
    table.add_column("Awaiting", justify="right", no_wrap=True, style="bright_green")
    table.add_column("Active", justify="right", no_wrap=True, style="yellow")
    table.add_column("Blocked", justify="right", no_wrap=True, style="red")
    table.add_column("Pending", justify="right", no_wrap=True, style="dim")
    table.add_column("%", justify="right", no_wrap=True)

    grand: dict[str, int] = {"total": 0, "merged": 0, "awaiting": 0, "active": 0, "blocked": 0, "pending": 0}
    active_states = {
        State.PROVISIONING.value,
        State.PLANNING.value,
        State.COMMITTING.value,
        State.PUSHING.value,
        State.PR_OPENING.value,
        State.PENDING_CI.value,
    }
    for k in ordered:
        ids = groups[k]
        c: dict[str, int] = {"merged": 0, "awaiting": 0, "active": 0, "blocked": 0, "pending": 0}
        for nid in ids:
            st = state_of(nid)
            if st == State.MERGED.value:
                c["merged"] += 1
            elif st == State.PENDING_CI.value:
                c["awaiting"] += 1
            elif st in (State.BLOCKED.value, State.FAILED.value, State.ABORTED.value):
                c["blocked"] += 1
            elif st in active_states:
                c["active"] += 1
            else:
                c["pending"] += 1
        for k2, value in c.items():
            grand[k2] += value
        grand["total"] += len(ids)
        pct = (100 * c["merged"] // len(ids)) if ids else 0
        table.add_row(
            title_for(k),
            str(len(ids)),
            str(c["merged"]) if c["merged"] else "—",
            str(c["awaiting"]) if c["awaiting"] else "—",
            str(c["active"]) if c["active"] else "—",
            str(c["blocked"]) if c["blocked"] else "—",
            str(c["pending"]) if c["pending"] else "—",
            f"{pct}%",
        )
    table.add_section()
    table.add_row(
        "[bold]TOTAL[/]",
        str(grand["total"]),
        str(grand["merged"]),
        str(grand["awaiting"]),
        str(grand["active"]),
        str(grand["blocked"]),
        str(grand["pending"]),
        f"{(100 * grand['merged'] // grand['total']) if grand['total'] else 0}%",
    )
    console.print(table)
