"""Daemon supervisor for `quikode run`.

A thin wrapper that spawns `quikode run` as a child subprocess and restarts
it on crash with exponential backoff. The supervisor:

- Writes its OWN PID file at `state_dir/daemon.pid` (separate from
  `orchestrator.pid` written by the inner `quikode run`).
- Appends a daemon log at `state_dir/logs/daemon.log` (TODO: rotation).
- On child clean exit (rc=0): supervisor exits 0. No restart.
- On child crash (rc!=0): supervisor sleeps with backoff (per cfg
  `daemon_backoff_schedule_s`, default `[60, 300, 1800]` = 1m/5m/30m,
  capped). Backoff resets to the first entry after a successful run of at
  least `cfg.daemon_min_run_for_backoff_reset_s` seconds (default 5m).
- On supervisor SIGTERM/SIGINT: forwards SIGTERM to the child, waits up to
  30s for graceful exit, then SIGKILL. Supervisor exits cleanly.

Status is read from `daemon.pid` + `orchestrator.heartbeat`. The heartbeat
is written by the inner orchestrator; the supervisor itself never writes
it (otherwise stale-detection wouldn't work when the child crashes).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import worktree
from .config import Config

log = logging.getLogger(__name__)

# How long we wait after sending SIGTERM to the child before SIGKILL.
CHILD_TERM_TIMEOUT_S = 30

# Polling interval while waiting on child termination.
TERM_POLL_INTERVAL_S = 0.5


def daemon_pid_file(cfg: Config) -> Path:
    return cfg.state_dir / "daemon.pid"


def heartbeat_file(cfg: Config) -> Path:
    return cfg.state_dir / "orchestrator.heartbeat"


def daemon_log_file(cfg: Config) -> Path:
    return cfg.log_dir / "daemon.log"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    # If the process is our child, reap zombies first so kill(pid, 0) doesn't
    # report "alive" for an already-dead-but-unreaped child.
    try:
        reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
        if reaped_pid == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_daemon_pid(cfg: Config) -> tuple[int | None, float | None]:
    """Return (pid, started_ts) from the daemon pid file, or (None, None)."""
    p = daemon_pid_file(cfg)
    if not p.exists():
        return None, None
    try:
        raw = p.read_text().strip()
        pid_str, _, ts_str = raw.partition("@")
        pid = int(pid_str)
        ts = float(ts_str) if ts_str else None
    except (ValueError, OSError):
        return None, None
    return pid, ts


def read_heartbeat(cfg: Config) -> dict | None:
    """Return parsed heartbeat JSON, or None if absent / unreadable."""
    p = heartbeat_file(cfg)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class DaemonStatus:
    daemon_pid: int | None
    daemon_alive: bool
    daemon_started_ts: float | None
    heartbeat_data: dict | None
    heartbeat_age_s: float | None
    heartbeat_stale: bool
    staleness_threshold_s: int

    @property
    def daemon_uptime_s(self) -> float | None:
        if self.daemon_started_ts is None:
            return None
        return max(0.0, time.time() - self.daemon_started_ts)

    def to_json_dict(self) -> dict:
        return {
            "daemon_pid": self.daemon_pid,
            "daemon_alive": self.daemon_alive,
            "daemon_started_ts": self.daemon_started_ts,
            "daemon_uptime_s": self.daemon_uptime_s,
            "heartbeat": self.heartbeat_data,
            "heartbeat_age_s": self.heartbeat_age_s,
            "heartbeat_stale": self.heartbeat_stale,
            "staleness_threshold_s": self.staleness_threshold_s,
        }


def get_status(cfg: Config) -> DaemonStatus:
    pid, ts = read_daemon_pid(cfg)
    alive = pid is not None and _pid_alive(pid)
    hb = read_heartbeat(cfg)
    age: float | None = None
    stale = False
    threshold = cfg.daemon_heartbeat_staleness_s
    if hb is not None:
        try:
            hb_ts = float(hb.get("ts", 0.0))
            age = max(0.0, time.time() - hb_ts)
            stale = age > threshold
        except (TypeError, ValueError):
            age = None
            stale = False
    return DaemonStatus(
        daemon_pid=pid,
        daemon_alive=alive,
        daemon_started_ts=ts,
        heartbeat_data=hb,
        heartbeat_age_s=age,
        heartbeat_stale=stale,
        staleness_threshold_s=threshold,
    )


def _backoff_for_attempt(schedule: list[int], attempt: int) -> int:
    """Pick the backoff delay for the Nth consecutive crash (1-indexed).

    For attempt > len(schedule), returns the last (cap) value.
    Empty schedule falls back to a 60s cap so we never spin.
    """
    if not schedule:
        return 60
    idx = max(0, min(attempt - 1, len(schedule) - 1))
    return int(schedule[idx])


def _quikode_run_argv(extra: list[str]) -> list[str]:
    """Build the argv used to spawn the inner `quikode run`.

    Honors the `QUIKODE_BIN` environment variable for tests/dev so we can
    swap in a stub binary without monkeypatching at the subprocess layer.
    """
    bin_override = os.environ.get("QUIKODE_BIN")
    if bin_override:
        return [bin_override, "run", *extra]
    return [sys.executable, "-m", "quikode.cli", "run", *extra]


def _spawn_child(cfg: Config, run_args: list[str], log_fp) -> subprocess.Popen:
    argv = _quikode_run_argv(run_args)
    # Spawn the child with the WORKSPACE dir as cwd, not the target repo. The
    # inner `quikode run` calls `load_config()` which walks up from cwd looking
    # for `.quikode/config.toml`. `cfg.repo_path` is the target source repo
    # (e.g. /home/.../tanren) — config.toml lives in the *workspace* (e.g.
    # /home/.../tanren-runs/) which is always state_dir.parent.
    workspace = cfg.state_dir.parent
    return subprocess.Popen(
        argv,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        cwd=str(workspace),
        # Don't `start_new_session=True` — we want the child to receive our
        # signals normally if we choose to forward them. We forward SIGTERM
        # explicitly anyway; the difference is mainly about Ctrl-C in an
        # interactive terminal, which we DO want to swallow at the
        # supervisor and forward intentionally.
    )


def _terminate_child(child: subprocess.Popen, *, timeout_s: int = CHILD_TERM_TIMEOUT_S) -> int:
    """Send SIGTERM, wait up to timeout, then SIGKILL. Return final exit code."""
    if child.poll() is not None:
        return int(child.returncode)
    try:
        child.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        return int(child.returncode or 0)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if child.poll() is not None:
            return int(child.returncode)
        time.sleep(TERM_POLL_INTERVAL_S)
    # Hard kill
    try:
        child.kill()
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    return int(child.returncode or -9)


class _SupervisorState:
    """Mutable state for the supervisor loop. Module-level so signal
    handlers can flip the flag without juggling closures."""

    def __init__(self) -> None:
        self.shutdown = False
        self.current_child: subprocess.Popen | None = None


def _schedule_failsafe_kill(
    child: subprocess.Popen, timeout_s: int = CHILD_TERM_TIMEOUT_S
) -> threading.Timer:
    """Schedule SIGKILL on `child` after `timeout_s` if it's still alive.

    Module-level so tests can mock it. Returns the Timer (started) so callers
    can cancel it on a clean exit.
    """

    def _kill_if_still_alive() -> None:
        if child.poll() is None:
            log.warning(
                "supervisor: child pid=%d ignored SIGTERM after %ds — sending SIGKILL",
                child.pid,
                timeout_s,
            )
            try:
                child.send_signal(signal.SIGKILL)
            except ProcessLookupError:
                pass

    timer = threading.Timer(timeout_s, _kill_if_still_alive)
    timer.daemon = True
    timer.start()
    return timer


def _install_signal_handlers(state: _SupervisorState) -> None:
    def _handler(signum, _frame):
        log.info("supervisor received signal %d, forwarding to child + shutting down", signum)
        state.shutdown = True
        ch = state.current_child
        if ch is not None and ch.poll() is None:
            try:
                ch.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
            # Failsafe: if the inner orchestrator doesn't obey SIGTERM within
            # CHILD_TERM_TIMEOUT_S (e.g. it's blocked on a long-running agent
            # subprocess.run), fire SIGKILL from a background timer. Without
            # this, child.wait() hangs forever; `stop_daemon` then SIGKILLs
            # the supervisor — orphaning the child against a now cleaned-up
            # workspace. The orphan keeps running and chews retry budget in
            # 1-second cycles against a dead container.
            _schedule_failsafe_kill(ch)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def _write_pid_file(cfg: Config) -> Path:
    p = daemon_pid_file(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{os.getpid()}@{time.time():.0f}\n")
    return p


def _cleanup_pid_file(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def supervise(cfg: Config, run_args: list[str], *, sleep_fn=time.sleep) -> int:
    """Run the supervisor loop. Returns the exit code the daemon should exit with.

    `sleep_fn` is injectable for tests so backoff sleeps don't actually delay.
    """
    state = _SupervisorState()
    _install_signal_handlers(state)
    pid_path = _write_pid_file(cfg)

    log_path = daemon_log_file(cfg)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Append-only for now. TODO: rotate (e.g., size-based or daily) once the
    # daemon has soaked enough that we know typical log volumes.
    log_fp = log_path.open("ab")

    consecutive_crashes = 0
    schedule = list(cfg.daemon_backoff_schedule_s)
    min_run_reset = cfg.daemon_min_run_for_backoff_reset_s

    try:
        while not state.shutdown:
            # Defensive worktree prune before each (re)spawn. A crashed inner
            # `quikode run` can leave stale dirs that, if reused on the
            # restart, confuse `git worktree add`. Best-effort: failures here
            # are logged but never block the spawn.
            try:
                pruned = worktree.prune_stale_worktrees(cfg.repo_path, cfg.worktree_root)
                if pruned:
                    log_fp.write(
                        f"--- [supervisor] pruned {len(pruned)} stale worktree dir(s) before spawn ---\n".encode()
                    )
                    log_fp.flush()
            except Exception as e:
                log_fp.write(f"--- [supervisor] worktree prune skipped: {e} ---\n".encode())
                log_fp.flush()

            child_started = time.time()
            log_fp.write(
                f"\n--- [supervisor] spawn quikode run at {time.strftime('%Y-%m-%dT%H:%M:%S')} ---\n".encode()
            )
            log_fp.flush()
            child = _spawn_child(cfg, run_args, log_fp)
            state.current_child = child
            log.info("supervisor spawned child pid=%d", child.pid)

            # Block until the child exits. We don't need a wakeup loop — when
            # the supervisor receives SIGTERM, the handler forwards SIGTERM to
            # the child, which triggers the child's own graceful shutdown and
            # eventually returns from .wait().
            try:
                rc = child.wait()
            except KeyboardInterrupt:
                # If KeyboardInterrupt sneaks past the signal handler (race
                # during install), force the same shutdown path.
                state.shutdown = True
                rc = _terminate_child(child)

            state.current_child = None
            ran_for = time.time() - child_started
            log_fp.write(f"--- [supervisor] child exited rc={rc} after {ran_for:.0f}s ---\n".encode())
            log_fp.flush()

            if state.shutdown:
                # We were asked to stop. If the child didn't exit cleanly on
                # the SIGTERM we forwarded, finish it off.
                if child.poll() is None:
                    rc = _terminate_child(child)
                log.info("supervisor shutting down (child rc=%d)", rc)
                # Daemon was asked to stop. Always exit 0 — distinguishing
                # "child obeyed SIGTERM" from "child crashed during shutdown"
                # isn't meaningful from the operator's perspective: they asked
                # for a stop and got one.
                return 0

            if rc == 0:
                log.info("inner quikode run completed cleanly — supervisor exiting 0")
                return 0

            # Crash path: backoff + restart
            if ran_for >= min_run_reset:
                consecutive_crashes = 1
                log.info(
                    "child ran %.0fs (>= %ds reset) before crashing rc=%d — backoff reset",
                    ran_for,
                    min_run_reset,
                    rc,
                )
            else:
                consecutive_crashes += 1
                log.info(
                    "child crashed rc=%d after %.0fs (consecutive=%d)",
                    rc,
                    ran_for,
                    consecutive_crashes,
                )

            backoff = _backoff_for_attempt(schedule, consecutive_crashes)
            log_fp.write(
                f"--- [supervisor] backoff {backoff}s before restart (consecutive={consecutive_crashes}) ---\n".encode()
            )
            log_fp.flush()
            log.info("supervisor sleeping %ds before restart", backoff)
            sleep_fn(backoff)
            if state.shutdown:
                log.info("supervisor: shutdown requested during backoff sleep")
                return 0
        return 0
    finally:
        try:
            log_fp.close()
        except OSError:
            pass
        _cleanup_pid_file(pid_path)


def stop_daemon(cfg: Config, *, timeout_s: int = CHILD_TERM_TIMEOUT_S) -> bool:
    """Send SIGTERM to a running daemon. SIGKILL after timeout. Return True if stopped."""
    pid, _ = read_daemon_pid(cfg)
    if pid is None or not _pid_alive(pid):
        # Best-effort cleanup if pid file is stale
        if pid is not None:
            _cleanup_pid_file(daemon_pid_file(cfg))
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _cleanup_pid_file(daemon_pid_file(cfg))
        return True
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _pid_alive(pid):
            _cleanup_pid_file(daemon_pid_file(cfg))
            return True
        time.sleep(TERM_POLL_INTERVAL_S)
    # Fall back to SIGKILL
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    # Final tiny grace
    time.sleep(0.5)
    _cleanup_pid_file(daemon_pid_file(cfg))
    return not _pid_alive(pid)
