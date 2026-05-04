"""Typer CLI entry point."""

from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.table import Table

from . import daemon as daemon_mod
from . import docker_env, sound, worktree  # noqa: F401
from .config import DEFAULT_CONFIG_TOML, Config, find_config_root, load_config
from .dag import DAG
from .orchestrator import Orchestrator
from .state import State, Store
from .tui import run_tui

app = typer.Typer(add_completion=False, no_args_is_help=True, pretty_exceptions_show_locals=False)
console = Console()


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )


def _open_store(cfg: Config) -> Store:
    return Store(cfg.state_dir / "quikode.db")


# ----------------------------- init ----------------------------------------


@app.command()
def init(
    repo: Path = typer.Option(..., "--repo", help="Path to the target git repo"),
    dag: Path = typer.Option(..., "--dag", help="Path to the dag.json file"),
    force: bool = typer.Option(False, "--force"),
):
    """Create .quikode/config.toml in the current directory."""
    _setup_logging()
    cwd = Path.cwd()
    cfg_dir = cwd / ".quikode"
    cfg_path = cfg_dir / "config.toml"
    if cfg_path.exists() and not force:
        console.print(f"[yellow]config exists at {cfg_path}; use --force to overwrite[/]")
        raise typer.Exit(1)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    repo_abs = repo.resolve()
    dag_abs = dag.resolve()
    if not repo_abs.exists():
        console.print(f"[red]repo not found: {repo_abs}[/]")
        raise typer.Exit(2)
    if not dag_abs.exists():
        console.print(f"[red]dag not found: {dag_abs}[/]")
        raise typer.Exit(2)
    cfg_path.write_text(DEFAULT_CONFIG_TOML.format(repo_path=repo_abs, dag_path=dag_abs))
    (cfg_dir / "logs").mkdir(exist_ok=True)
    (cfg_dir / "worktrees").mkdir(exist_ok=True)
    console.print(f"[green]wrote {cfg_path}[/]")
    console.print(
        "Next: edit the config to set agent models, then `quikode doctor`, then `quikode build-image`."
    )


# ----------------------------- doctor --------------------------------------


@app.command()
def doctor():
    """Check the local environment: docker, gh auth, agent CLIs, paths."""
    _setup_logging()
    cfg = load_config()
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        if cond:
            console.print(f"[green]✓[/] {name}{(' — ' + detail) if detail else ''}")
        else:
            console.print(f"[red]✗[/] {name}{(' — ' + detail) if detail else ''}")
            ok = False

    check("docker installed", shutil.which("docker") is not None)
    r = subprocess.run(["docker", "info"], capture_output=True, text=True)
    check("docker daemon reachable", r.returncode == 0)
    check("git installed", shutil.which("git") is not None)
    check("gh installed", shutil.which("gh") is not None)
    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    check("gh authenticated", r.returncode == 0, r.stderr.strip().splitlines()[-1] if r.stderr else "")
    check("repo path exists", cfg.repo_path.exists(), str(cfg.repo_path))
    check("dag path exists", cfg.dag_path.exists(), str(cfg.dag_path))
    check("claude auth dir", cfg.claude_auth_dir.exists(), str(cfg.claude_auth_dir))
    check("codex auth dir", cfg.codex_auth_dir.exists(), str(cfg.codex_auth_dir))
    check("opencode auth dir", cfg.opencode_auth_dir.exists(), str(cfg.opencode_auth_dir))
    check("opencode config dir", cfg.opencode_config_dir.exists(), str(cfg.opencode_config_dir))
    r = subprocess.run(["docker", "image", "inspect", cfg.image_tag], capture_output=True, text=True)
    check(
        f"image {cfg.image_tag} present",
        r.returncode == 0,
        "" if r.returncode == 0 else "run `quikode build-image`",
    )
    # DAG sanity
    try:
        d = DAG.load(cfg.dag_path)
        s = d.stats()
        check(
            "dag loads",
            True,
            f"{s['node_count']} nodes, depth {s['depth']}, max width {s['max_layer_width']}",
        )
    except Exception as e:
        check("dag loads", False, str(e))

    raise typer.Exit(0 if ok else 1)


# ----------------------------- build-image ---------------------------------


@app.command("build-image")
def build_image(flavor: str = typer.Option("tanren", "--flavor", help="tanren | python")):
    """Build the dev container image. --flavor selects the Dockerfile."""
    _setup_logging()
    cfg = load_config()
    here = Path(__file__).resolve().parent.parent / "docker"
    dockerfile = {
        "tanren": here / "Dockerfile",
        "python": here / "Dockerfile.python",
    }.get(flavor)
    if dockerfile is None or not dockerfile.exists():
        console.print(f"[red]unknown flavor: {flavor}[/]")
        raise typer.Exit(2)
    cmd = ["docker", "build", "-t", cfg.image_tag, "-f", str(dockerfile), str(here)]
    console.print(f"[cyan]$ {' '.join(cmd)}[/]")
    r = subprocess.run(cmd)
    raise typer.Exit(r.returncode)


# ----------------------------- run -----------------------------------------


