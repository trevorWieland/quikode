"""Shutdown + orphan-detection helpers for the daemon supervisor.

Split out of `daemon.py` to keep that file under the architecture line-budget
and to make the new (post-incident) lifecycle code easy to navigate. The
public surface here is:

  * `stop_daemon(cfg, *, timeout_s, log_fn)` — terminate supervisor + tree
  * `detect_orphan_quikode_runs(cfg)` — find live `quikode.cli run` pids
  * `cleanup_lifecycle_files(cfg)` — idempotent pid+heartbeat removal

Internal helpers (`_signal_pid`, `_sigkill_order`, etc.) are kept module-private.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path

from .config import Config
from .process_tree import (
    ProcInfo,
    discover_descendants,
    find_orphan_quikode_runs,
    process_alive,
    read_cmdline,
)

log = logging.getLogger(__name__)

CHILD_TERM_TIMEOUT_S = 30
TERM_POLL_INTERVAL_S = 0.5
SIGKILL_GRACE_S = 5


def _daemon_pid_file(cfg: Config) -> Path:
    return cfg.state_dir / "daemon.pid"


def _heartbeat_file(cfg: Config) -> Path:
    return cfg.state_dir / "orchestrator.heartbeat"


def _read_daemon_pid(cfg: Config) -> tuple[int | None, float | None]:
    p = _daemon_pid_file(cfg)
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


def _pid_alive_for_stop(pid: int) -> bool:
    """Same liveness check `daemon.py` uses, minus zombie reaping.

    The zombie filter in `process_tree.process_alive` is what we want for
    the busy-wait loop; for the initial "is the supervisor alive at all"
    check we just need a kernel signal probe.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _signal_pid(pid: int, sig: int) -> bool:
    """Send `sig` to `pid`, swallowing ProcessLookupError. True if delivered."""
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        return False
    except OSError as exc:
        log.warning("daemon stop: kill(%d, %d) failed: %s", pid, sig, exc)
        return False
    return True


