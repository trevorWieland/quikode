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
from .config_validation import ConfigValidationError, validate_launch_config


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
    try:
        validate_launch_config(cfg)
    except ConfigValidationError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc

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
    """Stop the daemon supervisor + every descendant.

    SIGTERMs the supervisor and every child process it (or its children)
    forked, waits up to `--timeout-s` seconds for clean exit, then SIGKILLs
    anything still alive. Removes the pid + heartbeat files unconditionally
    on exit so `qk daemon status` and `qk reset` see a clean slate.
    """

    _setup_logging("WARNING")
    cfg = load_config()
    pid, _ = daemon_mod.read_daemon_pid(cfg)
    if pid is None:
        # No pid file — but a child may still be orphaned from a previous
        # crashed supervisor. Surface it loudly rather than exit 0/1 silently.
        _, orphans = daemon_mod.detect_orphan_quikode_runs(cfg)
        if orphans:
            console.print("[red]no daemon.pid, but orphaned quikode.cli run children detected:[/]")
            for o in orphans:
                console.print(f"  pid={o.pid} cmdline={o.short_cmdline()!r}")
            console.print("[yellow]run `kill -9 <pid>` for each, then retry[/]")
            raise typer.Exit(2)
        console.print("[yellow]no daemon running (no daemon.pid)[/]")
        raise typer.Exit(1)
    if not daemon_mod._pid_alive(pid):
        console.print(f"[yellow]daemon pid {pid} not alive — cleaning up stale pid file[/]")
        try:
            daemon_mod.daemon_pid_file(cfg).unlink()
        except OSError:
            pass
        try:
            daemon_mod.heartbeat_file(cfg).unlink()
        except OSError:
            pass
        # Same orphan check: pid file pointed somewhere dead, but child may
        # be reparented elsewhere.
        _, orphans = daemon_mod.detect_orphan_quikode_runs(cfg)
        if orphans:
            console.print("[red]orphaned quikode.cli run children still alive:[/]")
            for o in orphans:
                console.print(f"  pid={o.pid} cmdline={o.short_cmdline()!r}")
            raise typer.Exit(2)
        raise typer.Exit(1)
    console.print(f"[cyan]stopping daemon pid={pid} (timeout {timeout_s}s)...[/]")

    def _emit(msg: str) -> None:
        console.print(msg)

    ok = daemon_mod.stop_daemon(cfg, timeout_s=timeout_s, log_fn=_emit)
    if ok:
        console.print("[green]daemon stopped[/]")
        raise typer.Exit(0)
    console.print("[red]daemon did not stop cleanly — see ERROR log lines above for surviving pids[/]")
    raise typer.Exit(1)


@daemon_app.command("status")
def daemon_status(
    output_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON instead of a summary"),
):
    """Report daemon liveness + heartbeat freshness + orphan detection.

    Exit codes:
      0 — daemon alive AND heartbeat fresh
      1 — daemon not running
      2 — daemon alive but heartbeat is stale, OR
          orphan `quikode.cli run` detected (supervisor dead, child kept ticking)
    """

    _setup_logging("WARNING")
    cfg = load_config()
    s = daemon_mod.get_status(cfg)
    sup_proc, orphans = daemon_mod.detect_orphan_quikode_runs(cfg)
    if output_json:
        payload = s.to_json_dict()
        payload["orphan_quikode_runs"] = [
            {"pid": o.pid, "ppid": o.ppid, "cmdline": o.cmdline} for o in orphans
        ]
        print(json.dumps(payload, indent=2))
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
        # Orphan warnings — when supervisor is dead but child ticks on, OR
        # when the pid file is missing but heartbeat is fresh (same disease,
        # different symptom). Either way the operator needs to act.
        if orphans and not s.daemon_alive:
            console.print(
                f"[bold red]WARNING:[/] orphaned quikode child detected "
                f"({len(orphans)} pid(s)) — daemon supervisor is dead but child(ren) "
                f"still running. `kill -9 <pid>` to clean up:"
            )
            for o in orphans:
                console.print(f"  [red]pid={o.pid}[/] cmdline={o.short_cmdline()!r}")
        elif not s.daemon_alive and s.heartbeat_data is not None and not s.heartbeat_stale:
            console.print(
                "[bold red]WARNING:[/] heartbeat fresh but no supervisor in pid file — "
                "orphaned child likely. Re-check `ps -eo pid,ppid,cmd | grep quikode.cli`."
            )
        # When supervisor is healthy, sup_proc is non-None — used only to
        # silence pyright "unused"; no message needed for the OK path.
        del sup_proc
    if not s.daemon_alive:
        # Supervisor dead AND a fresh-looking heartbeat or orphan child:
        # signal "something is wrong" via exit 2 so scripts can detect.
        if orphans or (s.heartbeat_data is not None and not s.heartbeat_stale):
            raise typer.Exit(2)
        raise typer.Exit(1)
    if s.heartbeat_data is None or s.heartbeat_stale:
        raise typer.Exit(2)
    raise typer.Exit(0)
