"""Shared Typer CLI context and common imports."""

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
from . import docker_env, fsm_runtime, retry_classify, sound, worktree
from . import workspace as workspace_mod
from .config import Config
from .config_loader import find_config_root, load_config
from .config_template import DEFAULT_CONFIG_TOML, render_config_toml
from .dag import DAG
from .orchestrator import Orchestrator
from .profiles import BUILTIN_PROFILES, get_profile
from .state import State, Store
from .tui import run_tui

__all__ = [
    "BUILTIN_PROFILES",
    "DAG",
    "DEFAULT_CONFIG_TOML",
    "Config",
    "Console",
    "Live",
    "Orchestrator",
    "Path",
    "State",
    "Store",
    "Table",
    "_build_status_table",
    "_compute_max_parallel",
    "_dir_size",
    "_humanize_bytes",
    "_humanize_secs",
    "_last_state_change",
    "_open_store",
    "_resolve_repo_clone_url",
    "_setup_logging",
    "_worktree_age_seconds",
    "_worktree_mtime",
    "app",
    "atexit",
    "console",
    "daemon_app",
    "daemon_mod",
    "docker_env",
    "find_config_root",
    "fsm_runtime",
    "get_profile",
    "json",
    "load_config",
    "os",
    "render_config_toml",
    "retry_classify",
    "run_tui",
    "shutil",
    "signal",
    "sound",
    "subprocess",
    "sys",
    "time",
    "typer",
    "workspace_mod",
    "worktree",
]

app = typer.Typer(add_completion=False, no_args_is_help=True, pretty_exceptions_show_locals=False)
console = Console()
daemon_app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    invoke_without_command=True,
    help="Run/manage the orchestrator under a crash-restart supervisor.",
)
app.add_typer(daemon_app, name="daemon")


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )


def _open_store(cfg: Config) -> Store:
    return Store(cfg.state_dir / "quikode.db")


_SKIP_MTIME_DIRS = {".git", "target", "node_modules", ".venv", ".mypy_cache", ".pytest_cache"}


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


def _last_state_change(store: Store, task_id: str) -> float | None:
    """Timestamp of the most recent transition into the current state."""
    r = store.conn.execute(
        "SELECT MAX(ts) AS ts FROM state_log WHERE task_id = ? AND to_state = "
        "(SELECT state FROM tasks WHERE id = ?)",
        (task_id, task_id),
    ).fetchone()
    return r["ts"] if r and r["ts"] else None


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


def _build_status_table(store: Store, *, show_terminal: bool = True) -> Table:
    rows = store.all_tasks()
    by_state: dict[str, int] = {}
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    total = len(rows)
    merged = by_state.get(State.MERGED.value, 0)
    awaiting = by_state.get(State.PENDING_CI.value, 0)
    blocked = by_state.get(State.BLOCKED.value, 0) + by_state.get(State.FAILED.value, 0)
    pct = (100 * merged // total) if total else 0
    summary = f"merged={merged}/{total} ({pct}%)  awaiting={awaiting}  blocked={blocked}  " + "  ".join(
        f"{s}={n}"
        for s, n in sorted(by_state.items())
        if s not in (State.MERGED.value, State.PENDING_CI.value, State.BLOCKED.value, State.FAILED.value)
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
        State.PENDING_CI.value: "bright_green",
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
        wt_mt = _worktree_mtime(Path(str(r["worktree_path"]))) if r.get("worktree_path") else None
        wt_age = (now - wt_mt) if wt_mt else None
        # red flag: in active state but worktree quiet for >5 min
        wt_color = "white"
        if wt_age is not None and st == State.DOING_SUBTASK.value:
            if wt_age > 300:
                wt_color = "red"
            elif wt_age > 120:
                wt_color = "yellow"
        retries = f"{r.get('ci_triage_retries') or 0}"
        pr = r.get("pr_url") or ""
        pr_n = pr.rsplit("/", 1)[-1] if pr else ""
        branch_pr = r.get("branch") or ""
        if pr_n:
            branch_pr += f" → PR #{pr_n}"
        # state-elapsed colour: stale yellow at 10min, red at 30min for active states
        in_state_color = "white"
        if in_state and st in (State.PLANNING.value,):
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


def _humanize_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}PB"


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


def _worktree_age_seconds(now: float, raw_path: object) -> float | None:
    if not raw_path:
        return None
    mtime = _worktree_mtime(Path(str(raw_path)))
    return (now - mtime) if mtime is not None else None


__all__ = [name for name in globals() if not name.startswith("__")]
