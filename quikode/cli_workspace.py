"""Typer command group."""

from __future__ import annotations

from .cli_context import (
    Path,
    Table,
    _open_store,
    _setup_logging,
    app,
    console,
    json,
    load_config,
    notify_mod,
    subprocess,
    typer,
    workspace_mod,
)


@app.command()
def status(
    output_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON instead of a table"),
):
    """Print task states as a table (or JSON with --json)."""
    _setup_logging("WARNING")
    cfg = load_config()
    store = _open_store(cfg)
    rows = store.all_tasks()
    if output_json:
        payload = {
            "tasks": [
                {
                    "id": r["id"],
                    "state": r["state"],
                    "branch": r.get("branch"),
                    "pr_url": r.get("pr_url"),
                    "pr_number": r.get("pr_number"),
                    "ci_triage_retries": r.get("ci_triage_retries") or 0,
                    "last_error": r.get("last_error"),
                    "parent_task_ids": store.get_parent_task_ids(r["id"]),
                }
                for r in rows
            ],
        }
        print(json.dumps(payload, indent=2))
        return
    if not rows:
        console.print("[yellow]no tasks recorded yet[/]")
        return

    by_state: dict[str, int] = {}
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    summary = "  ".join(f"[bold]{s}[/]={n}" for s, n in sorted(by_state.items()))
    console.print(summary)

    table = Table(show_lines=False)
    table.add_column("ID")
    table.add_column("State")
    table.add_column("Branch")
    table.add_column("PR")
    table.add_column("Retries (do/ci/rev)")
    table.add_column("Last")
    for r in rows:
        retries = f"{r.get('ci_triage_retries') or 0}"
        pr = r.get("pr_url") or ""
        table.add_row(
            r["id"],
            r["state"],
            r.get("branch") or "",
            pr.split("/")[-1] if pr else "",
            retries,
            (r.get("last_error") or "")[:60],
        )
    console.print(table)


def _seed_from_base_impl(
    merged_nodes_file: Path | None,
    output_json: bool,
) -> None:
    cfg = load_config()
    store = _open_store(cfg)
    result = workspace_mod.seed_from_base(cfg, store, merged_nodes_file=merged_nodes_file)
    if output_json:
        print(json.dumps({"merged": result.merged, "pending": result.pending}, indent=2))
        return
    console.print(
        f"[green]seeded {len(result.merged)} merged DAG node(s) from {cfg.pr_remote}/{cfg.base_branch}[/]"
    )
    console.print(f"[cyan]{len(result.pending)} node(s) remain pending[/]")


@app.command("seed-from-base")
def seed_from_base(
    merged_nodes_file: Path | None = typer.Option(
        None,
        "--merged-nodes-file",
        help="JSON object/list of explicit merged-node evidence.",
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Seed a fresh workspace from deterministic DAG and configured base branch evidence."""
    _seed_from_base_impl(merged_nodes_file=merged_nodes_file, output_json=output_json)


@app.command("seed-from-main")
def seed_from_main(
    merged_nodes_file: Path | None = typer.Option(
        None,
        "--merged-nodes-file",
        help="JSON object/list of explicit merged-node evidence.",
    ),
    output_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """Compatibility alias for seed-from-base."""
    _seed_from_base_impl(merged_nodes_file=merged_nodes_file, output_json=output_json)


@app.command()
def tail(task_id: str, lines: int = typer.Option(80, "-n")):
    """Tail a task's log."""
    cfg = load_config()
    p = cfg.log_dir / f"{task_id}.log"
    if not p.exists():
        console.print(f"[red]no log at {p}[/]")
        raise typer.Exit(1)
    subprocess.run(["tail", "-n", str(lines), "-f", str(p)])


@app.command()
def logs(task_id: str):
    """Print the path to a task's log file (use with $(...) or pipe)."""
    cfg = load_config()
    p = cfg.log_dir / f"{task_id}.log"
    if not p.exists():
        raise typer.Exit(1)
    print(p)


@app.command("notify-test")
def notify_test():
    """Send a test settled-task notification via the configured channel(s).

    Verifies your `notify_settled_channel` / `notify_ntfy_topic` /
    `notify_slack_webhook_url` settings are correct and that the
    operator can actually receive the ping. Run this once after
    setup, then again any time you suspect delivery is broken.
    """
    cfg = load_config()
    if cfg.notify_settled_channel == "none":
        console.print(
            "[yellow]notify_settled_channel = 'none' — no channel configured.[/]\n"
            'Set `notify_settled_channel = "ntfy"` (or `slack` / `both`) '
            "in `.quikode/config.toml`."
        )
        raise typer.Exit(1)
    msg = notify_mod.SettledMessage(
        task_id="TEST-0001",
        title="quikode notify-test",
        pr_url="https://github.com/example/repo/pull/0",
        summary="this is a test notification — if you got this, delivery works",
        cost_usd=0.00,
    )
    console.print(f"[cyan]sending test notification via channel='{cfg.notify_settled_channel}'...[/]")
    ok = notify_mod.notify_settled(cfg, msg)
    if ok:
        console.print("[green]✓ delivered[/] — check your phone / Slack workspace")
    else:
        console.print(
            "[red]✗ no channel succeeded[/] — see daemon log or run with "
            "`PYTHONLOGLEVEL=DEBUG` for the HTTP details."
        )
        raise typer.Exit(2)
