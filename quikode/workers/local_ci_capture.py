"""Plan 53: capture `local_ci_command` at worktree HEAD for the
fixup-planner local-vs-CI signal.

Both the post-PR poll loop (`pr_lifecycle._handle_polled_ci_failure`)
and the daemon-driven CI-fix entry (`feedback.run_ci_fix_response`) run
this BEFORE invoking the fixup planner so the planner can dispatch on
the three-case ladder (GitHub fails AND local fails / passes / etc.).

Lives in its own module so `pr_lifecycle.py` stays under the 600-line
architecture budget and the helper has one source of truth instead of
being duplicated on each call site.
"""

from __future__ import annotations

from typing import Any


def capture_local_ci_at_head(worker: Any, _tw: Any) -> tuple[bool, str] | None:
    """Run the local-CI command against the worktree HEAD and return
    `(passed, excerpt)` so the fixup planner sees the local-vs-CI
    signal directly.

    Returns None when no container handle is available (worker without
    dev container) or when `local_ci_command` is empty in config —
    both shapes mean we cannot honestly capture the signal and the
    prompt-side three-case dispatch will simply skip the local-CI
    section. Failures during execution (timeout, OSError) also return
    None and log a warning; we never let a launch-side capture
    failure derail the actual fixup-planner call.

    Persists a `local_ci_at_head` artifact on the task row so `qk show`
    surfaces the local-CI evidence the planner saw.
    """
    cmd_str = (worker.cfg.local_ci_command or "").strip()
    if not cmd_str:
        return None
    if getattr(worker, "handle", None) is None:
        return None
    try:
        rc, stdout, stderr = _tw.exec_in(
            worker._h,
            ["bash", "-lc", f"cd /workspace && {cmd_str}"],
            log_path=worker.log_path,
            timeout=worker.cfg.local_ci_timeout_s,
        )
    except (_tw.subprocess.TimeoutExpired, OSError) as exc:
        _tw.log.warning(
            "local_ci_at_head capture raised %s for task %s; passing None to the fixup planner",
            exc,
            worker.node.id,
        )
        return None
    passed = rc == 0
    blob = (stdout or "") + ("\n" + stderr if stderr else "")
    excerpt = _tw._last_lines(blob, 40)
    try:
        worker.store.add_artifact(
            worker.node.id,
            "local_ci_at_head",
            f"rc={rc}; passed={passed}\n\n{blob[:20000]}",
        )
    except Exception as exc:
        _tw.log.debug("local_ci_at_head artifact persist failed: %s", exc)
    return (passed, excerpt)


__all__ = ["capture_local_ci_at_head"]
