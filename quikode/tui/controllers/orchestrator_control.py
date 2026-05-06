"""Orchestrator subprocess control. Spawn / stop / detect / attach.

Design (per docs/design-tui.md):
- The orchestrator is `quikode run`. It runs as its own subprocess so it
  survives TUI restarts (the TUI is the cockpit, not the engine).
- A PID file at `.quikode/orchestrator.pid` lets a fresh TUI re-attach to
  a running orchestrator on launch.
- `/stop` is graceful (SIGTERM, 60s wait). `/force-quit` is SIGKILL.
- The TUI never blocks on subprocess I/O; spawn/stop are async helpers.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# How long /stop waits before falling back to /force-quit semantics.
STOP_TIMEOUT_S = 60

# Heartbeat staleness threshold used by the TUI when no Config-driven value is
# available (e.g. older callers). The daemon module's get_status uses the
# config-derived value; this mirrors the default so behaviour is consistent.
HEARTBEAT_STALENESS_S = 30


@dataclass(frozen=True)
class OrchestratorStatus:
    pid: int | None
    running: bool
    pid_file: Path
    started_at: float | None = None
    heartbeat_age_s: float | None = None
    heartbeat_data: dict | None = None
    heartbeat_stale: bool = False


def _pid_file(workspace: Path) -> Path:
    return workspace / ".quikode" / "orchestrator.pid"


def _log_file(workspace: Path) -> Path:
    return workspace / ".quikode" / "logs" / "orchestrator.log"


def _heartbeat_file(workspace: Path) -> Path:
    return workspace / ".quikode" / "orchestrator.heartbeat"


def _read_heartbeat(workspace: Path) -> tuple[dict | None, float | None]:
    """Return (parsed-json, age-in-seconds), both None if missing/unreadable."""
    p = _heartbeat_file(workspace)
    if not p.exists():
        return None, None
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return None, None
    age: float | None = None
    try:
        ts = float(data.get("ts", 0.0))
        age = max(0.0, time.time() - ts)
    except (TypeError, ValueError):
        age = None
    return data, age


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    # If the process is our child, reap zombies first so kill(pid, 0) doesn't
    # report "alive" for an already-dead-but-unreaped child.
    try:
        reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
        if reaped_pid == pid:
            return False
    except ChildProcessError:
        # Not our child (e.g. orphan reattached after TUI restart) — kernel reaps it.
        pass
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but isn't ours — treat as alive for our purposes.
        return True
    except OSError as e:
        return e.errno != errno.ESRCH
    return True


def status(workspace: Path, *, staleness_s: int = HEARTBEAT_STALENESS_S) -> OrchestratorStatus:
    """Read the PID file and check if the recorded PID is still alive.

    Also reads `orchestrator.heartbeat` and reports its age + stale state.
    "Running" remains process-alive (so the TUI can still talk to a stopped-but-
    not-cleaned-up daemon). Stale-heartbeat callers should consult
    `heartbeat_stale` separately to surface a warning state.
    """
    pid_file = _pid_file(workspace)
    hb_data, hb_age = _read_heartbeat(workspace)
    hb_stale = hb_data is not None and hb_age is not None and hb_age > staleness_s
    if not pid_file.exists():
        return OrchestratorStatus(
            pid=None,
            running=False,
            pid_file=pid_file,
            heartbeat_age_s=hb_age,
            heartbeat_data=hb_data,
            heartbeat_stale=hb_stale,
        )
    try:
        raw = pid_file.read_text().strip()
        pid_str, _, ts_str = raw.partition("@")
        pid = int(pid_str)
        started_at: float | None = float(ts_str) if ts_str else None
    except (ValueError, OSError):
        return OrchestratorStatus(
            pid=None,
            running=False,
            pid_file=pid_file,
            heartbeat_age_s=hb_age,
            heartbeat_data=hb_data,
            heartbeat_stale=hb_stale,
        )
    if not _is_pid_alive(pid):
        # Stale pid file. Best-effort cleanup so the next /run isn't blocked.
        try:
            pid_file.unlink()
        except OSError:
            pass
        return OrchestratorStatus(
            pid=None,
            running=False,
            pid_file=pid_file,
            heartbeat_age_s=hb_age,
            heartbeat_data=hb_data,
            heartbeat_stale=hb_stale,
        )
    return OrchestratorStatus(
        pid=pid,
        running=True,
        pid_file=pid_file,
        started_at=started_at,
        heartbeat_age_s=hb_age,
        heartbeat_data=hb_data,
        heartbeat_stale=hb_stale,
    )


def spawn(workspace: Path, *, extra_args: list[str] | None = None) -> OrchestratorStatus:
    """Spawn `quikode run` as a detached subprocess. Returns the new status.

    Raises FileExistsError if an orchestrator is already running.
    """
    cur = status(workspace)
    if cur.running:
        raise FileExistsError(f"orchestrator already running with pid {cur.pid} (see {cur.pid_file})")
    pid_file = _pid_file(workspace)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    log_path = _log_file(workspace)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fp = log_path.open("ab")
    argv = _quikode_run_argv(extra_args or [])
    # Detach using start_new_session so the orchestrator survives TUI exit.
    proc = subprocess.Popen(
        argv,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        cwd=str(workspace),
        start_new_session=True,
    )
    # Parent writes the PID file immediately so status() can see the spawn
    # right away (don't race the child's startup write). The child overwrites
    # with the same content on its own startup and owns atexit cleanup —
    # CLI-started runs (no spawn parent) write the same PID file themselves.
    pid_file.write_text(f"{proc.pid}@{time.time():.0f}\n")
    return OrchestratorStatus(pid=proc.pid, running=True, pid_file=pid_file, started_at=time.time())


def stop(workspace: Path, *, timeout_s: int = STOP_TIMEOUT_S) -> bool:
    """Graceful stop — SIGTERM, then poll for exit. Returns True if stopped."""
    cur = status(workspace)
    if not cur.running or cur.pid is None:
        return False
    try:
        os.kill(cur.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _is_pid_alive(cur.pid):
            try:
                cur.pid_file.unlink()
            except OSError:
                pass
            return True
        time.sleep(1)
    return False


def force_quit(workspace: Path) -> bool:
    """Hard kill — SIGKILL. Containers will be stranded."""
    cur = status(workspace)
    if not cur.running or cur.pid is None:
        return False
    try:
        os.kill(cur.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        cur.pid_file.unlink()
    except OSError:
        pass
    return True


def parting_status_message(workspace: Path) -> str | None:
    """Return None if no orchestrator is running, else the message to print
    on TUI exit. Per design-tui.md §3."""
    s = status(workspace)
    if not s.running or s.pid is None:
        return None
    log = _log_file(workspace)
    return (
        "quikode is still running in the background.\n"
        f"  orchestrator pid: {s.pid}\n"
        f"  pid file:        {s.pid_file}\n"
        f"  log:             {log}\n"
        "  re-attach:       quikode tui\n"
        "  stop:            quikode stop\n"
    )


def _quikode_run_argv(extra: list[str]) -> list[str]:
    bin_override = os.environ.get("QUIKODE_BIN")
    if bin_override:
        return [bin_override, "run", *extra]
    return [sys.executable, "-m", "quikode.cli", "run", *extra]
