"""Phase 1D: `_acquire_orchestrator_lock` enforces singleton orchestrator.

The 2026-05-07 incident's FSM-cascade race happened because a second
`qk run` ran `cleanup_all_quikode` + `recover_orphan_tasks` against a
workspace where prior daemon's worker threads were still alive. The fix
puts an exclusive flock on `<state_dir>/orchestrator.lock` BEFORE any
destructive prep — second invocation exits 2 instead of clobbering.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest
import typer

from quikode import cli_core
from quikode.config import Config


def _cfg(tmp_path: Path) -> Config:
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json")
    cfg.state_dir = tmp_path / ".quikode"
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def teardown_function(_):
    """Release any module-level lock left behind by a test."""
    cli_core._release_orchestrator_lock()


def test_first_acquire_succeeds(tmp_path):
    cfg = _cfg(tmp_path)
    cli_core._acquire_orchestrator_lock(cfg)
    assert cli_core._orchestrator_lock_fd is not None
    assert (cfg.state_dir / "orchestrator.lock").exists()


def test_second_acquire_blocks(tmp_path):
    """Simulate a second invocation by holding the flock externally and
    asserting `_acquire_orchestrator_lock` exits with code 2."""
    cfg = _cfg(tmp_path)
    lock_path = cfg.state_dir / "orchestrator.lock"
    # Acquire the lock externally (as if another daemon held it).
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(typer.Exit) as exc:
            cli_core._acquire_orchestrator_lock(cfg)
        assert exc.value.exit_code == 2
        # The handle should NOT have been retained (second invocation must
        # not leak a partial handle on rejection).
        assert cli_core._orchestrator_lock_fd is None
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_release_clears_module_state(tmp_path):
    cfg = _cfg(tmp_path)
    cli_core._acquire_orchestrator_lock(cfg)
    assert cli_core._orchestrator_lock_fd is not None
    cli_core._release_orchestrator_lock()
    assert cli_core._orchestrator_lock_fd is None


def test_release_is_idempotent(tmp_path):
    """Calling release twice must not raise."""
    cli_core._release_orchestrator_lock()
    cli_core._release_orchestrator_lock()
    assert cli_core._orchestrator_lock_fd is None
