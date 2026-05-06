"""Crash-restart supervisor for `quikode run`."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

from . import worktree
from .config import Config

log = logging.getLogger(__name__)

CHILD_TERM_TIMEOUT_S = 30
TERM_POLL_INTERVAL_S = 0.5


class ChildProcess(Protocol):
    pid: int
    returncode: int | None

    def wait(self, timeout: int | float | None = None) -> int: ...

    def poll(self) -> int | None: ...

    def send_signal(self, sig: int) -> None: ...

    def kill(self) -> None: ...


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


def _terminate_child(child: ChildProcess, *, timeout_s: int | float = CHILD_TERM_TIMEOUT_S) -> int:
    """Send SIGTERM, wait up to timeout, then SIGKILL. Return final exit code."""
    rc = child.poll()
    if rc is not None:
        return int(rc)
    try:
        child.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        return int(child.returncode or 0)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rc = child.poll()
        if rc is not None:
            return int(rc)
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
    child: ChildProcess, timeout_s: int | float = CHILD_TERM_TIMEOUT_S
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


def detach_into_background(log_path: Path) -> int:
    """Fork into a session-leader child writing to `log_path`. Parent returns child PID.

    The child:
      - calls `os.setsid()` so it survives SIGHUP from the controlling terminal,
      - re-opens stdin from /dev/null,
      - redirects stdout/stderr to `log_path` (append mode).

    The parent returns the child's PID. The child returns 0 — the caller
    distinguishes the two by checking the return value, like `os.fork()`.

    Why not double-fork? `os.setsid` already detaches from the controlling
    terminal; the second fork only matters if you're worried about reacquiring
    one via opening a tty device, which we never do.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    child_pid = os.fork()
    if child_pid > 0:
        # Parent — return child's pid so the caller can announce it.
        return child_pid

    # Child path. New session, detach from controlling terminal.
    os.setsid()

    # Redirect stdio. /dev/null for stdin; append to log_path for the rest.
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)

    log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(log_fd)

    return 0


