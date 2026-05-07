"""Phase 1B: ensure_dev_container_running idempotency + recreation path.

The 2026-05-07 incident showed that the orchestrator detected dead-container
failures correctly but never recreated the container before the next agent
call. This test pins the contract for the recovery path:

- When `docker inspect` reports running=true, no recreate happens.
- When inspect reports running=false (or fails entirely), the helper tears
  down + recreates postgres + dev container + waits for ready.
- Recreation fans out into the existing primitives (network_create,
  start_postgres, wait_postgres_healthy, start_dev_container,
  wait_dev_ready) so future maintainers don't accidentally let those drift.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from quikode import docker_env
from quikode.config import Config
from quikode.docker_env import TaskContainer


def _handle() -> TaskContainer:
    return TaskContainer(
        task_id="R-test",
        project_name="qk-r-test-abc123",
        workspace_id="r-test-abc123",
        container_name="qk-r-test-abc123-dev",
        pg_container_name="qk-r-test-abc123-pg",
        network_name="qk-r-test-abc123-net",
    )


def _cfg(tmp_path: Path) -> Config:
    return Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json")


def _completed(stdout: str = "", rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")


def test_is_dev_container_running_true(tmp_path):
    with patch("quikode.docker_env.subprocess.run", return_value=_completed("true\n", rc=0)):
        assert docker_env.is_dev_container_running(_handle()) is True


def test_is_dev_container_running_false_when_exited(tmp_path):
    with patch("quikode.docker_env.subprocess.run", return_value=_completed("false\n", rc=0)):
        assert docker_env.is_dev_container_running(_handle()) is False


def test_is_dev_container_running_false_when_missing(tmp_path):
    # docker inspect on a missing container exits non-zero
    with patch("quikode.docker_env.subprocess.run", return_value=_completed("", rc=1)):
        assert docker_env.is_dev_container_running(_handle()) is False


def test_ensure_dev_container_running_skips_when_alive(tmp_path):
    handle = _handle()
    cfg = _cfg(tmp_path)
    with (
        patch("quikode.docker_env.is_dev_container_running", return_value=True),
        patch("quikode.docker_env.start_dev_container") as start,
        patch("quikode.docker_env.start_postgres") as pg,
        patch("quikode.docker_env.wait_dev_ready") as wait,
    ):
        recreated = docker_env.ensure_dev_container_running(handle, cfg, tmp_path / "wt")
    assert recreated is False
    assert start.call_count == 0
    assert pg.call_count == 0
    assert wait.call_count == 0


def test_ensure_dev_container_running_recreates_when_dead(tmp_path):
    handle = _handle()
    cfg = _cfg(tmp_path)
    with (
        patch("quikode.docker_env.is_dev_container_running", return_value=False),
        patch("quikode.docker_env._run") as run,
        patch("quikode.docker_env.network_remove") as nrm,
        patch("quikode.docker_env.network_create") as ncr,
        patch("quikode.docker_env.start_postgres") as pg,
        patch("quikode.docker_env.wait_postgres_healthy") as wpg,
        patch("quikode.docker_env.start_dev_container") as start,
        patch("quikode.docker_env.wait_dev_ready") as wait,
    ):
        recreated = docker_env.ensure_dev_container_running(handle, cfg, tmp_path / "wt")
    assert recreated is True
    # Two pre-flight `docker rm -f` calls (dev + pg)
    assert run.call_count == 2
    nrm.assert_called_once_with(handle.network_name)
    assert ncr.call_count == 1
    pg.assert_called_once()
    wpg.assert_called_once_with(handle)
    start.assert_called_once_with(handle, cfg, tmp_path / "wt")
    wait.assert_called_once()
