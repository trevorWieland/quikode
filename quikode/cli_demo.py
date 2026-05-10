"""`qk demo` command — materialize a task's PR branch in a sibling clone."""

from __future__ import annotations

from .cli_context import (
    Path,
    _open_store,
    _resolve_repo_clone_url,
    app,
    console,
    load_config,
    shutil,
    subprocess,
    typer,
)


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
        _checkout_demo_branch(target_dir, str(branch))
    else:
        _clone_demo_repo(repo_path, target_dir, str(branch))
    console.print(f"\n[bold green]demo ready[/] at [cyan]{target_dir}[/]")
    _print_demo_hint(target_dir)


def _checkout_demo_branch(target_dir: Path, branch: str) -> None:
    console.print(f"[cyan]demo dir exists at {target_dir}[/] — fetching + checking out [b]{branch}[/]")
    subprocess.run(["git", "fetch", "origin", branch], cwd=str(target_dir), check=False)
    rc = _git_checkout(target_dir, branch)
    if rc.returncode != 0:
        console.print(f"[red]git checkout failed: {rc.stderr}[/]")
        raise typer.Exit(1)


def _clone_demo_repo(repo_path: Path, target_dir: Path, branch: str) -> None:
    clone_url = _resolve_repo_clone_url(repo_path)
    if not clone_url:
        console.print("[red]could not determine clone url for the repo[/]")
        raise typer.Exit(1)
    console.print(f"[cyan]cloning[/] {clone_url} → {target_dir}")
    rc = subprocess.run(["git", "clone", clone_url, str(target_dir)], capture_output=True, text=True)
    if rc.returncode != 0:
        console.print(f"[red]git clone failed: {rc.stderr}[/]")
        raise typer.Exit(1)
    rc = _git_checkout(target_dir, branch)
    if rc.returncode == 0:
        return
    subprocess.run(["git", "fetch", "origin", branch], cwd=str(target_dir), check=False)
    rc = _git_checkout(target_dir, branch)
    if rc.returncode != 0:
        console.print(f"[red]git checkout {branch} failed: {rc.stderr}[/]")
        raise typer.Exit(1)


def _git_checkout(target_dir: Path, branch: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "checkout", branch], cwd=str(target_dir), capture_output=True, text=True)


def _print_demo_hint(target_dir: Path) -> None:
    if (target_dir / "pyproject.toml").exists() or (target_dir / "uv.lock").exists():
        console.print(f"  cd {target_dir} && uv sync && source .venv/bin/activate")
    elif (target_dir / "Cargo.toml").exists():
        console.print(f"  cd {target_dir} && cargo build")
    elif (target_dir / "package.json").exists():
        console.print(f"  cd {target_dir} && npm install")
    else:
        console.print(f"  cd {target_dir}")
