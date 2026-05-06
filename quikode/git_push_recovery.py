"""Push retry logic for `commit_subtask`.

Sits in its own module so `worktree.py` stays under the production line budget.
The recovery path: when `git push` is rejected as non-fast-forward (typically
because the doer ran `git reset`/`git rebase` and rewrote local history off
what's on the remote), fetch + rebase + retry. Fail closed if rebase
conflicts.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from .docker_env import TaskContainer

_NON_FAST_FORWARD_MARKERS = ("non-fast-forward", "rejected]", "fetch first")


def is_non_fast_forward(combined: str) -> bool:
    return bool(combined) and any(m in combined for m in _NON_FAST_FORWARD_MARKERS)


def push_with_recovery(
    handle: TaskContainer,
    branch: str,
    remote: str,
    *,
    exec_fn: Any,
    log_path: Path | None,
    timeout: int,
    is_transient: Any,
    success: Any,
    failure: Any,
):
    """Push, auto-rebase + retry on non-fast-forward. Returns the caller's
    success/failure dataclass instances via the passed-in factory callables —
    keeps the recovery logic provider-agnostic. `exec_fn` is the docker exec
    function (passed in so tests can patch the caller's `exec_in` and have it
    flow through)."""
    rc, out, err = _push(exec_fn, handle, branch, remote, log_path, timeout)
    if rc == 0:
        return success
    combined = (out or "") + "\n" + (err or "")
    if not is_non_fast_forward(combined):
        return failure(f"git push failed (rc={rc}):\n{combined}", transient=is_transient(rc, combined))
    rebase_err = _rebase_on_remote(exec_fn, handle, remote, branch, log_path, timeout)
    if rebase_err is not None:
        return failure(f"git push rejected non-fast-forward; auto-rebase failed:\n{rebase_err}")
    rc2, out2, err2 = _push(exec_fn, handle, branch, remote, log_path, timeout)
    if rc2 == 0:
        return success
    combined2 = (out2 or "") + "\n" + (err2 or "")
    return failure(
        f"git push failed after auto-rebase (rc={rc2}):\n{combined2}",
        transient=is_transient(rc2, combined2),
    )


def _push(exec_fn, handle, branch, remote, log_path, timeout):
    return exec_fn(
        handle,
        ["bash", "-lc", f"cd /workspace && git push {shlex.quote(remote)} {shlex.quote(branch)}"],
        log_path=log_path,
        timeout=timeout,
    )


def _rebase_on_remote(exec_fn, handle, remote, branch, log_path, timeout) -> str | None:
    cmd = (
        f"cd /workspace && git fetch {shlex.quote(remote)} {shlex.quote(branch)} "
        f"&& git rebase {shlex.quote(remote)}/{shlex.quote(branch)}"
    )
    rc, out, err = exec_fn(handle, ["bash", "-lc", cmd], log_path=log_path, timeout=timeout)
    if rc == 0:
        return None
    exec_fn(
        handle,
        ["bash", "-lc", "cd /workspace && git rebase --abort"],
        log_path=log_path,
        timeout=30,
    )
    return (out or "") + "\n" + (err or "")
