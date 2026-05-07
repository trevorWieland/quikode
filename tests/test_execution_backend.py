from __future__ import annotations

import subprocess
from pathlib import Path

from quikode import docker_env, execution, pre_pr_audit, worktree
from quikode.agents import base as agent_base
from quikode.config import Config
from quikode.dag import DAG, Node
from quikode.execution import (
    FakeExecutionBackend,
    build_credential_bundle,
    build_execution_backend,
)
from quikode.github import exec_in as github_exec_in
from quikode.state import Store
from quikode.worker import TaskWorker


def _node(task_id: str = "T-001") -> Node:
    return Node(
        id=task_id,
        kind="behavior",
        milestone="M-1",
        title="x",
        scope="x",
        depends_on=(),
        completes_behaviors=(),
        supports_behaviors=(),
        boundary_with_neighbors="",
        expected_evidence=(),
        playbook=(),
        rationale="",
        risks=(),
        raw={},
    )


class _DAG(DAG):
    def __init__(self, node: Node):
        self.nodes = {node.id: node}


def _cfg(tmp_path: Path, **kw) -> Config:
    return Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        log_dir=tmp_path / ".quikode" / "logs",
        execution_backend="fake",
        **kw,
    )


def _worker(tmp_path: Path, **cfg_kw) -> TaskWorker:
    node = _node()
    cfg = _cfg(tmp_path, **cfg_kw)
    store = Store(tmp_path / "q.db")
    store.upsert_pending(node.id)
    return TaskWorker(cfg, _DAG(node), store, node)


def test_worker_provisions_executes_ensures_and_tears_down_through_fake_backend(tmp_path):
    worker = _worker(tmp_path)
    backend = worker.execution_backend
    assert isinstance(backend, FakeExecutionBackend)

    wt = tmp_path / "wt"
    wt.mkdir()
    worker._provision_container(wt)

    rc, out, err = execution.exec_in(worker._h, ["bash", "-lc", "echo ok"])
    assert (rc, out, err) == (0, "", "")

    recreated = worker.execution_backend.ensure_running(worker._h, wt)
    worker._teardown()

    assert recreated is False
    assert [c.name for c in backend.calls] == ["provision", "exec", "ensure_running", "teardown"]


def test_fake_backend_recreates_dead_sandbox_before_retry(tmp_path):
    worker = _worker(tmp_path)
    backend = worker.execution_backend
    assert isinstance(backend, FakeExecutionBackend)

    wt = tmp_path / "wt"
    wt.mkdir()
    worker._provision_container(wt)
    old_unit = worker._h.unit_id
    backend.mark_dead(worker._h)

    assert worker.execution_backend.ensure_running(worker._h, wt) is True
    assert worker._h.unit_id != old_unit
    assert worker._h.unit_id.endswith("-recreated")


def test_fake_backend_records_postgres_disabled_without_sidecar_request(tmp_path):
    worker = _worker(tmp_path, postgres_enabled=False)
    backend = worker.execution_backend
    assert isinstance(backend, FakeExecutionBackend)

    wt = tmp_path / "wt"
    wt.mkdir()
    worker._provision_container(wt)

    provision = backend.calls[0]
    assert provision.name == "provision"
    assert provision.postgres_requested is False


def test_agent_timeout_and_transient_classification_use_backend_exec(tmp_path):
    worker = _worker(tmp_path)
    backend = worker.execution_backend
    assert isinstance(backend, FakeExecutionBackend)
    wt = tmp_path / "wt"
    wt.mkdir()
    worker._provision_container(wt)

    backend.simulate_transient_exec_failure()
    transient = agent_base._exec(worker._h, ["agent"])
    assert transient.rc == 124
    assert transient.transient is True

    backend.exec_responses.append(subprocess.TimeoutExpired(cmd=["agent"], timeout=3))
    timed_out = agent_base._exec(worker._h, ["agent"], timeout=3)
    assert timed_out.rc == 124
    assert timed_out.transient is True


def test_helpers_import_backend_exec_interface():
    assert worktree.exec_in is execution.exec_in
    assert github_exec_in is execution.exec_in
    assert pre_pr_audit.exec_in is execution.exec_in


def test_backend_factory_and_credential_bundle(tmp_path, monkeypatch):
    claude_json = tmp_path / "claude.json"
    claude_json.write_text("{}")
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        claude_auth_dir=tmp_path / "claude",
        claude_json_path=claude_json,
        codex_auth_dir=tmp_path / "codex",
        opencode_auth_dir=tmp_path / "opencode-data",
        opencode_config_dir=tmp_path / "opencode-config",
        github_token_env="QK_TEST_GITHUB_TOKEN",
    )
    monkeypatch.setenv("QK_TEST_GITHUB_TOKEN", "secret")

    bundle = build_credential_bundle(cfg)
    by_name = {s.name: s for s in bundle.sources}
    assert by_name["claude_auth"].install_path == "/host-auth/claude"
    assert by_name["codex_auth"].install_path == "/host-auth/codex"
    assert by_name["opencode_data"].install_path == "/host-auth/opencode-data"
    assert by_name["opencode_config"].install_path == "/host-auth/opencode-config"
    assert by_name["claude_json"].install_path == "/host-auth/claude.json"
    assert bundle.github_env_name() == "QK_TEST_GITHUB_TOKEN"

    backend = build_execution_backend(cfg)
    assert backend.name == "docker"


def test_docker_start_dev_container_consumes_credential_bundle(tmp_path, monkeypatch):
    for dirname in ("claude", "codex", "opencode-data", "opencode-config", "worktree", ".git"):
        (tmp_path / dirname).mkdir()
    claude_json = tmp_path / "claude.json"
    claude_json.write_text("{}")
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        claude_auth_dir=tmp_path / "claude",
        claude_json_path=claude_json,
        codex_auth_dir=tmp_path / "codex",
        opencode_auth_dir=tmp_path / "opencode-data",
        opencode_config_dir=tmp_path / "opencode-config",
        sccache_dir=tmp_path / "sccache",
        github_token_env="QK_TEST_GITHUB_TOKEN",
    )
    monkeypatch.setenv("QK_TEST_GITHUB_TOKEN", "secret-token")
    bundle = build_credential_bundle(cfg)
    handle = docker_env.TaskContainer("T-1", "qk-t-1", "t-1", "qk-t-1-dev", "qk-t-1-pg", "qk-t-1-net")
    captured: list[list[str]] = []

    def fake_run(cmd, check=True, capture=True):
        captured.append(cmd)

        class _Result:
            stdout = "cid\n"
            returncode = 0

        return _Result()

    monkeypatch.setattr("quikode.docker_env._run", fake_run)

    docker_env.start_dev_container(handle, cfg, tmp_path / "worktree", credential_bundle=bundle)

    cmd = captured[0]
    joined = "\n".join(cmd)
    assert f"src={tmp_path / 'claude'},dst=/host-auth/claude,ro=true" in joined
    assert f"src={tmp_path / 'codex'},dst=/host-auth/codex,ro=true" in joined
    assert f"src={tmp_path / 'opencode-data'},dst=/host-auth/opencode-data,ro=true" in joined
    assert f"src={tmp_path / 'opencode-config'},dst=/host-auth/opencode-config,ro=true" in joined
    assert f"src={claude_json},dst=/host-auth/claude.json,ro=true" in joined
    assert "GITHUB_TOKEN=secret-token" in cmd
    assert "GH_TOKEN=secret-token" in cmd
