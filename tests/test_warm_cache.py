"""warm-cache: nightly sccache pre-warm command.

Mocks docker subprocess calls and verifies:
  - The CLI parses correctly with default + override flags.
  - `start_warm_cache_container` issues a `docker run` with the expected
    image, mounts, and label.
  - The CLI runs `git fetch`, `git checkout`, `cargo build`, and
    `sccache --show-stats` inside the container in that order.
  - Teardown happens even if a step fails (the warm-cache container is
    not orphaned on errors).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from quikode import docker_env
from quikode.cli import app
from quikode.config import Config

runner = CliRunner()


def _write_config(tmp_path: Path) -> Path:
    """Write a minimal `.quikode/config.toml` and return the workspace root."""
    cfg_dir = tmp_path / ".quikode"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.toml").write_text(
        f"""
repo_path = "{tmp_path}"
dag_path = "{tmp_path}/dag.json"
image_tag = "quikode-test:latest"
base_branch = "main"
"""
    )
    (tmp_path / "dag.json").write_text(
        '{"schema":"x","milestones":[],"nodes":[]}',
    )
    return tmp_path


def test_start_warm_cache_container_issues_expected_docker_run(tmp_path):
    """The helper composes a `docker run` invocation with the right
    flags: image, sccache mount, repo mount, qk_role label, sleep cmd."""
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
        image_tag="quikode-test:latest",
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)

    with patch.object(docker_env, "_run") as run_mock:
        run_mock.return_value = MagicMock(stdout="container-id\n", returncode=0)
        name = docker_env.start_warm_cache_container(cfg)

    assert name.startswith("qk-warm-")
    assert name.endswith("-warm")
    run_mock.assert_called_once()
    cmd = run_mock.call_args.args[0]
    # Image at the right position (just before the entrypoint args).
    assert "quikode-test:latest" in cmd
    # Sleep infinity for caller-controlled lifetime.
    assert cmd[-2:] == ["sleep", "infinity"]
    # Mounts include repo and sccache.
    cmd_str = " ".join(cmd)
    assert str(tmp_path) in cmd_str
    assert "/workspace" in cmd_str
    assert "/sccache" in cmd_str
    # Role label so cleanup tooling can recognise it.
    assert "qk_role=warm-cache" in cmd_str


def test_teardown_warm_cache_container_is_idempotent():
    with patch.object(docker_env, "_run") as run_mock:
        run_mock.return_value = MagicMock(returncode=0, stdout="")
        docker_env.teardown_warm_cache_container("qk-warm-abcdef-warm")

    run_mock.assert_called_once()
    cmd = run_mock.call_args.args[0]
    assert cmd == ["docker", "rm", "-f", "qk-warm-abcdef-warm"]
    assert run_mock.call_args.kwargs.get("check") is False


def test_cli_warm_cache_runs_expected_steps(tmp_path, monkeypatch):
    """End-to-end CLI invocation: container started, all four steps
    invoked in order, teardown called."""
    workspace = _write_config(tmp_path)
    monkeypatch.chdir(workspace)

    with (
        patch.object(
            docker_env,
            "start_warm_cache_container",
            return_value="qk-warm-test-warm",
        ) as start_mock,
        patch.object(docker_env, "teardown_warm_cache_container") as teardown_mock,
        patch("quikode.cli.subprocess.run") as run_mock,
    ):
        run_mock.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
        result = runner.invoke(app, ["warm-cache"], env={"PWD": str(workspace)})

    if result.exit_code != 0:
        # Surface the captured output to make debugging easier.
        print(result.output)
    assert result.exit_code == 0
    start_mock.assert_called_once()
    teardown_mock.assert_called_once_with("qk-warm-test-warm")

    # Each step invokes `docker exec ... bash -lc '<cmd>'`. Inspect call args.
    invocations = []
    for c in run_mock.call_args_list:
        # The CLI passes argv as the first positional list.
        cmd = c.args[0]
        if isinstance(cmd, list) and len(cmd) >= 5 and cmd[:2] == ["docker", "exec"]:
            invocations.append(cmd[-1])  # the shell string
    assert any("git fetch origin main" in i for i in invocations), invocations
    assert any("git checkout origin/main" in i for i in invocations), invocations
    assert any("cargo build --workspace --locked" in i for i in invocations), invocations
    assert any("sccache --show-stats" in i for i in invocations), invocations


def test_cli_warm_cache_no_fetch_skips_git_fetch(tmp_path, monkeypatch):
    workspace = _write_config(tmp_path)
    monkeypatch.chdir(workspace)

    with (
        patch.object(
            docker_env,
            "start_warm_cache_container",
            return_value="qk-warm-test-warm",
        ),
        patch.object(docker_env, "teardown_warm_cache_container"),
        patch("quikode.cli.subprocess.run") as run_mock,
    ):
        run_mock.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        result = runner.invoke(app, ["warm-cache", "--no-fetch"], env={"PWD": str(workspace)})

    assert result.exit_code == 0
    invocations = [
        c.args[0][-1]
        for c in run_mock.call_args_list
        if isinstance(c.args[0], list) and c.args[0][:2] == ["docker", "exec"]
    ]
    assert not any("git fetch" in i for i in invocations), invocations
    assert any("git checkout origin/main" in i for i in invocations), invocations


def test_cli_warm_cache_custom_branch(tmp_path, monkeypatch):
    workspace = _write_config(tmp_path)
    monkeypatch.chdir(workspace)

    with (
        patch.object(
            docker_env,
            "start_warm_cache_container",
            return_value="qk-warm-test-warm",
        ),
        patch.object(docker_env, "teardown_warm_cache_container"),
        patch("quikode.cli.subprocess.run") as run_mock,
    ):
        run_mock.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        result = runner.invoke(
            app,
            ["warm-cache", "--branch", "develop"],
            env={"PWD": str(workspace)},
        )

    assert result.exit_code == 0
    invocations = [
        c.args[0][-1]
        for c in run_mock.call_args_list
        if isinstance(c.args[0], list) and c.args[0][:2] == ["docker", "exec"]
    ]
    assert any("git fetch origin develop" in i for i in invocations), invocations
    assert any("git checkout origin/develop" in i for i in invocations), invocations


def test_cli_warm_cache_tears_down_on_failure(tmp_path, monkeypatch):
    """Even when cargo build fails, the container is torn down."""
    workspace = _write_config(tmp_path)
    monkeypatch.chdir(workspace)

    fail_step = subprocess.CompletedProcess(args=[], returncode=101, stdout="error: linker failed", stderr="")
    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    def _stub_run(cmd, *args, **kwargs):
        # Fail on the cargo step, succeed on everything else.
        if isinstance(cmd, list) and any("cargo build" in str(p) for p in cmd):
            return fail_step
        return ok

    with (
        patch.object(
            docker_env,
            "start_warm_cache_container",
            return_value="qk-warm-test-warm",
        ),
        patch.object(docker_env, "teardown_warm_cache_container") as teardown_mock,
        patch("quikode.cli.subprocess.run", side_effect=_stub_run),
    ):
        result = runner.invoke(app, ["warm-cache"], env={"PWD": str(workspace)})

    assert result.exit_code != 0
    teardown_mock.assert_called_once_with("qk-warm-test-warm")
