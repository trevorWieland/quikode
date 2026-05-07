"""Typer command group."""

from __future__ import annotations

import fcntl

from .cli_context import (
    BUILTIN_PROFILES,
    DAG,
    Config,
    Orchestrator,
    Path,
    State,
    Store,
    _compute_max_parallel,
    _open_store,
    _setup_logging,
    app,
    atexit,
    console,
    docker_env,
    fsm_runtime,
    get_profile,
    load_config,
    os,
    render_config_toml,
    shutil,
    signal,
    subprocess,
    time,
    typer,
    workspace_mod,
    worktree,
)


@app.command()
def init(
    repo: Path = typer.Option(..., "--repo", help="Path to the target git repo"),
    dag: Path = typer.Option(..., "--dag", help="Path to the dag.json file"),
    profile: str = typer.Option("tanren", "--profile", help="Project profile"),
    force: bool = typer.Option(False, "--force"),
    no_seed_from_base: bool = typer.Option(
        False,
        "--no-seed-from-base",
        help="Do not seed already-merged DAG nodes from the configured base branch.",
    ),
    no_seed_from_main: bool = typer.Option(
        False,
        "--no-seed-from-main",
        help="Compatibility alias for --no-seed-from-base.",
    ),
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
    if profile not in BUILTIN_PROFILES:
        console.print(f"[red]unknown profile: {profile}[/]")
        console.print(f"valid profiles: {', '.join(sorted(BUILTIN_PROFILES))}")
        raise typer.Exit(2)
    profile_def = get_profile(profile)
    content = render_config_toml(repo_path=repo_abs, dag_path=dag_abs, profile=profile_def)
    cfg_path.write_text(content)
    (cfg_dir / "logs").mkdir(exist_ok=True)
    (cfg_dir / "worktrees").mkdir(exist_ok=True)
    console.print(f"[green]wrote {cfg_path}[/]")
    should_seed = not (no_seed_from_base or no_seed_from_main)
    if should_seed:
        cfg = load_config()
        store = _open_store(cfg)
        result = workspace_mod.seed_from_base(cfg, store)
        console.print(
            f"[green]seeded {len(result.merged)} merged DAG node(s) from {cfg.pr_remote}/{cfg.base_branch}[/]"
        )
    console.print(
        "Next: edit the config to set agent models, then `quikode doctor`, then `quikode build-image`."
    )


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


@app.command("build-image")
def build_image(flavor: str = typer.Option("tanren", "--flavor", help="tanren | rust | python")):
    """Build the dev container image. --flavor selects the Dockerfile."""
    _setup_logging()
    cfg = load_config()
    here = Path(__file__).resolve().parent.parent / "docker"
    dockerfile = {
        "tanren": here / "Dockerfile",
        "rust": here / "Dockerfile",
        "python": here / "Dockerfile.python",
    }.get(flavor)
    if dockerfile is None or not dockerfile.exists():
        console.print(f"[red]unknown flavor: {flavor}[/]")
        raise typer.Exit(2)
    cmd = ["docker", "build", "-t", cfg.image_tag, "-f", str(dockerfile), str(here)]
    console.print(f"[cyan]$ {' '.join(cmd)}[/]")
    r = subprocess.run(cmd)
    raise typer.Exit(r.returncode)


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
    scope = _resolve_run_scope(dag, only, milestone)
    _prepare_run_workspace(cfg, max_parallel=max_parallel)
    store = _open_store(cfg)
    _recover_orphan_tasks(store)
    if retry_failed:
        _retry_failed_tasks(store, scope)
    _print_run_start_summary(cfg, dag, store, scope)
    pid_file = cfg.state_dir / "orchestrator.pid"
    _write_orchestrator_pid(pid_file)

    def cleanup_pid() -> None:
        _cleanup_pid(pid_file)
        _release_orchestrator_lock()

    atexit.register(cleanup_pid)
    orch = Orchestrator(cfg, dag, store, task_filter=scope)
    _install_stop_handlers(orch)
    try:
        orch.run()
    except KeyboardInterrupt:
        console.print("[yellow]stopping...[/]")
        orch.stop()
    finally:
        cleanup_pid()


def _resolve_run_scope(dag: DAG, only: list[str] | None, milestone: str | None) -> set[str] | None:
    if only or milestone:
        scope = dag.filter(ids=only, milestone=milestone)
        console.print(f"[cyan]scope: {len(scope)} nodes (incl. transitive deps)[/]")
        return scope
    console.print(f"[cyan]scope: all {len(dag.nodes)} nodes[/]")
    return None


_orchestrator_lock_fd: int | None = None


def _acquire_orchestrator_lock(cfg: Config) -> None:
    """Acquire an exclusive flock on `<state_dir>/orchestrator.lock` BEFORE
    any destructive workspace prep (container cleanup, orphan recovery).

    Without this lock, two `qk run` invocations against the same workspace
    race: the second's `cleanup_all_quikode` kills the first's live worker
    containers, and `recover_orphan_tasks` flips rows to PENDING while the
    first's worker threads are still firing FSM events against them — which
    then crash with InvalidTransition. See plan 20 / 2026-05-07 incident.

    flock is advisory and kernel-released on FD close (incl. SIGKILL), so a
    crashed daemon never leaks the lock. The file descriptor is held in
    module state for the daemon's lifetime — released by `cleanup_pid` via
    the existing atexit hook. We use raw os.open / os.close rather than the
    `open` builtin because the FD's lifetime intentionally exceeds any
    function scope; a `with` would release the lock instantly.
    """
    global _orchestrator_lock_fd
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cfg.state_dir / "orchestrator.lock"
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        console.print(
            f"[red]another quikode daemon holds {lock_path} — refusing to clean up "
            f"containers; stop the other daemon first.[/]"
        )
        raise typer.Exit(2) from exc
    _orchestrator_lock_fd = fd


def _release_orchestrator_lock() -> None:
    """Idempotent. Called from cleanup_pid atexit hook."""
    global _orchestrator_lock_fd
    if _orchestrator_lock_fd is not None:
        try:
            fcntl.flock(_orchestrator_lock_fd, fcntl.LOCK_UN)
            os.close(_orchestrator_lock_fd)
        except Exception:
            pass
        _orchestrator_lock_fd = None


def _prepare_run_workspace(cfg: Config, *, max_parallel: int | None) -> None:
    for path in (cfg.log_dir, cfg.worktree_root, cfg.state_dir, cfg.sccache_dir):
        path.mkdir(parents=True, exist_ok=True)
    _acquire_orchestrator_lock(cfg)
    if cfg.max_parallel_auto and max_parallel is None:
        host = docker_env.host_resources()
        cap, expl = _compute_max_parallel(cfg, host)
        cfg.max_parallel = cap
        console.print(f"[cyan]max_parallel auto:[/] {cap}  [dim]({expl})[/]")
    n = docker_env.cleanup_all_quikode(cfg)
    if n:
        console.print(f"[yellow]cleaned up {n} stranded qk-* containers[/]")
    _prune_stale_worktrees(cfg)


def _prune_stale_worktrees(cfg: Config) -> None:
    try:
        pruned = worktree.prune_stale_worktrees(cfg.repo_path, cfg.worktree_root)
    except Exception as e:
        console.print(f"[yellow]worktree prune skipped: {e}[/]")
        return
    if pruned:
        console.print(f"[yellow]pruned {len(pruned)} stale worktree dir(s)[/]")


def _recover_orphan_tasks(store: Store) -> None:
    recovered = store.recover_orphan_tasks()
    for tid, frm, to in recovered:
        console.print(f"[yellow]orphan recovery:[/] {tid}: {frm} -> {to}")
    if recovered:
        console.print(f"[yellow]recovered {len(recovered)} orphan task(s) from prior run[/]")


def _retry_failed_tasks(store: Store, scope: set[str] | None) -> None:
    terminal_to_retry = (State.BLOCKED.value, State.FAILED.value, State.ABORTED.value)
    reset_count = 0
    for row in store.all_tasks():
        if row["state"] in terminal_to_retry and (scope is None or row["id"] in scope):
            fsm_runtime.retry_task(
                store,
                row["id"],
                note="auto retry-failed",
                ci_triage_retries=0,
                last_error=None,
                container_id=None,
                resume_from_existing_subtasks=1,
            )
            reset_count += 1
    if reset_count:
        console.print(f"[yellow]auto-retry: resumed {reset_count} blocked/failed task(s)[/]")


def _print_run_start_summary(cfg: Config, dag: DAG, store: Store, scope: set[str] | None) -> None:
    actual_scope = scope if scope is not None else set(dag.nodes)
    completed = store.completed_ids() & actual_scope
    by_state = _state_counts_for_scope(store, actual_scope)
    ready_now = [
        node
        for node in dag.ready_nodes(completed_ids=completed, in_progress_ids=set())
        if node.id in actual_scope
    ]
    summary = "  ".join(f"{state}={count}" for state, count in sorted(by_state.items()))
    console.print(
        f"[bold]start:[/] {summary}  |  [cyan]{len(ready_now)} ready now[/]  |  max-parallel {cfg.max_parallel}"
    )
    if not ready_now:
        console.print("[yellow]nothing ready to schedule. use `quikode plan` or `quikode retry <id>`.[/]")


def _state_counts_for_scope(store: Store, scope: set[str]) -> dict[str, int]:
    by_state: dict[str, int] = {}
    for node_id in scope:
        row = store.get(node_id)
        state = row["state"] if row else State.PENDING.value
        by_state[state] = by_state.get(state, 0) + 1
    return by_state


def _write_orchestrator_pid(pid_file: Path) -> None:
    if pid_file.exists():
        ts = _pid_file_timestamp(pid_file)
        if ts and time.time() - ts < 60:
            console.print(f"[red]another orchestrator pid file is fresh ({pid_file}); refusing to start.[/]")
            raise typer.Exit(1)
    pid_file.write_text(f"{os.getpid()}@{time.time():.0f}\n")


def _pid_file_timestamp(pid_file: Path) -> float:
    try:
        content = pid_file.read_text().strip()
        return float(content.rsplit("@", 1)[-1]) if "@" in content else 0.0
    except (OSError, ValueError):
        return 0.0


def _cleanup_pid(pid_file: Path) -> None:
    try:
        pid_file.unlink()
    except OSError:
        pass


def _install_stop_handlers(orch: Orchestrator) -> None:
    def _request_stop(_signum, _frame):
        try:
            console.print("[yellow]received stop signal - winding down...[/]")
        except Exception:
            pass
        orch.stop()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