def _cleanup_pid_file(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


# Watchdog: how often to read the heartbeat while the child runs. Cheap
# (one stat + one tiny json parse) so a 5s cadence is fine.
_WATCHDOG_POLL_S = 5.0


def _wait_with_watchdog(
    cfg: Config,
    child: subprocess.Popen,
    state: _SupervisorState,
    log_fp,
    sleep_fn,
) -> int:
    """Wait for `child` to exit, but SIGTERM it if the heartbeat goes stale.

    Returns the child's final return code. Two consecutive stale reads (over
    `cfg.daemon_heartbeat_stale_kill_s`) are required before we kill — single
    missed windows shouldn't trip on bursty heartbeat writers.

    A `daemon_heartbeat_stale_kill_s <= 0` setting disables the watchdog and
    falls back to the previous wait-forever behavior.

    Implementation note: we use `child.wait(timeout=...)` rather than a poll +
    sleep loop. wait() with a timeout actually reaps the child when it exits,
    so on real subprocess.Popen the next iteration sees the rc immediately;
    a poll/sleep loop would race the same outcome but spin a thread.
    """
    threshold = int(cfg.daemon_heartbeat_stale_kill_s)
    if threshold <= 0:
        return int(child.wait())

    consecutive_stale = 0
    while not state.shutdown:
        try:
            return int(child.wait(timeout=_WATCHDOG_POLL_S))
        except subprocess.TimeoutExpired:
            pass

        hb = read_heartbeat(cfg)
        now = time.time()
        if hb is None:
            # No heartbeat yet → orchestrator hasn't bootstrapped its writer.
            # Tolerate this until the child has been alive longer than the
            # staleness threshold; after that, treat absence as stale too.
            ts_age = now - _process_start_time(child.pid)
        else:
            try:
                ts_age = max(0.0, now - float(hb.get("ts", 0.0)))
            except (TypeError, ValueError):
                ts_age = float("inf")

        if ts_age > threshold:
            consecutive_stale += 1
            log_fp.write(
                f"--- [supervisor] heartbeat stale {ts_age:.0f}s > {threshold}s "
                f"(consecutive={consecutive_stale}) ---\n".encode()
            )
            log_fp.flush()
            if consecutive_stale >= 2:
                log.warning(
                    "supervisor: heartbeat stale %.0fs > %ds — killing inner orchestrator",
                    ts_age,
                    threshold,
                )
                log_fp.write("--- [supervisor] heartbeat watchdog firing — SIGTERM child ---\n".encode())
                log_fp.flush()
                # Treat as crash: forward SIGTERM, fall back to SIGKILL after
                # the standard timeout, and return the rc so the caller's
                # backoff path runs (state.shutdown stays False).
                return _terminate_child(child)
        else:
            consecutive_stale = 0

        sleep_fn(0)  # pacing hook; the wait() above is the real timer

    # Shutdown was requested while we were polling — let the caller's signal
    # handler path drive cleanup. Just wait for the child to exit.
    return int(child.wait())


def _process_start_time(pid: int) -> float:
    """Best-effort wall-clock start time for `pid` (epoch seconds).

    Used to bound the "no heartbeat yet" tolerance: if the inner orchestrator
    has been alive longer than the staleness threshold and still hasn't
    written a heartbeat, we treat it as stale.
    """
    try:
        # /proc/<pid>/stat field 22 is starttime in clock ticks since boot.
        # Skip the comm field (parenthesized, may contain spaces) by anchoring
        # on the final ')'. starttime is field 22 → index 19 in the post-')' split.
        with open(f"/proc/{pid}/stat", "rb") as fp:
            raw = fp.read().decode("utf-8", "replace")
        rest = raw[raw.rfind(")") + 1 :].split()
        starttime_ticks = int(rest[19])
        clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        with open("/proc/stat", "rb") as fp:
            for line in fp:
                if line.startswith(b"btime "):
                    btime = int(line.split()[1])
                    return float(btime) + starttime_ticks / clk_tck
    except (OSError, ValueError, IndexError, KeyError):
        pass
    # Fallback: pretend the process started "now" so we don't false-positive.
    return time.time()


def supervise(cfg: Config, run_args: list[str], *, sleep_fn=time.sleep) -> int:
    """Run the supervisor loop. Returns the exit code the daemon should exit with.

    `sleep_fn` is injectable for tests so backoff sleeps don't actually delay.
    """
    state = _SupervisorState()
    _install_signal_handlers(state)
    pid_path = _write_pid_file(cfg)

    log_path = daemon_log_file(cfg)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fp = log_path.open("ab")
    consecutive_crashes = 0
    schedule = list(cfg.daemon_backoff_schedule_s)
    min_run_reset = cfg.daemon_min_run_for_backoff_reset_s

    try:
        while not state.shutdown:
            _supervisor_prune_worktrees(cfg, log_fp)
            child_started = time.time()
            child = _supervisor_spawn_child(cfg, run_args, log_fp, state)
            rc = _supervisor_wait_for_child(cfg, child, state, log_fp, sleep_fn)
            state.current_child = None
            ran_for = time.time() - child_started
            _supervisor_log(log_fp, f"--- [supervisor] child exited rc={rc} after {ran_for:.0f}s ---")

            if state.shutdown:
                return _supervisor_shutdown_child(child, rc)

            if rc == 0:
                log.info("inner quikode run completed cleanly — supervisor exiting 0")
                return 0

            consecutive_crashes = _next_crash_count(consecutive_crashes, ran_for, min_run_reset, rc)
            backoff = _backoff_for_attempt(schedule, consecutive_crashes)
            if _supervisor_backoff(log_fp, sleep_fn, backoff, consecutive_crashes, state):
                log.info("supervisor: shutdown requested during backoff sleep")
                return 0
        return 0
    finally:
        try:
            log_fp.close()
        except OSError:
            pass
        _cleanup_pid_file(pid_path)


def _supervisor_log(log_fp: IO[bytes], line: str) -> None:
    log_fp.write((line + "\n").encode())
    log_fp.flush()


def _supervisor_prune_worktrees(cfg: Config, log_fp: IO[bytes]) -> None:
    try:
        pruned = worktree.prune_stale_worktrees(cfg.repo_path, cfg.worktree_root)
    except Exception as e:
        _supervisor_log(log_fp, f"--- [supervisor] worktree prune skipped: {e} ---")
        return
    if pruned:
        _supervisor_log(
            log_fp, f"--- [supervisor] pruned {len(pruned)} stale worktree dir(s) before spawn ---"
        )


def _supervisor_spawn_child(
    cfg: Config, run_args: list[str], log_fp: IO[bytes], state: _SupervisorState
) -> subprocess.Popen:
    _supervisor_log(
        log_fp, f"\n--- [supervisor] spawn quikode run at {time.strftime('%Y-%m-%dT%H:%M:%S')} ---"
    )
    child = _spawn_child(cfg, run_args, log_fp)
    state.current_child = child
    log.info("supervisor spawned child pid=%d", child.pid)
    return child


def _supervisor_wait_for_child(
    cfg: Config,
    child: subprocess.Popen,
    state: _SupervisorState,
    log_fp: IO[bytes],
    sleep_fn: Callable[[float], object],
) -> int:
    try:
        return _wait_with_watchdog(cfg, child, state, log_fp, sleep_fn)
    except KeyboardInterrupt:
        state.shutdown = True
        return _terminate_child(child)


def _supervisor_shutdown_child(child: subprocess.Popen, rc: int) -> int:
    if child.poll() is None:
        rc = _terminate_child(child)
    log.info("supervisor shutting down (child rc=%d)", rc)
    return 0


def _next_crash_count(previous: int, ran_for: float, min_run_reset: int, rc: int) -> int:
    if ran_for >= min_run_reset:
        log.info(
            "child ran %.0fs (>= %ds reset) before crashing rc=%d - backoff reset", ran_for, min_run_reset, rc
        )
        return 1
    next_count = previous + 1
    log.info("child crashed rc=%d after %.0fs (consecutive=%d)", rc, ran_for, next_count)
    return next_count


def _supervisor_backoff(
    log_fp: IO[bytes],
    sleep_fn: Callable[[float], object],
    backoff: int,
    consecutive_crashes: int,
    state: _SupervisorState,
) -> bool:
    _supervisor_log(
        log_fp, f"--- [supervisor] backoff {backoff}s before restart (consecutive={consecutive_crashes}) ---"
    )
    log.info("supervisor sleeping %ds before restart", backoff)
    sleep_fn(backoff)
    return state.shutdown


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