@app.command()
def run(
    only: list[str] = typer.Option(None, "--only", help="Limit to specific node IDs (and their deps)"),
    milestone: str = typer.Option(None, "--milestone", help="Limit to a milestone"),
    max_parallel: int = typer.Option(None, "--max-parallel"),
    log_level: str = typer.Option("INFO", "--log-level"),
    retry_failed: bool = typer.Option(
        False,
        "--retry-failed",
        help="On startup, reset any BLOCKED/FAILED tasks in scope to PENDING so they're attempted again",
    ),
):
    """Run the orchestrator. Schedules ready tasks up to --max-parallel."""
    _setup_logging(log_level)
    cfg = load_config()
    if max_parallel is not None:
        cfg.max_parallel = max_parallel
    dag = DAG.load(cfg.dag_path)
    scope = None
    if only or milestone:
        scope = dag.filter(ids=only, milestone=milestone)
        console.print(f"[cyan]scope: {len(scope)} nodes (incl. transitive deps)[/]")
    else:
        console.print(f"[cyan]scope: all {len(dag.nodes)} nodes[/]")
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    cfg.worktree_root.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.sccache_dir.mkdir(parents=True, exist_ok=True)

    # Resources: optionally compute max_parallel from host headroom
    if cfg.max_parallel_auto and max_parallel is None:
        host = docker_env.host_resources()
        cap, expl = _compute_max_parallel(cfg, host)
        cfg.max_parallel = cap
        console.print(f"[cyan]max_parallel auto:[/] {cap}  [dim]({expl})[/]")

    # Reconcile any stranded containers from a previous crashed run
    n = docker_env.cleanup_all_quikode(cfg)
    if n:
        console.print(f"[yellow]cleaned up {n} stranded qk-* containers[/]")

    # Reconcile stale worktree dirs left over from a crashed prior run.
    # Git's worktree subsystem and the on-disk dir layout can drift if a
    # `quikode run` is killed mid-provision; this brings them back in sync.
    try:
        pruned = worktree.prune_stale_worktrees(cfg.repo_path, cfg.worktree_root)
        if pruned:
            console.print(f"[yellow]pruned {len(pruned)} stale worktree dir(s)[/]")
    except Exception as e:
        # Pruning is best-effort: never block startup on it.
        console.print(f"[yellow]worktree prune skipped: {e}[/]")

    store = _open_store(cfg)

    # Orphan recovery: any task in an active state when we started had a
    # worker driving it, but that orchestrator process is gone. Reset to
    # PENDING (with the resume marker so the worker picks up where it
    # left off where possible) or AWAITING_MERGE for PR-already-open
    # cases. Do this BEFORE constructing the orchestrator so its first
    # `_pick_next` sees a sane store.
    recovered = store.recover_orphan_tasks()
    if recovered:
        for tid, frm, to in recovered:
            console.print(f"[yellow]orphan recovery:[/] {tid}: {frm} → {to}")
        console.print(f"[yellow]recovered {len(recovered)} orphan task(s) from prior run[/]")

    # Optional: reset BLOCKED/FAILED tasks in scope so they get re-attempted.
    # Useful for "auto-retry overnight" loops.
    if retry_failed:
        reset_count = 0
        terminal_to_retry = (State.BLOCKED.value, State.FAILED.value, State.ABORTED.value)
        for r in store.all_tasks():
            if r["state"] in terminal_to_retry and (scope is None or r["id"] in scope):
                wt = r.get("worktree_path")
                if wt and Path(wt).exists():
                    worktree.remove_worktree(cfg.repo_path, Path(wt), force=True)
                if r.get("branch"):
                    subprocess.run(
                        ["git", "branch", "-D", r["branch"]],
                        cwd=cfg.repo_path,
                        capture_output=True,
                        text=True,
                    )
                store.transition(
                    r["id"],
                    State.PENDING,
                    note="auto retry-failed",
                    do_check_retries=0,
                    ci_triage_retries=0,
                    review_triage_retries=0,
                    last_error=None,
                    branch=None,
                    worktree_path=None,
                    container_id=None,
                    pr_url=None,
                    pr_number=None,
                )
                reset_count += 1
        if reset_count:
            worktree.prune(cfg.repo_path)
            console.print(f"[yellow]auto-retry: reset {reset_count} blocked/failed task(s) to pending[/]")

    # Print scope summary so the user knows exactly what's about to happen
    actual_scope = scope if scope is not None else set(dag.nodes)
    completed = store.completed_ids() & actual_scope
    by_state: dict[str, int] = {}
    for nid in actual_scope:
        r = store.get(nid)
        s = r["state"] if r else "pending"
        by_state[s] = by_state.get(s, 0) + 1
    ready_now = [
        n for n in dag.ready_nodes(completed_ids=completed, in_progress_ids=set()) if n.id in actual_scope
    ]
    summary = "  ".join(f"{s}={cnt}" for s, cnt in sorted(by_state.items()))
    console.print(
        f"[bold]start:[/] {summary}  |  [cyan]{len(ready_now)} ready now[/]  |  max-parallel {cfg.max_parallel}"
    )
    if not ready_now:
        console.print(
            "[yellow]nothing ready to schedule. (use `quikode plan` to see what's blocked, or `quikode retry <id>` to reset failed tasks.)[/]"
        )

    # Write a PID file so the TUI (and `quikode stop` later) can detect
    # this orchestrator is alive — applies whether started from CLI or via
    # the TUI's /run. atexit cleans up on normal shutdown; signal handlers
    # cover SIGTERM/SIGINT-driven shutdown via /stop or `quikode stop`.
    pid_file = cfg.state_dir / "orchestrator.pid"
    # Refuse to start if a fresh PID is already on disk — another `quikode
    # run` may be alive. The daemon supervisor has its own pid file; this
    # guards the direct-CLI path from accidental dual orchestration.
    if pid_file.exists():
        try:
            content = pid_file.read_text().strip()
            ts = float(content.rsplit("@", 1)[-1]) if "@" in content else 0.0
        except (OSError, ValueError):
            ts = 0.0
        if ts and time.time() - ts < 60:
            console.print(
                f"[red]another orchestrator pid file is fresh ({pid_file}); "
                f"refusing to start a second one. Wait 60s or remove the file if stale.[/]"
            )
            raise typer.Exit(1)
    pid_file.write_text(f"{os.getpid()}@{time.time():.0f}\n")

    def _cleanup_pid():
        try:
            pid_file.unlink()
        except OSError:
            pass

    atexit.register(_cleanup_pid)

    orch = Orchestrator(cfg, dag, store, task_filter=scope)

    # SIGTERM/SIGINT install AFTER the orchestrator exists: the daemon
    # supervisor sends SIGTERM for clean shutdown; we want it to set the
    # orchestrator's stop event so in-flight workers get a chance to wind
    # down (the ThreadPoolExecutor's __exit__ then waits for them to exit
    # the main loop). The daemon supervisor's clean-exit branch keys off
    # rc=0 here, so SIGTERM-triggered shutdown returns 0 too.
    def _request_stop(_signum, _frame):
        try:
            console.print("[yellow]received stop signal — winding down...[/]")
        except Exception:
            pass
        orch.stop()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    try:
        orch.run()
    except KeyboardInterrupt:
        console.print("[yellow]stopping...[/]")
        orch.stop()
    finally:
        _cleanup_pid()


# ----------------------------- daemon supervisor ---------------------------

daemon_app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    invoke_without_command=True,
    help="Run/manage the orchestrator under a crash-restart supervisor.",
)
app.add_typer(daemon_app, name="daemon")


@daemon_app.callback()
def _daemon_default(
    ctx: typer.Context,
    only: list[str] = typer.Option(
        None, "--only", help="Limit to specific node IDs (forwarded to inner `quikode run`)"
    ),
    milestone: str = typer.Option(None, "--milestone", help="Limit to a milestone"),
    max_parallel: int = typer.Option(None, "--max-parallel"),
    log_level: str = typer.Option("INFO", "--log-level"),
    retry_failed: bool = typer.Option(False, "--retry-failed"),
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
    )


@daemon_app.command("start")
def daemon_start(
    only: list[str] = typer.Option(None, "--only", help="Limit to specific node IDs"),
    milestone: str = typer.Option(None, "--milestone", help="Limit to a milestone"),
    max_parallel: int = typer.Option(None, "--max-parallel"),
    log_level: str = typer.Option("INFO", "--log-level"),
    retry_failed: bool = typer.Option(False, "--retry-failed"),
):
    """Start the daemon (foreground supervisor that restarts `quikode run` on crash)."""
    _daemon_start_impl(
        only=only,
        milestone=milestone,
        max_parallel=max_parallel,
        log_level=log_level,
        retry_failed=retry_failed,
    )


def _daemon_start_impl(
    *,
    only: list[str] | None,
    milestone: str | None,
    max_parallel: int | None,
    log_level: str,
    retry_failed: bool,
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

    console.print(f"[green]daemon started[/], pid={os.getpid()}, log={daemon_mod.daemon_log_file(cfg)}")
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
                f"awaiting_merge={hb.get('awaiting_merge')} "
                f"responding_to_review={hb.get('responding_to_review')}"
            )
    if not s.daemon_alive:
        raise typer.Exit(1)
    if s.heartbeat_data is None or s.heartbeat_stale:
        raise typer.Exit(2)
    raise typer.Exit(0)


# ----------------------------- status --------------------------------------


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
                    "do_check_retries": r.get("do_check_retries") or 0,
                    "ci_triage_retries": r.get("ci_triage_retries") or 0,
                    "review_triage_retries": r.get("review_triage_retries") or 0,
                    "last_error": r.get("last_error"),
                    "parent_task_id": r.get("parent_task_id"),
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
        retries = f"{r.get('do_check_retries') or 0}/{r.get('ci_triage_retries') or 0}/{r.get('review_triage_retries') or 0}"
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


# ----------------------------- tail ---------------------------------------


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


# ----------------------------- abort / retry / clean -----------------------


@app.command()
def abort(
    task_id: str,
    reason: str | None = typer.Option(
        None, "--reason", "-r", help="Reason for the abort, recorded in state_log."
    ),
):
    """Mark a task ABORTED and tear down its container."""
    cfg = load_config()
    store = _open_store(cfg)
    row = store.get(task_id)
    if not row:
        console.print(f"[red]no such task: {task_id}[/]")
        raise typer.Exit(1)
    note = f"aborted by user: {reason}" if reason else "aborted by user"
    store.transition(task_id, State.ABORTED, note=note)
    docker_env.cleanup_all_quikode(cfg)  # heavy-handed but reliable
    console.print(f"[yellow]aborted {task_id}[/]")