def cleanup_lifecycle_files(cfg: Config) -> None:
    """Remove pid + heartbeat files. Idempotent."""
    for path in (_daemon_pid_file(cfg), _heartbeat_file(cfg)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("daemon stop: could not remove %s: %s", path, exc)


def _default_log_fn(msg: str) -> None:
    log.info(msg)


def stop_daemon(
    cfg: Config,
    *,
    timeout_s: int = CHILD_TERM_TIMEOUT_S,
    log_fn: Callable[[str], None] | None = None,
) -> bool:
    """Terminate the supervisor + every descendant, then clean lifecycle files.

    Playbook (post-2026-05-08 incident):

      1. Read supervisor pid from the pid file.
      2. Discover its full descendant tree via `/proc` ppid scans.
      3. SIGTERM the supervisor and every descendant explicitly. The
         supervisor's own SIGTERM handler also forwards to the child, but
         we cannot trust that path alone — the supervisor died first
         tonight and orphan'd its child for 12+ minutes.
      4. Wait up to `timeout_s` for everyone to exit; emit progress every 5s.
      5. SIGKILL anything still alive (supervisor first, then ordinary
         children, then docker-exec descendants last so dockerd can reap).
      6. `SIGKILL_GRACE_S` more for kernel reaping.
      7. Always remove pid + heartbeat files on exit. Clean files == "no
         daemon" — that's the contract `qk daemon status` and `qk reset`
         depend on.

    Returns True if every process terminated within budget; False if any
    pid is still alive after SIGKILL grace expired (operator must
    `kill -9` manually).
    """
    log_fn = log_fn or _default_log_fn
    pid, _ = _read_daemon_pid(cfg)
    if pid is None or not _pid_alive_for_stop(pid):
        if pid is not None:
            log_fn(f"daemon stop: supervisor pid={pid} not alive — cleaning up stale files")
        cleanup_lifecycle_files(cfg)
        return False

    # Snapshot descendants BEFORE signaling: a fork-then-die race would
    # otherwise hide the orphan from us.
    descendants = discover_descendants(pid)
    log_fn(f"daemon stop: SIGTERM supervisor pid={pid}")
    _signal_pid(pid, signal.SIGTERM)
    for proc in descendants:
        log_fn(f"daemon stop: SIGTERM child pid={proc.pid} cmdline={proc.short_cmdline()!r}")
        _signal_pid(proc.pid, signal.SIGTERM)

    all_pids: list[int] = [pid] + [p.pid for p in descendants]
    deadline = time.time() + timeout_s
    last_progress = time.time()
    while time.time() < deadline:
        alive = [p for p in all_pids if process_alive(p)]
        if not alive:
            log_fn("daemon stop: clean — all processes terminated")
            cleanup_lifecycle_files(cfg)
            return True
        if time.time() - last_progress >= 5.0:
            remaining = deadline - time.time()
            log_fn(f"daemon stop: still waiting on {len(alive)} pid(s): {alive} ({remaining:.0f}s remaining)")
            last_progress = time.time()
        time.sleep(TERM_POLL_INTERVAL_S)

    # Some pids didn't honor SIGTERM. Remove lifecycle files BEFORE the
    # final kill so a late heartbeat write from the dying child can't
    # resurrect a stale view.
    cleanup_lifecycle_files(cfg)
    survivors = [p for p in all_pids if process_alive(p)]
    if survivors:
        for kp in _sigkill_order(pid, survivors):
            log_fn(f"daemon stop: SIGKILL pid={kp} (didn't exit in {timeout_s}s)")
            _signal_pid(kp, signal.SIGKILL)
        kill_deadline = time.time() + SIGKILL_GRACE_S
        while time.time() < kill_deadline:
            survivors = [p for p in survivors if process_alive(p)]
            if not survivors:
                break
            time.sleep(TERM_POLL_INTERVAL_S)

    # One more delete in case the dying child flushed a heartbeat write
    # between our delete-before-kill and now.
    cleanup_lifecycle_files(cfg)
    survivors = [p for p in all_pids if process_alive(p)]
    if survivors:
        for sp in survivors:
            log.error(
                "daemon stop: pid=%d STILL ALIVE after SIGKILL — manual `kill -9 %d` needed (cmdline=%r)",
                sp,
                sp,
                read_cmdline(sp),
            )
        return False
    log_fn("daemon stop: clean — pid+heartbeat files removed (after SIGKILL fallback)")
    return True


def _sigkill_order(supervisor_pid: int, survivors: list[int]) -> list[int]:
    """Order: supervisor → ordinary children → docker-exec descendants.

    docker exec / docker run descendants can hold open file handles
    dockerd needs to reap; killing them last gives dockerd the best
    shot at clean teardown.
    """
    sup = [p for p in survivors if p == supervisor_pid]
    docker_pids: list[int] = []
    others: list[int] = []
    for p in survivors:
        if p == supervisor_pid:
            continue
        cmd = read_cmdline(p)
        if "docker" in cmd:
            docker_pids.append(p)
        else:
            others.append(p)
    return sup + others + docker_pids


def detect_orphan_quikode_runs(cfg: Config) -> tuple[ProcInfo | None, list[ProcInfo]]:
    """Detect orphaned `quikode.cli run` processes when supervisor is dead.

    Returns (supervisor_proc | None, orphan_children).

    `supervisor_proc` is non-None only when the pid-file's supervisor pid
    is alive — caller can use it for "supervisor still running, refuse".
    The second element is the danger case: pid file says supervisor is
    dead (or missing) but a `quikode.cli run` is still ticking under PID 1.
    """
    pid, _ = _read_daemon_pid(cfg)
    supervisor_alive = pid is not None and _pid_alive_for_stop(pid)
    exclude: set[int] = {pid} if supervisor_alive and pid is not None else set()
    orphans = find_orphan_quikode_runs(exclude_pids=exclude)
    sup_proc: ProcInfo | None = None
    if supervisor_alive and pid is not None:
        sup_proc = ProcInfo(pid=pid, ppid=0, cmdline=read_cmdline(pid))
    return sup_proc, orphans
