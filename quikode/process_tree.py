"""Linux process tree discovery + orphan detection (stdlib only).

The daemon supervisor's job is to manage the inner `quikode.cli run` child;
when `qk daemon stop` SIGTERMs the supervisor, the supervisor is supposed to
forward SIGTERM to its child and reap it. In practice the supervisor has
sometimes died before its child responded — leaving the child reparented to
PID 1 and ticking against a workspace the operator believes is clean.

This module gives the daemon-stop / status / reset commands the tools to:

  * walk a process's full descendant tree via `/proc/*/stat` ppid scans,
  * scan host-wide for orphaned `quikode.cli run` processes whose supervisor
    is dead.

Linux-only. Per the project layout we don't ship a Windows/macOS code path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ProcInfo",
    "discover_descendants",
    "find_orphan_quikode_runs",
    "process_alive",
    "read_cmdline",
    "read_ppid",
]


@dataclass(frozen=True)
class ProcInfo:
    """Snapshot of a process for diagnostic logging."""

    pid: int
    ppid: int
    cmdline: str

    def short_cmdline(self, limit: int = 120) -> str:
        cmd = self.cmdline or "<no cmdline>"
        return cmd if len(cmd) <= limit else cmd[: limit - 3] + "..."


def process_alive(pid: int) -> bool:
    """True if pid is alive AND not a zombie.

    `kill(pid, 0)` returns success even for zombie processes — the kernel
    keeps the pid reserved until the parent reaps. For our use case
    (deciding whether to keep waiting / SIGKILL again), zombies are dead.
    We read /proc/<pid>/stat field 3 (state) and treat 'Z' as dead.
    """
    if pid <= 0:
        return False
    if not _pid_signalable(pid):
        return False
    return _proc_state(pid) != "Z"


def _pid_signalable(pid: int) -> bool:
    """Probe for pid existence via signal 0. PermissionError counts as alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _proc_state(pid: int) -> str:
    """Read /proc/<pid>/stat field 3 (single-char state). Empty on failure.

    Anchored on the last ')' of the comm field so processes with spaces in
    their command name don't fool the parser.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes().decode("utf-8", "replace")
    except OSError:
        return ""
    rparen = raw.rfind(")")
    if rparen < 0:
        return ""
    rest = raw[rparen + 1 :].split()
    return rest[0] if rest else ""


def read_cmdline(pid: int) -> str:
    """Return the process's cmdline (NUL → space). Empty string on failure."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    if not raw:
        return ""
    # /proc/<pid>/cmdline uses NUL separators; trailing NUL is normal.
    return raw.rstrip(b"\x00").replace(b"\x00", b" ").decode("utf-8", "replace")


def read_ppid(pid: int) -> int | None:
    """Return parent pid from /proc/<pid>/stat, or None if unreadable.

    /proc/<pid>/stat field 4 is ppid. The comm field (2) is parenthesized
    and may contain spaces, so we anchor on the final ')' and split the
    remainder; ppid is the second whitespace-separated field after that.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    rparen = raw.rfind(")")
    if rparen < 0:
        return None
    rest = raw[rparen + 1 :].split()
    if len(rest) < 2:
        return None
    try:
        return int(rest[1])
    except ValueError:
        return None


def _all_pids() -> list[int]:
    pids: list[int] = []
    try:
        entries = [p.name for p in Path("/proc").iterdir()]
    except OSError:
        return pids
    for name in entries:
        if name.isdigit():
            try:
                pids.append(int(name))
            except ValueError:
                continue
    return pids


def discover_descendants(root_pid: int) -> list[ProcInfo]:
    """Return ALL descendants of root_pid (children, grandchildren, ...).

    Order: BFS by generation. Excludes root_pid itself. Self-protective:
    skips the calling process if it would be returned (defensive — a tool
    asking for its own child tree shouldn't accidentally signal itself).
    """
    self_pid = os.getpid()
    pids = _all_pids()
    by_ppid: dict[int, list[int]] = {}
    for pid in pids:
        ppid = read_ppid(pid)
        if ppid is None:
            continue
        by_ppid.setdefault(ppid, []).append(pid)

    descendants: list[ProcInfo] = []
    seen: set[int] = set()
    queue: list[int] = list(by_ppid.get(root_pid, []))
    while queue:
        pid = queue.pop(0)
        if pid in seen or pid == self_pid:
            continue
        seen.add(pid)
        ppid = read_ppid(pid) or 0
        cmdline = read_cmdline(pid)
        descendants.append(ProcInfo(pid=pid, ppid=ppid, cmdline=cmdline))
        queue.extend(by_ppid.get(pid, []))
    return descendants


def find_orphan_quikode_runs(*, exclude_pids: set[int] | None = None) -> list[ProcInfo]:
    """Scan the host for live `quikode.cli run` processes.

    Detection signal: cmdline contains both "quikode.cli" and "run", OR
    contains both "quikode" (binary name) and "run" with `--max-parallel`.
    The latter form catches the installed `qk`/`quikode` console-script.

    Filters out:
      * the calling process,
      * any pid in `exclude_pids` (typically the supervisor pid itself),
      * processes whose cmdline lacks the run-mode marker (we don't want
        to flag a sibling `qk daemon status` invocation).
    """
    excluded: set[int] = set(exclude_pids or set())
    excluded.add(os.getpid())
    matches: list[ProcInfo] = []
    for pid in _all_pids():
        if pid in excluded:
            continue
        cmd = read_cmdline(pid)
        if not cmd:
            continue
        if not _looks_like_quikode_run(cmd):
            continue
        ppid = read_ppid(pid) or 0
        matches.append(ProcInfo(pid=pid, ppid=ppid, cmdline=cmd))
    return matches


def _looks_like_quikode_run(cmd: str) -> bool:
    """Best-effort cmdline match for an inner `quikode.cli run` invocation.

    We accept either:
      * `... -m quikode.cli run ...`           (the supervisor's spawn form)
      * `.../bin/quikode run ...`               (the console-script form)
      * `.../bin/qk run ...`                    (the short alias)
    AND require either `--max-parallel` or `--retry-failed` so we don't
    match short-lived `qk run --help` invocations.
    """
    has_module_form = "quikode.cli" in cmd and " run" in cmd
    has_script_form = (
        "/quikode " in cmd or cmd.endswith("/quikode") or " quikode " in cmd or "/qk " in cmd
    ) and " run" in cmd
    if not (has_module_form or has_script_form):
        return False
    return "--max-parallel" in cmd or "--retry-failed" in cmd or "--milestone" in cmd or "--only" in cmd