@app.command()
def retry(
    task_id: str,
    keep_worktree: bool = typer.Option(False, "--keep-worktree", help="Don't delete the prior worktree dir"),
    reason: str | None = typer.Option(
        None, "--reason", "-r", help="Reason for the retry, recorded in state_log."
    ),
):
    """Reset a BLOCKED/FAILED task back to PENDING and clean up its prior worktree."""
    cfg = load_config()
    store = _open_store(cfg)
    row = store.get(task_id)
    if not row:
        raise typer.Exit(1)
    # Clean up prior worktree + branch so the next provision starts fresh
    if not keep_worktree:
        wt_path = row.get("worktree_path")
        if wt_path and Path(wt_path).exists():
            worktree.remove_worktree(cfg.repo_path, Path(wt_path), force=True)
        if row.get("branch"):
            subprocess.run(
                ["git", "branch", "-D", row["branch"]], cwd=cfg.repo_path, capture_output=True, text=True
            )
        worktree.prune(cfg.repo_path)
    note = f"manual retry: {reason}" if reason else "manual retry"
    store.transition(
        task_id,
        State.PENDING,
        note=note,
        do_check_retries=0,
        ci_triage_retries=0,
        review_triage_retries=0,
        last_error=None,
        branch=None,
        worktree_path=None,
        container_id=None,
        pr_url=None,
        pr_number=None,
    )
    console.print(f"[green]reset {task_id} → pending[/]")


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
    store.transition(
        task_id,
        State.PENDING,
        note=note,
        do_check_retries=0,
        ci_triage_retries=0,
        review_triage_retries=0,
        last_error=None,
        container_id=None,  # container is gone; let provision spin up a fresh one
        resume_from_existing_subtasks=1,
    )
    # Re-pend every non-done subtask. "skipped" is included because the worker
    # uses it as a cascade-skip marker (set by _mark_remaining_pending_as_skipped
    # when an upstream blocked) — not as an intentional user skip. Once the
    # upstream block is resolved, those downstream slices need a fresh chance.
    for s in subs:
        if s["state"] != "done":
            store.update_subtask(task_id, s["subtask_id"], state="pending")
    console.print(
        f"[green]resume {task_id} → pending[/]  "
        f"[dim]({done} done · {pending} to redo · planner will be skipped)[/]"
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

    # Pick the first BLOCKED subtask (if any) for context.
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
    console.print("\n[bold]To unblock:[/]")
    console.print(f"  - cd {worktree_path}")
    console.print("  - investigate; commit fixes")
    console.print(f"  - run [b]quikode resume {task_id}[/] from the workspace dir to continue")

    if edit:
        editor = os.environ.get("EDITOR") or "vi"
        wt = row.get("worktree_path")
        if not wt:
            console.print("[yellow]--edit requested but no worktree path set; skipping editor launch[/]")
            return
        try:
            subprocess.run([editor, str(wt)], check=False)
        except (FileNotFoundError, subprocess.SubprocessError) as e:
            console.print(f"[yellow]could not launch editor {editor!r}: {e}[/]")


@app.command("demo")
def demo(
    task_id: str,
    clean: bool = typer.Option(False, "--clean", help="If target dir exists, remove it and re-clone"),
):
    """Materialize a task's PR branch in `<repo-parent>/<repo>-demo` for hands-on testing.

    Solves "git worktree already in use": instead of attaching another
    worktree to the daemon's repo, we maintain a separate clone at a
    sibling path. Re-runs are idempotent — existing demo dirs get a fetch
    + checkout instead of a fresh clone (unless --clean is passed).
    """
    cfg = load_config()
    store = _open_store(cfg)
    row = store.get(task_id)
    if not row:
        console.print(f"[red]no task {task_id} in store[/]")
        raise typer.Exit(1)
    branch = row.get("branch")
    if not branch:
        console.print(f"[red]task {task_id} has no branch yet — has it been provisioned?[/]")
        raise typer.Exit(1)

    repo_path = cfg.repo_path
    target_dir = repo_path.parent / f"{repo_path.name}-demo"

    if clean and target_dir.exists():
        console.print(f"[yellow]--clean: removing {target_dir}[/]")
        shutil.rmtree(target_dir)

    if target_dir.exists():
        console.print(f"[cyan]demo dir exists at {target_dir}[/] — fetching + checking out [b]{branch}[/]")
        subprocess.run(["git", "fetch", "origin", branch], cwd=str(target_dir), check=False)
        rc = subprocess.run(
            ["git", "checkout", branch],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
        )
        if rc.returncode != 0:
            console.print(f"[red]git checkout failed: {rc.stderr}[/]")
            raise typer.Exit(1)
    else:
        # Determine clone URL: prefer `gh repo view --json url` (works as long
        # as we're inside a gh-authenticated checkout); fall back to reading
        # `.git/config`'s origin url.
        clone_url = _resolve_repo_clone_url(repo_path)
        if not clone_url:
            console.print("[red]could not determine clone url for the repo[/]")
            raise typer.Exit(1)
        console.print(f"[cyan]cloning[/] {clone_url} → {target_dir}")
        rc = subprocess.run(
            ["git", "clone", clone_url, str(target_dir)],
            capture_output=True,
            text=True,
        )
        if rc.returncode != 0:
            console.print(f"[red]git clone failed: {rc.stderr}[/]")
            raise typer.Exit(1)
        rc = subprocess.run(
            ["git", "checkout", branch],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
        )
        if rc.returncode != 0:
            # The branch might only exist on origin; fetch then retry.
            subprocess.run(["git", "fetch", "origin", branch], cwd=str(target_dir), check=False)
            rc = subprocess.run(
                ["git", "checkout", branch],
                cwd=str(target_dir),
                capture_output=True,
                text=True,
            )
            if rc.returncode != 0:
                console.print(f"[red]git checkout {branch} failed: {rc.stderr}[/]")
                raise typer.Exit(1)

    console.print(f"\n[bold green]demo ready[/] at [cyan]{target_dir}[/]")
    # Project-aware activation hint.
    if (target_dir / "pyproject.toml").exists() or (target_dir / "uv.lock").exists():
        console.print(f"  cd {target_dir} && uv sync && source .venv/bin/activate")
    elif (target_dir / "Cargo.toml").exists():
        console.print(f"  cd {target_dir} && cargo build")
    elif (target_dir / "package.json").exists():
        console.print(f"  cd {target_dir} && npm install")
    else:
        console.print(f"  cd {target_dir}")


def _resolve_repo_clone_url(repo_path: Path) -> str | None:
    """Best-effort: ask `gh repo view` first, fall back to `.git/config` origin url."""
    try:
        rc = subprocess.run(
            ["gh", "repo", "view", "--json", "url", "--jq", ".url"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=20,
        )
        if rc.returncode == 0 and rc.stdout.strip():
            url = rc.stdout.strip()
            # `gh repo view` returns the https URL; suffix `.git` so `git clone` is happy.
            if not url.endswith(".git"):
                url += ".git"
            return url
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    # Fallback: read git config.
    try:
        rc = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if rc.returncode == 0 and rc.stdout.strip():
            return rc.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    return None


@app.command("mark-merged")
def mark_merged(task_ids: list[str] = typer.Argument(..., help="One or more task IDs to mark MERGED")):
    """Mark tasks as MERGED in quikode's state without running them. Useful when a node
    is already complete in the upstream repo and you want to unblock its dependents."""
    cfg = load_config()
    store = _open_store(cfg)
    for tid in task_ids:
        store.upsert_pending(tid)
        store.transition(tid, State.MERGED, note="manually marked merged via mark-merged")
        console.print(f"[green]✓[/] {tid} → merged")


@app.command("clean-containers")
def clean_containers():
    """Remove all qk-* docker containers + networks. Does not touch state."""
    cfg = load_config()
    n = docker_env.cleanup_all_quikode(cfg)
    console.print(f"[green]removed {n} containers[/]")


# ----------------------------- warm-cache ----------------------------------


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


# ----------------------------- dag-stats -----------------------------------


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
        State.DOING.value,
        State.CHECKING.value,
        State.TRIAGING.value,
        State.COMMITTING.value,
        State.PUSHING.value,
        State.PR_OPENING.value,
        State.POLLING_CI.value,
    }
    for k in ordered:
        ids = groups[k]
        c: dict[str, int] = {"merged": 0, "awaiting": 0, "active": 0, "blocked": 0, "pending": 0}
        for nid in ids:
            st = state_of(nid)
            if st == State.MERGED.value:
                c["merged"] += 1
            elif st == State.AWAITING_MERGE.value:
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


# ----------------------------- watch ---------------------------------------

_SKIP_MTIME_DIRS = {
    "target",
    "node_modules",
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".uv-cache",
    ".venv",
    "dist",
    "build",
}


def _worktree_mtime(path: Path | None) -> float | None:
    """Most recent file mtime under `path`, skipping build artifact dirs.
    Returns None if path is missing or empty."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    latest = 0.0
    try:
        for root, dirs, files in __import__("os").walk(p):
            dirs[:] = [d for d in dirs if d not in _SKIP_MTIME_DIRS]
            for f in files:
                try:
                    mt = (Path(root) / f).stat().st_mtime
                    latest = max(latest, mt)
                except OSError:
                    continue
    except OSError:
        return None
    return latest if latest > 0 else None


def _last_state_change(store: Store, task_id: str) -> float | None:
    """Timestamp of the most recent transition into the current state."""
    r = store.conn.execute(
        "SELECT MAX(ts) AS ts FROM state_log WHERE task_id = ? AND to_state = "
        "(SELECT state FROM tasks WHERE id = ?)",
        (task_id, task_id),
    ).fetchone()
    return r["ts"] if r and r["ts"] else None


def _humanize_secs(s: float | None) -> str:
    if s is None:
        return "—"
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    h, rem = divmod(s, 3600)
    return f"{h}h{rem // 60:02d}m"


def _build_status_table(store: Store, *, show_terminal: bool = True) -> Table:
    rows = store.all_tasks()
    by_state: dict[str, int] = {}
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    total = len(rows)
    merged = by_state.get(State.MERGED.value, 0)
    awaiting = by_state.get(State.AWAITING_MERGE.value, 0)
    blocked = by_state.get(State.BLOCKED.value, 0) + by_state.get(State.FAILED.value, 0)
    pct = (100 * merged // total) if total else 0
    summary = f"merged={merged}/{total} ({pct}%)  awaiting={awaiting}  blocked={blocked}  " + "  ".join(
        f"{s}={n}"
        for s, n in sorted(by_state.items())
        if s not in (State.MERGED.value, State.AWAITING_MERGE.value, State.BLOCKED.value, State.FAILED.value)
    )
    table = Table(title=summary, show_lines=False, expand=True)
    table.add_column("ID", no_wrap=True)
    table.add_column("State", no_wrap=True)
    table.add_column("In state", no_wrap=True, justify="right")
    table.add_column("Worktree edit", no_wrap=True, justify="right")
    table.add_column("Branch / PR", overflow="fold")
    table.add_column("D/Ci/Rv", no_wrap=True)
    table.add_column("Note", overflow="fold")
    color = {
        State.MERGED.value: "green",
        State.AWAITING_MERGE.value: "bright_green",
        State.BLOCKED.value: "red",
        State.FAILED.value: "red",
        State.ABORTED.value: "dim",
        State.PENDING.value: "dim",
    }
    now = time.time()
    for r in rows:
        st = r["state"]
        if not show_terminal and st in (State.MERGED.value, State.PENDING.value, State.ABORTED.value):
            continue
        c = color.get(st, "yellow")
        last_change = _last_state_change(store, r["id"])
        in_state = (now - last_change) if last_change else None
        wt_mt = _worktree_mtime(Path(r["worktree_path"])) if r.get("worktree_path") else None
        wt_age = (now - wt_mt) if wt_mt else None
        # red flag: in active state but worktree quiet for >5 min
        wt_color = "white"
        if wt_age is not None and st in (State.DOING.value, State.CHECKING.value, State.TRIAGING.value):
            if wt_age > 300:
                wt_color = "red"
            elif wt_age > 120:
                wt_color = "yellow"
        retries = f"{r.get('do_check_retries') or 0}/{r.get('ci_triage_retries') or 0}/{r.get('review_triage_retries') or 0}"
        pr = r.get("pr_url") or ""
        pr_n = pr.rsplit("/", 1)[-1] if pr else ""
        branch_pr = r.get("branch") or ""
        if pr_n:
            branch_pr += f" → PR #{pr_n}"
        # state-elapsed colour: stale yellow at 10min, red at 30min for active states
        in_state_color = "white"
        if in_state and st in (
            State.DOING.value,
            State.CHECKING.value,
            State.TRIAGING.value,
            State.PLANNING.value,
        ):
            if in_state > 1800:
                in_state_color = "red"
            elif in_state > 600:
                in_state_color = "yellow"
        table.add_row(
            r["id"],
            f"[{c}]{st}[/]",
            f"[{in_state_color}]{_humanize_secs(in_state)}[/]",
            f"[{wt_color}]{_humanize_secs(wt_age)}[/]",
            branch_pr,
            retries,
            (r.get("last_error") or "")[:60],
        )
    return table


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


# ----------------------------- ready ---------------------------------------


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


# ----------------------------- resources -----------------------------------


def _compute_max_parallel(cfg: Config, host: dict) -> tuple[int, str]:
    """Return (max_parallel, explanation) given config + host info.
    Computes ceil from cpu and mem budgets, takes the min."""
    cpus = host.get("cpus") or 1
    mem_bytes = host.get("mem_bytes") or 0
    mem_gb = mem_bytes // (1024**3) if mem_bytes else 0
    avail_cpu = max(0, cpus - cfg.host_reserved_cpu)
    avail_mem = max(0, mem_gb - cfg.host_reserved_mem_gb)
    by_cpu = avail_cpu // max(cfg.cpu_per_task, 1)
    by_mem = avail_mem // max(cfg.mem_per_task_gb, 1)
    cap = max(1, min(by_cpu, by_mem))
    expl = (
        f"host: {cpus} cpus, {mem_gb} GB ; reserved: {cfg.host_reserved_cpu}/"
        f"{cfg.host_reserved_mem_gb}GB ; budget: {avail_cpu}/{avail_mem}GB ; "
        f"per-task: {cfg.cpu_per_task}/{cfg.mem_per_task_gb}GB ; "
        f"⇒ {cap} (cpu-bounded={by_cpu}, mem-bounded={by_mem})"
    )
    return cap, expl


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
            State.DOING,
            State.CHECKING,
            State.TRIAGING,
            State.DOING_SUBTASK,
            State.CHECKING_SUBTASK,
            State.TRIAGING_SUBTASK,
            State.FINAL_CHECKING,
            State.COMMITTING,
            State.PUSHING,
            State.PR_OPENING,
            State.POLLING_CI,
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
            State.DOING,
            State.CHECKING,
            State.TRIAGING,
            State.DOING_SUBTASK,
            State.CHECKING_SUBTASK,
            State.TRIAGING_SUBTASK,
            State.FINAL_CHECKING,
            State.COMMITTING,
            State.PUSHING,
            State.PR_OPENING,
            State.POLLING_CI,
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


# ----------------------------- prune ---------------------------------------


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for root, _, files in __import__("os").walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def _humanize_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


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
    actions: list[str] = []

    # 1. sccache: if over cap, blow it away (sccache rebuilds itself; nothing lost,
    #    just slower next-build for cache misses)
    cache_bytes = _dir_size(cfg.sccache_dir)
    cap_bytes = sccache_max_gb * 1024 * 1024 * 1024
    if cache_bytes > cap_bytes:
        actions.append(f"clear sccache ({_humanize_bytes(cache_bytes)} > {sccache_max_gb}GB cap)")

    # 2. terminal-task worktrees still on disk
    terminal = (State.MERGED.value, State.BLOCKED.value, State.FAILED.value, State.ABORTED.value)
    if worktrees:
        for r in store.all_tasks():
            wt = r.get("worktree_path")
            if not wt or r["state"] not in terminal:
                continue
            p = Path(wt)
            if p.exists():
                actions.append(f"remove worktree {wt} ({r['state']})")

    if not actions:
        console.print("[green]nothing to prune[/]")
        return
    for a in actions:
        console.print(f"  · {a}")
    if not yes and not typer.confirm("Proceed?"):
        raise typer.Exit(1)

    if cache_bytes > cap_bytes:
        for child in cfg.sccache_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except OSError:
                    pass
        console.print("[green]✓[/] cleared sccache")

    if worktrees:
        for r in store.all_tasks():
            wt = r.get("worktree_path")
            if not wt or r["state"] not in terminal:
                continue
            p = Path(wt)
            if p.exists():
                worktree.remove_worktree(cfg.repo_path, p, force=True)
                store.set_field(r["id"], worktree_path=None)
                console.print(f"[green]✓[/] removed worktree for {r['id']}")
    worktree.prune(cfg.repo_path)


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


# ----------------------------- reset ---------------------------------------


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
    if not yes:
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

    # 1. Containers + networks
    n = docker_env.cleanup_all_quikode(cfg)
    console.print(f"[green]✓[/] removed {n} containers")

    # 2. Worktrees + branches
    branches_killed = 0
    if cfg.repo_path.exists():
        # Best effort: remove every worktree under our worktree_root
        for sub in cfg.worktree_root.glob("*"):
            if sub.is_dir():
                worktree.remove_worktree(cfg.repo_path, sub, force=True)
        worktree.prune(cfg.repo_path)
        # Delete quikode/* branches locally and on remote
        r = subprocess.run(
            ["git", "branch", "--list", "quikode/*"],
            cwd=cfg.repo_path,
            capture_output=True,
            text=True,
        )
        local_branches = [b.strip().lstrip("* ") for b in r.stdout.splitlines() if b.strip()]
        for b in local_branches:
            subprocess.run(["git", "branch", "-D", b], cwd=cfg.repo_path, capture_output=True, text=True)
            branches_killed += 1
        # Remote branches
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
        console.print(f"[green]✓[/] removed {branches_killed} local quikode/* branches + their remote refs")

    # 3. Open PRs
    if close_prs:
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

    # 4. SQLite
    db = cfg.state_dir / "quikode.db"
    if not keep_db:
        for p in [db, db.with_suffix(".db-wal"), db.with_suffix(".db-shm"), db.with_suffix(".db-journal")]:
            if p.exists():
                p.unlink()
        console.print("[green]✓[/] dropped state db")
    # 5. Logs
    if cfg.log_dir.exists():
        for f in cfg.log_dir.glob("*.log"):
            f.unlink()
        console.print("[green]✓[/] cleared logs")

    console.print("[bold green]reset complete[/]")


# ----------------------------- plan / explain ------------------------------


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
        descendants = sorted(dag.descendants_of(task_id))
        deps = [
            {"id": d, "merged": d in completed, "state": (store.get(d) or {}).get("state", "pending")}
            for d in node.depends_on
        ]
        print(
            json.dumps(
                {
                    "id": node.id,
                    "title": node.title,
                    "milestone": node.milestone,
                    "kind": node.kind,
                    "state": (row or {}).get("state"),
                    "branch": (row or {}).get("branch"),
                    "pr_url": (row or {}).get("pr_url"),
                    "depends_on": deps,
                    "blocked_by": [d["id"] for d in deps if not d["merged"]],
                    "descendants": descendants,
                },
                indent=2,
                default=str,
            )
        )
        return

    console.print(f"[bold cyan]{node.id}[/]  {node.title}")
    console.print(f"[dim]milestone {node.milestone}  ·  kind {node.kind}[/]")
    console.print()

    if row:
        console.print(f"[bold]State:[/] [yellow]{row['state']}[/]")
        for k in (
            "branch",
            "pr_url",
            "do_check_retries",
            "ci_triage_retries",
            "review_triage_retries",
            "last_error",
            "container_id",
        ):
            v = row.get(k)
            if v not in (None, "", 0):
                console.print(f"  {k} = {v}")
    else:
        console.print("[bold]State:[/] [dim]not yet seeded (run [cyan]quikode run[/] to schedule)[/]")
    console.print()

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

    descendants = dag.descendants_of(task_id)
    if descendants:
        console.print(f"[bold]This task unblocks {len(descendants)} downstream node(s)[/]")
        sample = sorted(descendants)[:8]
        for d in sample:
            t = dag.nodes.get(d)
            console.print(f"  [cyan]{d}[/]  {t.title if t else ''}")
        if len(descendants) > 8:
            console.print(f"  [dim]... and {len(descendants) - 8} more[/]")


@app.command()
def export(
    task_id: str,
    output: Path = typer.Option(
        None, "--output", "-o", help="File to write the bundle to (default: <task-id>.review.md)"
    ),
    include_diff: bool = typer.Option(True, "--diff/--no-diff"),
):
    """Bundle everything you'd want to review for a task into one markdown file:
    planner output, doer summary, checker verdict, triage notes, full git diff,
    PR link. For human review at end of run."""
    cfg = load_config()
    store = _open_store(cfg)
    dag = DAG.load(cfg.dag_path)
    row = store.get(task_id)
    if not row:
        console.print(f"[red]no such task: {task_id}[/]")
        raise typer.Exit(1)
    node = dag.nodes.get(task_id)
    out = output or Path(f"{task_id}.review.md")

    # Latest artifact per kind
    rows = store.conn.execute(
        "SELECT kind, content, ts FROM artifacts WHERE task_id = ? ORDER BY ts DESC",
        (task_id,),
    ).fetchall()
    latest: dict[str, str] = {}
    for r in rows:
        if r["kind"] not in latest:
            latest[r["kind"]] = r["content"] or ""

    # State timeline
    log_rows = list(
        store.conn.execute(
            "SELECT from_state, to_state, note, ts FROM state_log WHERE task_id = ? ORDER BY ts",
            (task_id,),
        )
    )

    parts: list[str] = [f"# {task_id} review bundle\n"]
    if node:
        parts.append(f"**Title:** {node.title}\n")
        parts.append(f"**Milestone:** {node.milestone}\n")
        parts.append(f"**Final state:** `{row['state']}`\n")
        if row.get("pr_url"):
            parts.append(f"**PR:** {row['pr_url']}\n")
        if row.get("branch"):
            parts.append(f"**Branch:** `{row['branch']}`\n")
        parts.append("")
        parts.append("## Scope\n")
        parts.append(node.scope)
        if node.boundary_with_neighbors:
            parts.append("\n### Boundary with neighbors\n")
            parts.append(node.boundary_with_neighbors)
        if node.expected_evidence:
            parts.append("\n## Expected evidence\n")
            for ev in node.expected_evidence:
                parts.append(
                    f"- **{ev.get('kind', '')}** for {ev.get('behavior_id', '')} on "
                    f"{ev.get('interfaces', [])} — witnesses {ev.get('witnesses', [])}: "
                    f"{ev.get('description', '')}"
                )

    parts.append("\n## State timeline\n")
    parts.append("```")
    for r in log_rows:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
        note = f"  ({r['note']})" if r["note"] else ""
        parts.append(f"  {ts}  {r['from_state'] or '—':>14} → {r['to_state']}{note}")
    parts.append("```")

    # v2: subtask breakdown
    sub_rows = store.list_subtasks(task_id)
    if sub_rows:
        parts.append("\n## Subtasks\n")
        parts.append("| ID | State | Retries | Title | Files |")
        parts.append("|---|---|---|---|---|")
        for r in sub_rows:
            files = json.loads(r["files_to_touch"] or "[]")
            files_short = ", ".join(f"`{f}`" for f in files[:4])
            if len(files) > 4:
                files_short += f" (+{len(files) - 4})"
            parts.append(
                f"| {r['subtask_id']} | {r['state']} | "
                f"{r.get('retries') or 0} | {r.get('title') or ''} | {files_short} |"
            )
        # Per-subtask acceptance lists
        parts.append("\n### Subtask acceptance criteria\n")
        for r in sub_rows:
            crits = json.loads(r["acceptance"] or "[]")
            parts.append(f"\n**{r['subtask_id']}** — {r.get('title') or ''}")
            for c in crits:
                parts.append(f"- {c}")
            if r.get("triage_notes"):
                parts.append(f"\n_triage notes from last attempt:_\n```\n{r['triage_notes'][:1000]}\n```")

    if "planner_output" in latest:
        parts.append("\n## Plan (from planner agent)\n")
        parts.append(latest["planner_output"])
    if "doer_output" in latest:
        parts.append("\n## Doer summary\n")
        parts.append("```")
        parts.append(latest["doer_output"][-3000:])
        parts.append("```")
    if "checker_output" in latest:
        parts.append("\n## Checker verdict\n")
        parts.append("```")
        parts.append(latest["checker_output"])
        parts.append("```")
    if "triage_output" in latest:
        parts.append("\n## Latest triage notes\n")
        parts.append("```")
        parts.append(latest["triage_output"])
        parts.append("```")

    if include_diff and row.get("worktree_path"):
        wt = Path(row["worktree_path"])
        if wt.exists():
            parts.append("\n## Git diff (full)\n")
            parts.append("```diff")
            r = subprocess.run(
                ["git", "diff", f"{cfg.base_branch}...HEAD"],
                cwd=wt,
                capture_output=True,
                text=True,
                check=False,
            )
            parts.append(r.stdout[:200_000])
            parts.append("```")
            parts.append("\n## Files changed\n")
            r = subprocess.run(
                ["git", "diff", "--stat", f"{cfg.base_branch}...HEAD"],
                cwd=wt,
                capture_output=True,
                text=True,
                check=False,
            )
            parts.append("```")
            parts.append(r.stdout)
            parts.append("```")

    out.write_text("\n".join(parts))
    console.print(f"[green]wrote {out}[/]  ({out.stat().st_size:,} bytes)")


@app.command()
def subtasks(
    task_id: str,
    output_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """List subtasks for a task: id, state, retries, files, acceptance bullets.
    Subtasks are a v2-Phase-0 concept emitted by the planner."""
    cfg = load_config()
    store = _open_store(cfg)
    rows = store.list_subtasks(task_id)
    if output_json:
        payload = {
            "task_id": task_id,
            "subtasks": [
                {
                    "id": r["subtask_id"],
                    "state": r["state"],
                    "retries": r.get("retries") or 0,
                    "title": r.get("title"),
                    "depends_on": json.loads(r["depends_on"] or "[]"),
                    "files_to_touch": json.loads(r["files_to_touch"] or "[]"),
                    "acceptance": json.loads(r["acceptance"] or "[]"),
                    "boundary": r.get("boundary"),
                    "last_error": r.get("last_error"),
                }
                for r in rows
            ],
        }
        print(json.dumps(payload, indent=2))
        return
    if not rows:
        console.print(
            f"[yellow]no subtasks recorded for {task_id} (task hasn't reached planning, or planner pre-dates v2)[/]"
        )
        return

    state_color = {
        "pending": "dim",
        "doing": "yellow",
        "checking": "cyan",
        "triaging": "yellow",
        "done": "green",
        "blocked": "red",
        "skipped": "dim",
    }
    table = Table(show_lines=False, expand=True, title=f"Subtasks for {task_id}")
    table.add_column("ID", no_wrap=True)
    table.add_column("State", no_wrap=True)
    table.add_column("Retries", justify="right", no_wrap=True)
    table.add_column("Title", overflow="fold")
    table.add_column("Files", overflow="fold")
    table.add_column("Note", overflow="fold")
    for r in rows:
        st = r["state"]
        c = state_color.get(st, "white")
        files = json.loads(r["files_to_touch"] or "[]")
        files_short = ", ".join(files[:3]) + (f" (+{len(files) - 3} more)" if len(files) > 3 else "")
        table.add_row(
            r["subtask_id"],
            f"[{c}]{st}[/]",
            str(r.get("retries") or 0),
            r.get("title") or "",
            files_short,
            (r.get("last_error") or "")[:80],
        )
    console.print(table)


@app.command()
def show(
    task_id: str,
    full: bool = typer.Option(False, "--full", help="Print full artifact bodies instead of truncating"),
):
    """Print task summary: state timeline, agent-call cost breakdown, and the
    latest planner / checker / triage artifacts."""
    cfg = load_config()
    store = _open_store(cfg)
    row = store.get(task_id)
    if not row:
        console.print(f"[red]no such task: {task_id}[/]")
        raise typer.Exit(1)
    console.print(f"[bold cyan]{task_id}[/] — state [yellow]{row['state']}[/]")
    if row.get("branch"):
        console.print(f"  branch: {row['branch']}")
    if row.get("pr_url"):
        console.print(f"  PR: {row['pr_url']}")
    if row.get("last_error"):
        console.print(f"  [red]last_error:[/] {row['last_error']}")
    retries = (
        f"do/check {row.get('do_check_retries') or 0}, "
        f"ci-triage {row.get('ci_triage_retries') or 0}, "
        f"review-triage {row.get('review_triage_retries') or 0}"
    )
    console.print(f"  retries: {retries}")

    # State timeline
    log = list(
        store.conn.execute(
            "SELECT from_state, to_state, note, ts FROM state_log WHERE task_id = ? ORDER BY ts",
            (task_id,),
        )
    )
    if log:
        console.print("\n[bold]── state timeline ──[/]")
        prev_ts = None
        for r in log:
            ts_str = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
            dt = f" (+{int(r['ts'] - prev_ts)}s)" if prev_ts else ""
            note = f"  {r['note']}" if r["note"] else ""
            console.print(f"  {ts_str}{dt}  {r['from_state'] or '—':>14} → [cyan]{r['to_state']}[/]{note}")
            prev_ts = r["ts"]

    # Agent calls — cost breakdown
    calls = list(
        store.conn.execute(
            "SELECT phase, cli, model, rc, duration_s, tokens_used, ts "
            "FROM agent_calls WHERE task_id = ? ORDER BY ts",
            (task_id,),
        )
    )
    if calls:
        console.print("\n[bold]── agent calls ──[/]")
        total_tokens = 0
        total_secs = 0.0
        table = Table(show_header=True, expand=True)
        table.add_column("when")
        table.add_column("phase")
        table.add_column("cli/model", overflow="fold")
        table.add_column("rc", justify="right")
        table.add_column("duration", justify="right")
        table.add_column("tokens", justify="right")
        for c in calls:
            ts_str = time.strftime("%H:%M:%S", time.localtime(c["ts"]))
            tok = c["tokens_used"]
            dur = c["duration_s"]
            if tok:
                total_tokens += tok
            if dur:
                total_secs += dur
            table.add_row(
                ts_str,
                c["phase"],
                f"{c['cli']} {c['model'] or ''}",
                str(c["rc"]) if c["rc"] is not None else "—",
                _humanize_secs(dur) if dur else "—",
                f"{tok:,}" if tok else "—",
            )
        console.print(table)
        console.print(
            f"  total agent time: {_humanize_secs(total_secs)}, "
            f"reported tokens: {total_tokens:,}"
            + (" (codex only — others don't surface tokens in text mode)" if total_tokens else "")
        )

    # v2: subtasks summary
    sub_rows = store.list_subtasks(task_id)
    if sub_rows:
        # Per-subtask cost + call counts pulled from agent_calls.
        sub_cost: dict[str, dict[str, float]] = {}
        for c in store.conn.execute(
            "SELECT subtask_id, COUNT(*) AS n, SUM(duration_s) AS dur, SUM(cost_usd) AS cost "
            "FROM agent_calls WHERE task_id = ? AND subtask_id IS NOT NULL GROUP BY subtask_id",
            (task_id,),
        ):
            sub_cost[c["subtask_id"]] = {
                "n": c["n"] or 0,
                "dur": c["dur"] or 0.0,
                "cost": c["cost"] or 0.0,
            }
        console.print("\n[bold]── subtasks ──[/]")
        for r in sub_rows:
            ic = {
                "done": "[green]✓[/]",
                "blocked": "[red]✗[/]",
                "skipped": "[dim]·[/]",
                "pending": "[dim]·[/]",
            }.get(r["state"], "[yellow]…[/]")
            retries = f" (retries={r['retries']})" if (r.get("retries") or 0) else ""
            stats = sub_cost.get(r["subtask_id"])
            stats_str = ""
            if stats and stats["n"]:
                parts = [f"{stats['n']} calls", _humanize_secs(stats["dur"])]
                if stats["cost"]:
                    parts.append(f"${stats['cost']:.2f}")
                stats_str = f"  [dim]({', '.join(parts)})[/]"
            console.print(
                f"  {ic} [cyan]{r['subtask_id']}[/]  {r['state']}{retries}  {r.get('title') or ''}{stats_str}"
            )

    # v3: progress-check verdicts (latest per subtask). Surfaces what the
    # progress-check agent has been saying so operators can see FLATLINED
    # warnings without dropping into sqlite.
    pc_rows = list(
        store.conn.execute(
            "SELECT subtask_id, ts, attempts_at_check, verdict, rationale "
            "FROM progress_checks WHERE task_id = ? ORDER BY ts ASC",
            (task_id,),
        )
    )
    if pc_rows:
        latest: dict[str, dict] = {}
        counts: dict[str, dict[str, int]] = {}
        for r in pc_rows:
            sid = r["subtask_id"]
            latest[sid] = dict(r)
            counts.setdefault(sid, {})
            v = (r["verdict"] or "").lower()
            counts[sid][v] = counts[sid].get(v, 0) + 1
        console.print("\n[bold]── progress checks ──[/]")
        for sid, last in latest.items():
            verdict = (last["verdict"] or "").lower()
            color = {"flatlined": "red", "progressing": "green", "uncertain": "yellow"}.get(verdict, "white")
            tally = ", ".join(f"{v}={n}" for v, n in sorted(counts[sid].items()))
            ts_str = time.strftime("%H:%M:%S", time.localtime(last["ts"]))
            rationale = (last.get("rationale") or "").strip().replace("\n", " ")
            if len(rationale) > 200:
                rationale = rationale[:200] + "…"
            console.print(
                f"  [cyan]{sid}[/]  {ts_str}  attempt={last['attempts_at_check']}  "
                f"[{color}]{verdict}[/]  ({tally})"
            )
            if rationale:
                console.print(f"      [dim]{rationale}[/]")

    # Artifacts (latest of each kind)
    artifacts = store.conn.execute(
        "SELECT kind, content, ts FROM artifacts WHERE task_id = ? ORDER BY ts DESC",
        (task_id,),
    ).fetchall()
    seen: set[str] = set()
    for a in artifacts:
        kind = a["kind"]
        if kind in seen:
            continue
        seen.add(kind)
        console.print(f"\n[bold]── {kind} ──[/]")
        body = a["content"] or ""
        if not full and len(body) > 4000:
            body = body[:4000] + f"\n\n[dim]... ({len(a['content']) - 4000} more chars; pass --full)[/]"
        console.print(body)


@app.command()
def briefing(
    output_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of human-readable output"
    ),
):
    """One-shot snapshot for waking up to: in-flight states, recent transitions,
    cost so far, dag progress, disk, warnings. Designed for `quikode briefing` to be
    the first command you run in the morning."""
    cfg = load_config()
    store = _open_store(cfg)
    dag = DAG.load(cfg.dag_path)
    rows = store.all_tasks()
    now = time.time()
    if output_json:
        active_states = (
            State.PROVISIONING,
            State.PLANNING,
            State.DOING,
            State.CHECKING,
            State.TRIAGING,
            State.DOING_SUBTASK,
            State.CHECKING_SUBTASK,
            State.TRIAGING_SUBTASK,
            State.FINAL_CHECKING,
            State.COMMITTING,
            State.PUSHING,
            State.PR_OPENING,
            State.POLLING_CI,
            State.REBASING,
            State.CONFLICT_RESOLVING,
            State.INTENT_REVIEWING,
            State.REPLANNING,
        )
        actives = store.in_state(*active_states)
        cost_rows = list(
            store.conn.execute(
                "SELECT cli, COUNT(*) AS n, SUM(duration_s) AS total_s, SUM(tokens_used) AS total_tok "
                "FROM agent_calls GROUP BY cli ORDER BY cli"
            )
        )

        # v3 grouping: surface AWAITING_MERGE / RESPONDING_TO_REVIEW /
        # REBASING_TO_MAIN / BLOCKED separately so JSON consumers (TUI,
        # scripts) don't have to re-derive these from `tasks_by_state`.
        def _row_summary(r):
            return {
                "id": r["id"],
                "state": r["state"],
                "pr_number": r.get("pr_number"),
                "pr_url": r.get("pr_url"),
                "branch": r.get("branch"),
                "review_round": r.get("review_round"),
                "last_error": r.get("last_error"),
            }

        awaiting_merge = [_row_summary(r) for r in rows if r["state"] == State.AWAITING_MERGE.value]
        responding_to_review = [
            _row_summary(r) for r in rows if r["state"] == State.RESPONDING_TO_REVIEW.value
        ]
        rebasing_to_main = [_row_summary(r) for r in rows if r["state"] == State.REBASING_TO_MAIN.value]
        blocked_intervention = [_row_summary(r) for r in rows if r["state"] == State.BLOCKED.value]

        payload = {
            "tasks_by_state": {
                s: sum(1 for r in rows if r["state"] == s) for s in {r["state"] for r in rows}
            },
            "awaiting_merge": awaiting_merge,
            "responding_to_review": responding_to_review,
            "rebasing_to_main": rebasing_to_main,
            "blocked_needs_intervention": blocked_intervention,
            "in_flight": [
                {
                    "id": r["id"],
                    "state": r["state"],
                    "in_state_seconds": (now - (_last_state_change(store, r["id"]) or now)),
                    "worktree_mtime_age_seconds": (
                        (now - _worktree_mtime(Path(r["worktree_path"])))
                        if r.get("worktree_path") and _worktree_mtime(Path(r["worktree_path"]))
                        else None
                    ),
                    "max_rss_bytes": store.task_max_rss(r["id"]),
                    "pr_number": r.get("pr_number"),
                }
                for r in actives
            ],
            "agent_cost": {
                r["cli"]: {"calls": r["n"], "total_seconds": r["total_s"], "total_tokens": r["total_tok"]}
                for r in cost_rows
            },
            "dag": {
                "total_nodes": len(dag.nodes),
                "merged": sum(1 for r in rows if r["state"] == State.MERGED.value),
            },
            "disk": {
                "sccache_bytes": _dir_size(cfg.sccache_dir),
                "worktrees_bytes": _dir_size(cfg.worktree_root),
                "logs_bytes": _dir_size(cfg.log_dir),
            },
        }
        print(json.dumps(payload, indent=2, default=str))
        return

    console.rule("[bold cyan]quikode briefing[/]")

    # 1. Task summary
    by_state: dict[str, int] = {}
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    if by_state:
        console.print("\n[bold]Task states[/]")
        for s, n in sorted(by_state.items()):
            console.print(f"  {s}: {n}")
    else:
        console.print("\n[dim]No tasks recorded yet.[/]")

    # 2. Active tasks detail
    active_states = (
        State.PROVISIONING,
        State.PLANNING,
        State.DOING,
        State.CHECKING,
        State.TRIAGING,
        State.DOING_SUBTASK,
        State.CHECKING_SUBTASK,
        State.TRIAGING_SUBTASK,
        State.FINAL_CHECKING,
        State.COMMITTING,
        State.PUSHING,
        State.PR_OPENING,
        State.POLLING_CI,
    )
    actives = store.in_state(*active_states)
    if actives:
        console.print("\n[bold]In-flight[/]")
        for r in actives:
            last = _last_state_change(store, r["id"])
            in_state = (now - last) if last else None
            wt_mt = _worktree_mtime(Path(r["worktree_path"])) if r.get("worktree_path") else None
            wt_age = (now - wt_mt) if wt_mt else None
            mx = store.task_max_rss(r["id"])
            mem_str = f"  max_rss={mx / (1024**3):.1f}GB" if mx else ""
            cost = store.task_total_cost_usd(r["id"])
            cost_str = f"  · ${cost:.2f}" if cost else ""
            console.print(
                f"  [cyan]{r['id']}[/] [{r['state']}] "
                f"in-state {_humanize_secs(in_state)}; "
                f"worktree edit {_humanize_secs(wt_age)} ago"
                + (f"  pr#{r['pr_number']}" if r.get("pr_number") else "")
                + mem_str
                + cost_str
            )

    # 3. Awaiting human / blocked / v3 review-loop tasks
    awaiting = store.in_state(State.AWAITING_MERGE)
    responding = store.in_state(State.RESPONDING_TO_REVIEW)
    rebasing = store.in_state(State.REBASING_TO_MAIN)
    blocked_only = store.in_state(State.BLOCKED)
    failed_only = store.in_state(State.FAILED)
    if awaiting:
        console.print("\n[bold bright_green]Awaiting merge[/]")
        for r in awaiting:
            cost = store.task_total_cost_usd(r["id"])
            cost_str = f"  · ${cost:.2f}" if cost else ""
            console.print(f"  [cyan]{r['id']}[/]  PR: {r.get('pr_url') or '(local only)'}{cost_str}")
    if responding:
        console.print("\n[bold cyan]Responding to review[/]")
        for r in responding:
            rr = r.get("review_round")
            round_str = f" round {rr}" if rr else ""
            cost = store.task_total_cost_usd(r["id"])
            cost_str = f"  · ${cost:.2f}" if cost else ""
            console.print(
                f"  [cyan]{r['id']}[/]{round_str}  PR: {r.get('pr_url') or '(local only)'}{cost_str}"
            )
    if rebasing:
        console.print("\n[bold yellow]Rebasing onto main[/]")
        for r in rebasing:
            console.print(f"  [cyan]{r['id']}[/]  branch: {r.get('branch') or '(unknown)'}")
    if blocked_only:
        console.print("\n[bold red]Blocked — needs intervention[/]")
        for r in blocked_only:
            note = (r.get("last_error") or "")[:120]
            console.print(f"  [cyan]{r['id']}[/] {note}\n    [dim]→ quikode unblock {r['id']}[/]")
    if failed_only:
        console.print("\n[bold red]Failed[/]")
        for r in failed_only:
            note = (r.get("last_error") or "")[:120]
            console.print(f"  [cyan]{r['id']}[/] {note}")
    # `blocked` retained as a name for the warning aggregate at the end.
    blocked = list(blocked_only) + list(failed_only)

    # 4. Recent transitions (last 20)
    recent = list(
        store.conn.execute(
            "SELECT task_id, from_state, to_state, ts, note FROM state_log ORDER BY ts DESC LIMIT 20",
        )
    )
    if recent:
        console.print("\n[bold]Recent transitions[/]")
        for r in recent[:20]:
            ts_str = time.strftime("%m-%d %H:%M:%S", time.localtime(r["ts"]))
            note = f"  [dim]{r['note']}[/]" if r["note"] else ""
            console.print(
                f"  {ts_str}  {r['task_id']}  {r['from_state'] or '—'} → [cyan]{r['to_state']}[/]{note}"
            )

    # 4b. Recent merges with per-task cost (last 10 by merge ts)
    merged_rows = [r for r in rows if r["state"] == State.MERGED.value]
    if merged_rows:
        merge_ts: list[tuple[str, float, float | None]] = []
        for r in merged_rows:
            ts_row = store.conn.execute(
                "SELECT ts FROM state_log WHERE task_id = ? AND to_state = ? ORDER BY ts DESC LIMIT 1",
                (r["id"], State.MERGED.value),
            ).fetchone()
            ts = float(ts_row["ts"]) if ts_row else 0.0
            cost = store.task_total_cost_usd(r["id"])
            merge_ts.append((r["id"], ts, cost))
        merge_ts.sort(key=lambda t: t[1], reverse=True)
        if merge_ts:
            console.print("\n[bold]Recent merges[/]")
            for tid, ts, cost in merge_ts[:10]:
                ts_str = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "—"
                cost_str = f"  · ${cost:.2f}" if cost else ""
                console.print(f"  {ts_str}  [cyan]{tid}[/]{cost_str}")

    # 5. Agent cost so far
    cost_rows = list(
        store.conn.execute(
            "SELECT cli, COUNT(*) AS n, SUM(duration_s) AS total_s, SUM(tokens_used) AS total_tok, "
            "SUM(cost_usd) AS total_cost "
            "FROM agent_calls GROUP BY cli ORDER BY cli"
        )
    )
    if cost_rows:
        console.print("\n[bold]Agent cost (this workspace)[/]")
        for r in cost_rows:
            cost_str = f", ${r['total_cost']:.2f}" if r["total_cost"] else ""
            console.print(
                f"  {r['cli']}: {r['n']} calls, "
                f"{_humanize_secs(r['total_s'])} total, "
                f"{r['total_tok'] or 0:,} tokens"
                f"{cost_str}"
            )
        total_cost = store.workspace_total_cost_usd()
        if total_cost:
            merged_count = len(merged_rows)
            avg = (total_cost / merged_count) if merged_count else None
            avg_str = f"  (avg ${avg:.2f}/merged task)" if avg else ""
            console.print(f"  [bold]total: ${total_cost:.2f}[/]{avg_str}")

    # 6. DAG progress (one liner)
    total = len(dag.nodes)
    merged = sum(1 for r in rows if r["state"] == State.MERGED.value)
    awaiting_count = len(awaiting)
    pct = (100 * merged // total) if total else 0
    console.print(
        f"\n[bold]DAG[/]  {merged}/{total} merged ({pct}%)  +{awaiting_count} awaiting merge  +{len(actives)} active  +{len(blocked)} blocked"
    )

    # 7. Disk
    sccache = _dir_size(cfg.sccache_dir)
    worktrees = _dir_size(cfg.worktree_root)
    logs = _dir_size(cfg.log_dir)
    console.print(
        f"\n[bold]Disk[/]  sccache={_humanize_bytes(sccache)}  worktrees={_humanize_bytes(worktrees)}  logs={_humanize_bytes(logs)}"
    )

    # 8. Warnings
    warnings: list[str] = []
    for r in actives:
        if r["state"] == State.DOING.value:
            wt_mt = _worktree_mtime(Path(r["worktree_path"])) if r.get("worktree_path") else None
            if wt_mt and (now - wt_mt) > cfg.stall_warn_seconds:
                warnings.append(f"{r['id']} doer worktree quiet for {int((now - wt_mt) // 60)} min")
    # Containers without a corresponding active task = orphans.
    # Container names look like qk-<task-id-slug>-<hex>-{dev|pg}; we compare the
    # task slug portion against each active task's slug. Filter by workspace
    # label so we don't flag containers from sibling workspaces (e.g., the
    # fixture vs tanren running in parallel).
    qk_containers = docker_env.list_quikode_containers(label=docker_env.workspace_label(cfg))
    active_slugs = {docker_env.slugify(r["id"]) for r in actives}
    for c in qk_containers:
        # Strip qk- prefix and -{dev|pg} suffix, then drop trailing hex token.
        n = c["name"]
        if n.startswith("qk-"):
            inner = n[3:]
            for sfx in ("-dev", "-pg"):
                if inner.endswith(sfx):
                    inner = inner[: -len(sfx)]
                    break
            # inner is now <task-slug>-<hex>; the slug is everything before the last "-<hex>".
            parts = inner.rsplit("-", 1)
            slug = parts[0] if len(parts) == 2 else inner
            if slug not in active_slugs:
                warnings.append(f"orphan container: {c['name']}")
    if warnings:
        console.print("\n[bold yellow]Warnings[/]")
        for w in warnings:
            console.print(f"  · {w}")
    else:
        console.print("\n[green]No warnings.[/]")

    # 9. Quick command hints
    console.print("\n[dim]Hints:[/]")
    console.print("  [dim]quikode show <id>      — full state + artifacts[/]")
    console.print("  [dim]quikode export <id>    — bundle for review[/]")
    console.print("  [dim]quikode dag-stats      — per-milestone breakdown[/]")
    console.print("  [dim]quikode watch          — live-updating table[/]")


@app.command("dev-test")
def dev_test(
    fixture_root: Path = typer.Option(
        Path("/home/trevor/github/quikode-runs/fixture"),
        "--root",
        help="Workspace root for the fixture run",
    ),
    timeout_min: int = typer.Option(
        15, "--timeout-min", help="Fail if T-001 doesn't reach awaiting_merge within N min"
    ),
):
    """End-to-end smoke test against the fastapi fixture. Exits 0 on success.

    Use this to validate quikode changes without burning a tanren cycle. Assumes
    the fixture workspace is already initialized (`quikode init` was run there
    pointing at quikode-fixture)."""
    if not (fixture_root / ".quikode" / "config.toml").exists():
        console.print(f"[red]no fixture config at {fixture_root}/.quikode/config.toml[/]")
        console.print(
            "Run: quikode init --repo /home/trevor/github/quikode-fixture --dag /home/trevor/github/quikode-fixture/dag.json (in --root)"
        )
        raise typer.Exit(2)
    # Reset
    r = subprocess.run(
        [sys.argv[0], "reset", "--yes", "--close-prs"],
        cwd=fixture_root,
        capture_output=True,
        text=True,
    )
    console.print(r.stdout)
    if r.returncode != 0:
        console.print(f"[red]reset failed: {r.stderr}[/]")
        raise typer.Exit(2)
    # Launch run in background
    log_file = fixture_root / ".quikode" / "dev-test.log"
    proc = subprocess.Popen(
        [sys.argv[0], "run", "--max-parallel", "1", "--log-level", "INFO"],
        cwd=fixture_root,
        stdout=log_file.open("w"),
        stderr=subprocess.STDOUT,
    )
    console.print(f"[cyan]started run pid {proc.pid}; tailing → {log_file}[/]")
    deadline = time.time() + timeout_min * 60
    cfg = load_config(root=fixture_root)
    store = _open_store(cfg)
    while time.time() < deadline:
        time.sleep(15)
        rows = store.all_tasks()
        states = {r["id"]: r["state"] for r in rows}
        elapsed = int(time.time() - (deadline - timeout_min * 60))
        console.print(
            f"  [{elapsed // 60:02d}:{elapsed % 60:02d}] " + " ".join(f"{i}={s}" for i, s in states.items())
        )
        if any(s in (State.AWAITING_MERGE.value, State.MERGED.value) for s in states.values()):
            proc.terminate()
            proc.wait(timeout=5)
            console.print("[bold green]PASS[/] — fixture run reached awaiting_merge")
            return
        if (
            all(s in (State.BLOCKED.value, State.FAILED.value, State.ABORTED.value) for s in states.values())
            and states
        ):
            proc.terminate()
            console.print("[bold red]FAIL[/] — fixture run terminated unsuccessfully")
            console.print(f"see {log_file} and `quikode show <id>` for details")
            raise typer.Exit(1)
    proc.terminate()
    console.print(f"[bold red]TIMEOUT[/] — fixture run didn't finish in {timeout_min} min")
    raise typer.Exit(1)


@app.command()
def tui():
    """Launch the mission-control TUI (live dashboard for the orchestrator)."""
    cfg_root = find_config_root()
    run_tui(workspace=cfg_root)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
