"""Host-side metric helpers for the TUI poller.

Pulled out of `store_polls.py` so that module stays under the
architecture-budget cap. Three helpers:

* `_read_host_caps` — read host CPU / memory from /proc.
* `_runtime_max_parallel` — pull the live `max_parallel` from the daemon
  heartbeat, falling back to `cfg.max_parallel` when no heartbeat.
* `_worktree_recent_mtime` — newest file mtime under a worktree,
  skipping churning cache + build dirs.

Underscore-prefixed names are kept so the existing imports inside
`store_polls.py` stay private to the controller package.
"""

from __future__ import annotations

import os
from pathlib import Path

from quikode.config import Config
from quikode.daemon import read_heartbeat


def _read_host_caps() -> tuple[int | None, int | None]:
    """Best-effort: read host CPU + memory from /proc."""
    cpus: int | None
    cpus = os.cpu_count()
    mem_gb: int | None = None
    try:
        with Path("/proc/meminfo").open() as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    mem_gb = round(kb / 1024 / 1024)
                    break
    except OSError:
        mem_gb = None
    return cpus, mem_gb


def _runtime_max_parallel(cfg: Config) -> int:
    """Live max_parallel from the daemon's heartbeat, falling back to config.

    `qk daemon start --max-parallel N` overrides cfg in the daemon process
    only — the on-disk config still says whatever's in config.toml. The
    daemon writes its effective value into the heartbeat each tick; reading
    it here keeps the TUI honest about what the running daemon is using.
    Falls back to cfg.max_parallel when no heartbeat is present (daemon
    stopped, fresh workspace).
    """
    hb = read_heartbeat(cfg)
    if hb is not None:
        value = hb.get("max_parallel")
        if isinstance(value, int) and value > 0:
            return value
    return int(cfg.max_parallel)


# Skip these path components when computing worktree mtime — they churn
# constantly even when the agent isn't actually working (cache lookups,
# build-tool metadata) and would mask real idleness.
_WORKTREE_MTIME_SKIP = (".git", "target", ".rumdl_cache", "node_modules", ".next", "__pycache__")


def _worktree_recent_mtime(worktree: Path) -> float | None:
    """Most recent file mtime under the worktree, ignoring caches/build dirs.

    Cheap-ish: walks once. The TUI polls every 1s; for a 200-file worktree
    this is ~10ms. For pathological worktrees we'd want a watchman-style
    delta but it's not the bottleneck right now.
    """
    if not worktree.exists():
        return None
    latest = 0.0
    try:
        for root, dirs, files in os.walk(worktree):
            # In-place prune: skip churning dirs.
            dirs[:] = [d for d in dirs if d not in _WORKTREE_MTIME_SKIP]
            for f in files:
                try:
                    m = (Path(root) / f).stat().st_mtime
                except OSError:
                    continue
                latest = max(latest, m)
    except OSError:
        return None
    return latest if latest > 0 else None
