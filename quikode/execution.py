"""Execution backend contracts and adapters.

This module is the orchestration-facing runtime boundary. Worker code should
depend on these contracts rather than Docker primitives directly; Docker stays
the only production backend for now.

Future backend shapes covered by the contract:
- ``ssh-docker``: one remote host runs multiple Docker task sandboxes.
- ``vm-sandbox``: each VM is itself one task sandbox, with no inner Docker
  unit required by worker logic.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from . import docker_env
from .config import Config

BackendName = Literal["docker", "fake"]
FutureBackendName = Literal["ssh-docker", "vm-sandbox"]


@dataclass(frozen=True)
class ExecutionHost:
    """Optional placement target for a sandbox.

    ``kind`` is intentionally descriptive rather than operational in Phase 2:
    local Docker uses ``local``; future remote implementations can distinguish
    a shared remote Docker host from a VM that is itself the task sandbox.
    """

    kind: Literal["local", "remote-vm-host", "vm-sandbox"] = "local"
    id: str = "local"
    address: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CredentialSource:
    """One credential material source and its intended sandbox destination."""

    name: str
    source_type: Literal["path", "env"]
    source: Path | str
    install_path: str
    read_only: bool = True
    required: bool = False


@dataclass(frozen=True)
class CredentialBundle:
    """Declarative credential package for execution backends.

    Docker consumes path sources as read-only bind mounts. Remote backends will
    consume the same bundle as upload/install work without changing worker code.
    """

    sources: tuple[CredentialSource, ...] = ()

    def path_sources(self) -> tuple[CredentialSource, ...]:
        return tuple(s for s in self.sources if s.source_type == "path")

    def env_sources(self) -> tuple[CredentialSource, ...]:
        return tuple(s for s in self.sources if s.source_type == "env")

    def github_env_name(self) -> str:
        for source in self.env_sources():
            if source.name == "github_token":
                return str(source.source)
        return "GITHUB_TOKEN"


@dataclass
class ExecutionSandbox:
    """Per-task execution identity used by workers.

    ``native_handle`` lets the Docker adapter preserve existing fields such as
    ``container_name`` for compatibility with helper code and tests.
    """

    task_id: str
    backend: BackendName | FutureBackendName
    unit_id: str
    worktree_path: Path
    host: ExecutionHost = field(default_factory=ExecutionHost)
    native_handle: Any | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        if self.native_handle is not None:
            return getattr(self.native_handle, name)
        raise AttributeError(name)


@dataclass(frozen=True)
class ExecutionUnit:
    id: str
    name: str
    status: str
    backend: str
    host_id: str = "local"


class ExecutionBackend(Protocol):
    name: str

    def provision(
        self,
        task_id: str,
        worktree_path: Path,
        *,
        host: ExecutionHost | None = None,
    ) -> ExecutionSandbox: ...

    def ensure_running(self, sandbox: ExecutionSandbox, worktree_path: Path) -> bool: ...

    def exec(
        self,
        sandbox: ExecutionSandbox,
        cmd: list[str],
        log_path: Path | None = None,
        stdin: str | None = None,
        timeout: int | None = None,
    ) -> tuple[int, str, str]: ...

    def teardown(self, sandbox: ExecutionSandbox) -> None: ...

    def cleanup(self) -> int: ...

    def list_units(self) -> list[ExecutionUnit]: ...

    def sample_resources(self, unit_id: str) -> dict | None: ...

    def host_resources(self) -> dict: ...


def build_credential_bundle(cfg: Config) -> CredentialBundle:
    sources: list[CredentialSource] = [
        CredentialSource("claude_auth", "path", cfg.claude_auth_dir, "/host-auth/claude"),
        CredentialSource("codex_auth", "path", cfg.codex_auth_dir, "/host-auth/codex"),
        CredentialSource(
            "opencode_data",
            "path",
            cfg.opencode_auth_dir,
            "/host-auth/opencode-data",
        ),
        CredentialSource(
            "opencode_config",
            "path",
            cfg.opencode_config_dir,
            "/host-auth/opencode-config",
        ),
        CredentialSource("github_token", "env", cfg.github_token_env, "GITHUB_TOKEN"),
    ]
    if cfg.claude_json_path.exists():
        sources.append(
            CredentialSource("claude_json", "path", cfg.claude_json_path, "/host-auth/claude.json")
        )
    return CredentialBundle(tuple(sources))


class DockerExecutionBackend:
    name: str = "docker"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.credentials = build_credential_bundle(cfg)

    def provision(
        self,
        task_id: str,
        worktree_path: Path,
        *,
        host: ExecutionHost | None = None,
    ) -> ExecutionSandbox:
        handle = docker_env.make_handle(task_id)
        ws_label = docker_env.workspace_label(self.cfg)
        docker_env.network_create(handle.network_name, label=ws_label)
        if self.cfg.postgres_enabled:
            docker_env.start_postgres(handle, self.cfg, label=ws_label)
            docker_env.wait_postgres_healthy(handle)
        cid = docker_env.start_dev_container(
            handle,
            self.cfg,
            worktree_path,
            credential_bundle=self.credentials,
        )
        docker_env.wait_dev_ready(handle, timeout_s=240)
        return ExecutionSandbox(
            task_id=task_id,
            backend=cast(BackendName, self.name),
            unit_id=handle.container_name,
            worktree_path=worktree_path,
            host=host or ExecutionHost(),
            native_handle=handle,
            metadata={"container_id": cid, "_backend": self},
        )

    def ensure_running(self, sandbox: ExecutionSandbox, worktree_path: Path) -> bool:
        assert sandbox.native_handle is not None
        return docker_env.ensure_dev_container_running(
            sandbox.native_handle,
            self.cfg,
            worktree_path,
            credential_bundle=self.credentials,
        )

    def exec(
        self,
        sandbox: ExecutionSandbox,
        cmd: list[str],
        log_path: Path | None = None,
        stdin: str | None = None,
        timeout: int | None = None,
    ) -> tuple[int, str, str]:
        assert sandbox.native_handle is not None
        return docker_env.exec_in(sandbox.native_handle, cmd, log_path=log_path, stdin=stdin, timeout=timeout)

    def teardown(self, sandbox: ExecutionSandbox) -> None:
        assert sandbox.native_handle is not None
        docker_env.teardown(sandbox.native_handle)

    def cleanup(self) -> int:
        return docker_env.cleanup_all_quikode(self.cfg)

    def list_units(self) -> list[ExecutionUnit]:
        return [
            ExecutionUnit(
                id=str(c.get("id", "")),
                name=str(c.get("name", "")),
                status=str(c.get("status", "")),
                backend=self.name,
            )
            for c in docker_env.list_quikode_containers(label=docker_env.workspace_label(self.cfg))
        ]

    def sample_resources(self, unit_id: str) -> dict | None:
        return docker_env.sample_container_stats(unit_id)

    def host_resources(self) -> dict:
        return docker_env.host_resources()


@dataclass
class FakeCall:
    name: str
    task_id: str | None = None
    cmd: list[str] | None = None
    postgres_requested: bool | None = None


class FakeExecutionBackend:
    """In-memory execution backend for contract tests."""

    name: str = "fake"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.calls: list[FakeCall] = []
        self.sandboxes: dict[str, ExecutionSandbox] = {}
        self.dead_sandboxes: set[str] = set()
        self.exec_responses: list[tuple[int, str, str] | BaseException] = []
        self.resource_stats: dict[str, dict] = {}
        self.default_exec_response: tuple[int, str, str] = (0, "", "")

    def provision(
        self,
        task_id: str,
        worktree_path: Path,
        *,
        host: ExecutionHost | None = None,
    ) -> ExecutionSandbox:
        unit_id = f"fake-{docker_env.slugify(task_id)}-{len(self.sandboxes) + 1}"
        sandbox = ExecutionSandbox(
            task_id=task_id,
            backend=cast(BackendName, self.name),
            unit_id=unit_id,
            worktree_path=worktree_path,
            host=host or ExecutionHost(),
            native_handle=None,
            metadata={"_backend": self},
        )
        self.calls.append(
            FakeCall(
                name="provision",
                task_id=task_id,
                postgres_requested=bool(self.cfg.postgres_enabled),
            )
        )
        self.sandboxes[unit_id] = sandbox
        self.dead_sandboxes.discard(unit_id)
        return sandbox

    def ensure_running(self, sandbox: ExecutionSandbox, worktree_path: Path) -> bool:
        self.calls.append(FakeCall(name="ensure_running", task_id=sandbox.task_id))
        if sandbox.unit_id not in self.dead_sandboxes:
            return False
        self.dead_sandboxes.remove(sandbox.unit_id)
        replacement = sandbox.unit_id + "-recreated"
        self.sandboxes.pop(sandbox.unit_id, None)
        sandbox.unit_id = replacement
        sandbox.worktree_path = worktree_path
        self.sandboxes[replacement] = sandbox
        return True

    def exec(
        self,
        sandbox: ExecutionSandbox,
        cmd: list[str],
        log_path: Path | None = None,
        stdin: str | None = None,
        timeout: int | None = None,
    ) -> tuple[int, str, str]:
        self.calls.append(FakeCall(name="exec", task_id=sandbox.task_id, cmd=list(cmd)))
        if self.exec_responses:
            response = self.exec_responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response
        return self.default_exec_response

    def teardown(self, sandbox: ExecutionSandbox) -> None:
        self.calls.append(FakeCall(name="teardown", task_id=sandbox.task_id))
        self.sandboxes.pop(sandbox.unit_id, None)

    def cleanup(self) -> int:
        n = len(self.sandboxes)
        self.calls.append(FakeCall(name="cleanup"))
        self.sandboxes.clear()
        return n

    def list_units(self) -> list[ExecutionUnit]:
        self.calls.append(FakeCall(name="list_units"))
        return [
            ExecutionUnit(id=s.unit_id, name=s.unit_id, status="running", backend=self.name)
            for s in self.sandboxes.values()
        ]

    def sample_resources(self, unit_id: str) -> dict | None:
        self.calls.append(FakeCall(name="sample_resources"))
        return self.resource_stats.get(unit_id)

    def host_resources(self) -> dict:
        self.calls.append(FakeCall(name="host_resources"))
        return {"cpus": 8, "mem_bytes": 16 * 1024**3}

    def mark_dead(self, sandbox: ExecutionSandbox) -> None:
        self.dead_sandboxes.add(sandbox.unit_id)

    def simulate_transient_exec_failure(self) -> None:
        self.exec_responses.append((1, "", "Error response from daemon: container fake is not running"))

    def simulate_quota_exec_failure(self) -> None:
        self.exec_responses.append((1, "", "rate_limit_exceeded"))

    def simulate_timeout(self, timeout_s: int = 10) -> None:
        self.exec_responses.append(subprocess.TimeoutExpired(cmd=["fake"], timeout=timeout_s))


def build_execution_backend(cfg: Config) -> ExecutionBackend:
    backend = cfg.execution_backend
    if backend == "docker":
        return DockerExecutionBackend(cfg)
    if backend == "fake":
        return FakeExecutionBackend(cfg)
    raise ValueError(
        f"unsupported execution_backend {backend!r}; phase 2 supports 'docker' and 'fake' "
        "(future: 'ssh-docker', 'vm-sandbox')"
    )


def exec_in(
    sandbox: Any,
    cmd: list[str],
    log_path: Path | None = None,
    stdin: str | None = None,
    timeout: int | None = None,
) -> tuple[int, str, str]:
    """Compatibility exec helper.

    New code passes an ``ExecutionSandbox`` and routes to its backend. Older
    tests/helpers that pass a Docker ``TaskContainer`` still use Docker exec.
    """

    if isinstance(sandbox, ExecutionSandbox):
        backend = sandbox.metadata.get("_backend")
        if isinstance(backend, DockerExecutionBackend | FakeExecutionBackend):
            return backend.exec(sandbox, cmd, log_path=log_path, stdin=stdin, timeout=timeout)
        if sandbox.backend == "docker":
            return docker_env.exec_in(
                sandbox.native_handle,
                cmd,
                log_path=log_path,
                stdin=stdin,
                timeout=timeout,
            )
        raise RuntimeError(f"execution sandbox {sandbox.unit_id} has no backend instance")
    return docker_env.exec_in(sandbox, cmd, log_path=log_path, stdin=stdin, timeout=timeout)
