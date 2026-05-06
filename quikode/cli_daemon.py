"""Typer command group."""

from __future__ import annotations

from .cli_context import (
    _setup_logging,
    console,
    daemon_app,
    daemon_mod,
    json,
    load_config,
    os,
    typer,
)


def _daemon_default(
    ctx: typer.Context,
    only: list[str] = typer.Option(
        None, "--only", help="Limit to specific node IDs (forwarded to inner `quikode run`)"
    ),
    milestone: str = typer.Option(None, "--milestone", help="Limit to a milestone"),
    max_parallel: int = typer.Option(None, "--max-parallel"),
    log_level: str = typer.Option("INFO", "--log-level"),
    retry_failed: bool = typer.Option(False, "--retry-failed"),
    detach: bool = typer.Option(
        False, "--detach", "-d", help="Fork into background; survives SIGHUP from this shell"
    ),
):
    """Default action with no subcommand: start the supervisor in the foreground."""
    if ctx.invoked_subcommand is not None:
        return
    _daemon_start_impl(
        only=only,
        milestone=milestone,
        max_parallel=max_parallel,
        log_level=log_level,
        retry_failed=retry_failed,
        detach=detach,
    )


@daemon_app.command("start")
def daemon_start(
    only: list[str] = typer.Option(None, "--only", help="Limit to specific node IDs"),
    milestone: str = typer.Option(None, "--milestone", help="Limit to a milestone"),
    max_parallel: int = typer.Option(None, "--max-parallel"),
    log_level: str = typer.Option("INFO", "--log-level"),
    retry_failed: bool = typer.Option(False, "--retry-failed"),
    detach: bool = typer.Option(
        False, "--detach", "-d", help="Fork into background; survives SIGHUP from this shell"
    ),
):
    """Start the daemon (foreground supervisor that restarts `quikode run` on crash)."""
    _daemon_start_impl(
        only=only,
        milestone=milestone,
        max_parallel=max_parallel,
        log_level=log_level,
        retry_failed=retry_failed,
        detach=detach,
    )


def _daemon_start_impl(
    *,
    only: list[str] | None,
    milestone: str | None,
    max_parallel: int | None,
    log_level: str,
    retry_failed: bool,
    detach: bool = False,
) -> None:

    _setup_logging(log_level)
    cfg = load_config()
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)

    pid_path = daemon_mod.daemon_pid_file(cfg)
    existing_pid, _ = daemon_mod.read_daemon_pid(cfg)
    if existing_pid is not None and daemon_mod._pid_alive(existing_pid):
        console.print(f"[red]daemon already running with pid {existing_pid} (see {pid_path})[/]")
        raise typer.Exit(1)

    # Forward CLI args to the inner `quikode run`.
    run_args: list[str] = []
    if only:
        for o in only:
            run_args.extend(["--only", o])
    if milestone:
        run_args.extend(["--milestone", milestone])
    if max_parallel is not None:
        run_args.extend(["--max-parallel", str(max_parallel)])
    if log_level:
        run_args.extend(["--log-level", log_level])
    if retry_failed:
        run_args.append("--retry-failed")

    log_path = daemon_mod.daemon_log_file(cfg)
    if detach:
        # Fork-and-setsid so the supervisor outlives the terminal that started
        # it. The parent prints the child's pid + log path then exits 0; the
        # child re-points stdio at the daemon log and falls through into the
        # normal supervisor loop.
        child_pid = daemon_mod.detach_into_background(log_path)
        if child_pid > 0:
            console.print(f"[green]daemon detached[/], pid={child_pid}, log={log_path}")
            raise typer.Exit(0)
        # Child path: stdio is now the log file. Re-init logging so handlers
        # bind to the new stderr fd. The console's prior carriage state is
        # discarded — anything we print from here lives in the log.
        _setup_logging(log_level)
    else:
        console.print(f"[green]daemon started[/], pid={os.getpid()}, log={log_path}")

    rc = daemon_mod.supervise(cfg, run_args)
    raise typer.Exit(rc)


@daemon_app.command("stop")
def daemon_stop(
    timeout_s: int = typer.Option(30, "--timeout-s", help="SIGTERM grace period before SIGKILL"),
):
    """Send SIGTERM to a running daemon supervisor; SIGKILL if it doesn't exit in time."""

    _setup_logging("WARNING")
    cfg = load_config()
    pid, _ = daemon_mod.read_daemon_pid(cfg)
    if pid is None:
        console.print("[yellow]no daemon running (no daemon.pid)[/]")
        raise typer.Exit(1)
    if not daemon_mod._pid_alive(pid):
        console.print(f"[yellow]daemon pid {pid} not alive — cleaning up stale pid file[/]")
        try:
            daemon_mod.daemon_pid_file(cfg).unlink()
        except OSError:
            pass
        raise typer.Exit(1)
    console.print(f"[cyan]sending SIGTERM to daemon pid={pid}, waiting up to {timeout_s}s...[/]")
    ok = daemon_mod.stop_daemon(cfg, timeout_s=timeout_s)
    if ok:
        console.print("[green]daemon stopped[/]")
        raise typer.Exit(0)
    console.print("[red]daemon did not stop cleanly — investigate[/]")
    raise typer.Exit(1)


@daemon_app.command("status")
def daemon_status(
    output_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON instead of a summary"),
):
    """Report daemon liveness + heartbeat freshness.

    Exit codes:
      0 — daemon alive AND heartbeat fresh
      1 — daemon not running
      2 — daemon alive but heartbeat is stale (or missing)
    """

    _setup_logging("WARNING")
    cfg = load_config()
    s = daemon_mod.get_status(cfg)
    if output_json:
        print(json.dumps(s.to_json_dict(), indent=2))
    else:
        if s.daemon_alive:
            uptime = s.daemon_uptime_s or 0.0
            console.print(f"[green]daemon alive[/] pid={s.daemon_pid} uptime={uptime:.0f}s")
        else:
            console.print("[red]daemon not running[/]")
        if s.heartbeat_data is None:
            console.print("[yellow]no heartbeat file[/]")
        else:
            age = s.heartbeat_age_s or 0.0
            stale_tag = "[red]STALE[/]" if s.heartbeat_stale else "[green]fresh[/]"
            hb = s.heartbeat_data
            console.print(
                f"heartbeat {stale_tag} age={age:.0f}s "
                f"in_flight={hb.get('in_flight')} "
                f"pending_ci={hb.get('pending_ci')} "
                f"addressing_feedback={hb.get('addressing_feedback')}"
            )
    if not s.daemon_alive:
        raise typer.Exit(1)
    if s.heartbeat_data is None or s.heartbeat_stale:
        raise typer.Exit(2)
    raise typer.Exit(0)
