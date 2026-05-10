"""Plan 53: pre-fixup-planner capture of `local_ci_command` at the
worktree HEAD so the planner sees the local-vs-CI signal.

`capture_local_ci_at_head` is a pure function: it reads the cfg, runs
the command via `_tw.exec_in`, persists an artifact, and returns
`(passed, excerpt) | None`. Tests stub `_tw.exec_in` to verify the
shape of the call and the return-value branches.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from quikode.workers.local_ci_capture import capture_local_ci_at_head


def _make_worker(*, local_ci_command: str = "just ci", handle: Any | None = None) -> Any:
    cfg = SimpleNamespace(
        local_ci_command=local_ci_command,
        local_ci_timeout_s=120,
    )
    return SimpleNamespace(
        cfg=cfg,
        node=SimpleNamespace(id="R-0099"),
        log_path="/tmp/task.log",
        store=MagicMock(),
        handle=handle,
        _h=MagicMock(),
    )


def _make_tw(*, exec_result: tuple[int, str, str] | None = None, raise_exc: Exception | None = None) -> Any:
    """Stub `_tw` namespace with the symbols `capture_local_ci_at_head`
    pulls off it (`exec_in`, `subprocess`, `_last_lines`, `log`)."""

    def fake_exec_in(_h, _cmd, log_path=None, timeout=None):
        if raise_exc is not None:
            raise raise_exc
        assert exec_result is not None
        return exec_result

    return SimpleNamespace(
        exec_in=fake_exec_in,
        subprocess=subprocess,
        _last_lines=lambda s, n: "\n".join((s or "").splitlines()[-n:]),
        log=MagicMock(),
    )


def test_capture_returns_passed_excerpt_on_rc_zero():
    """Plan 53: rc==0 → returns `(True, excerpt)` and persists the
    `local_ci_at_head` artifact with the rc + blob captured."""
    worker = _make_worker(handle=MagicMock())
    tw = _make_tw(exec_result=(0, "all green", ""))
    out = capture_local_ci_at_head(worker, tw)
    assert out is not None
    passed, excerpt = out
    assert passed is True
    assert "all green" in excerpt
    # Artifact persisted with rc + blob.
    args = worker.store.add_artifact.call_args_list[0][0]
    assert args[1] == "local_ci_at_head"
    assert "rc=0" in args[2]
    assert "passed=True" in args[2]


def test_capture_returns_failed_excerpt_on_rc_nonzero():
    """Plan 53: rc != 0 → returns `(False, excerpt)`. The excerpt
    blends stdout and stderr so the planner sees the failure context."""
    worker = _make_worker(handle=MagicMock())
    tw = _make_tw(exec_result=(1, "tests passed", "FAIL: lint"))
    out = capture_local_ci_at_head(worker, tw)
    assert out is not None
    passed, excerpt = out
    assert passed is False
    assert "FAIL: lint" in excerpt


def test_capture_returns_none_when_command_empty():
    """Plan 53: empty `local_ci_command` → return None so the planner
    prompt skips the local-CI section entirely (no signal available)."""
    worker = _make_worker(local_ci_command="")
    tw = _make_tw(exec_result=(0, "ignored", ""))
    assert capture_local_ci_at_head(worker, tw) is None


def test_capture_returns_none_when_handle_missing():
    """Plan 53: no container handle (worker pre-provision or post-
    teardown) → return None. The planner sees None and routes through
    the standard non-local-CI prompt path."""
    worker = _make_worker(handle=None)
    tw = _make_tw(exec_result=(0, "ignored", ""))
    assert capture_local_ci_at_head(worker, tw) is None


def test_capture_returns_none_on_timeout():
    """Plan 53: a TimeoutExpired during exec_in returns None and logs
    a warning — we never let a launch-side capture failure derail the
    actual fixup planner call."""
    worker = _make_worker(handle=MagicMock())
    tw = _make_tw(raise_exc=subprocess.TimeoutExpired("just ci", 120))
    assert capture_local_ci_at_head(worker, tw) is None
    assert tw.log.warning.called


def test_capture_returns_none_on_oserror():
    """Plan 53: an OSError during exec_in (e.g. socket dead) returns
    None and logs a warning."""
    worker = _make_worker(handle=MagicMock())
    tw = _make_tw(raise_exc=OSError("docker socket gone"))
    assert capture_local_ci_at_head(worker, tw) is None
    assert tw.log.warning.called
