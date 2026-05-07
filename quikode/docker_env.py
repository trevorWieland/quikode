"""Per-task container lifecycle.

Each task gets its own container running the dev image, with:
  - the worktree bind-mounted at /workspace
  - agent CLI auth dirs read-only mounted
  - GITHUB_TOKEN injected
  - a Postgres sidecar in the same docker network (compose project name unique per task)
  - a `qk_workspace=<hash>` label so multiple quikode workspaces can run side-by-side
    without their resets clobbering each other's containers
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config


def workspace_label(cfg: Config) -> str:
    """Stable, human-readable label identifying a quikode workspace.

    Format: `qk_workspace=<8-hex>` derived from the absolute state_dir path.
    Used to scope container cleanup to a single workspace, so two parallel
    quikode runs in different workspaces don't tear each other down."""
    digest = hashlib.sha1(str(cfg.state_dir.resolve()).encode()).hexdigest()[:8]
    return f"qk_workspace={digest}"


@dataclass
class TaskContainer:
    task_id: str
    project_name: str  # docker compose project name
    workspace_id: str  # short slug appended to container names
    container_name: str  # the dev container holding the worktree + agents
    pg_container_name: str
    network_name: str


def slugify(task_id: str) -> str:
    s = task_id.lower().replace(":", "-").replace("/", "-")
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in s)


def make_handle(task_id: str) -> TaskContainer:
    workspace_id = f"{slugify(task_id)}-{secrets.token_hex(3)}"
    project = f"qk-{workspace_id}"
    return TaskContainer(
        task_id=task_id,
        project_name=project,
        workspace_id=workspace_id,
        container_name=f"{project}-dev",
        pg_container_name=f"{project}-pg",
        network_name=f"{project}-net",
    )


def _run(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def gh_token() -> str:
    """Resolve GITHUB_TOKEN from env or `gh auth token`."""
    if t := os.environ.get("GITHUB_TOKEN"):
        return t
    try:
        r = _run(["gh", "auth", "token"])
        return r.stdout.strip()
    except subprocess.CalledProcessError:
        return ""


def network_create(name: str, label: str | None = None) -> None:
    r = _run(["docker", "network", "inspect", name], check=False)
    if r.returncode != 0:
        cmd = ["docker", "network", "create"]
        if label:
            cmd += ["--label", label]
        cmd.append(name)
        _run(cmd)


def network_remove(name: str) -> None:
    _run(["docker", "network", "rm", name], check=False)


def start_postgres(handle: TaskContainer, label: str | None = None) -> None:
    network_create(handle.network_name)
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        handle.pg_container_name,
        "--network",
        handle.network_name,
        "--network-alias",
        "postgres",
        "-e",
        "POSTGRES_PASSWORD=dev",
        "-e",
        "POSTGRES_USER=postgres",
        "-e",
        "POSTGRES_DB=tanren",
        "--health-cmd",
        "pg_isready -U postgres",
        "--health-interval",
        "2s",
        "--health-timeout",
        "2s",
        "--health-retries",
        "20",
    ]
    if label:
        cmd += ["--label", label]
    cmd.append("postgres:16-alpine")
    _run(cmd)


def wait_postgres_healthy(handle: TaskContainer, timeout_s: int = 60) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = _run(
            ["docker", "inspect", "-f", "{{.State.Health.Status}}", handle.pg_container_name],
            check=False,
        )
        if r.returncode == 0 and r.stdout.strip() == "healthy":
            return
        time.sleep(1)
    raise TimeoutError(f"postgres did not become healthy within {timeout_s}s")


def start_dev_container(handle: TaskContainer, cfg: Config, worktree_path: Path) -> str:
    """Start the dev container detached. Returns container ID."""
    token = gh_token()
    # Ensure the sccache dir exists on the host
    cfg.sccache_dir.mkdir(parents=True, exist_ok=True)
    # Mount host auth dirs at /host-auth/* read-only; the entrypoint copies them
    # into writable container locations so each agent CLI can mutate its own
    # session/history state without contention.
    #
    # Worktree note: a git-worktree's .git file holds an absolute *host* path
    # back to the parent repo (e.g. /home/foo/repo/.git/worktrees/t-001). For
    # git inside the container to resolve that, we bind-mount the parent repo's
    # .git directory at the same absolute path. We mount only `.git` (not the
    # whole repo) to keep the surface area small and avoid mounting unrelated
    # working-tree state.
    repo_git_dir = cfg.repo_path / ".git"
    mounts = [
        ("type=bind", f"src={worktree_path}", "dst=/workspace"),
        ("type=bind", f"src={repo_git_dir}", f"dst={repo_git_dir}"),
        ("type=bind", f"src={cfg.claude_auth_dir}", "dst=/host-auth/claude", "ro=true"),
        ("type=bind", f"src={cfg.codex_auth_dir}", "dst=/host-auth/codex", "ro=true"),
        ("type=bind", f"src={cfg.opencode_auth_dir}", "dst=/host-auth/opencode-data", "ro=true"),
        ("type=bind", f"src={cfg.opencode_config_dir}", "dst=/host-auth/opencode-config", "ro=true"),
        # Shared sccache across all tasks — sccache handles concurrent access safely.
        ("type=bind", f"src={cfg.sccache_dir}", "dst=/sccache"),
    ]
    # claude.json is a file (not a dir); mount it only if it exists on host
    if cfg.claude_json_path.exists():
        mounts.append(("type=bind", f"src={cfg.claude_json_path}", "dst=/host-auth/claude.json", "ro=true"))
    mount_args: list[str] = []
    for m in mounts:
        mount_args += ["--mount", ",".join(m)]

    env = {
        "GITHUB_TOKEN": token,
        "GH_TOKEN": token,
        "HOME": "/home/dev",
        "DATABASE_URL": "postgres://postgres:dev@postgres:5432/tanren",
        "QK_TASK_ID": handle.task_id,
        # Identity for any commits the agent makes inside the container
        "QK_GIT_EMAIL": os.environ.get("QK_GIT_EMAIL", "wielandtrevor@gmail.com"),
        "QK_GIT_NAME": os.environ.get("QK_GIT_NAME", "trevorWieland"),
        # Lefthook: skip activation/run inside container; we run hooks explicitly via just ci.
        "LEFTHOOK": "0",
        # Cargo target dir lives outside the worktree to avoid polluting it
        "CARGO_TARGET_DIR": "/home/dev/cargo-target",
    }
    env_args: list[str] = []
    for k, v in env.items():
        if v:
            env_args += ["-e", f"{k}={v}"]

    uid = os.getuid()
    gid = os.getgid()

    # Resource caps. --memory-swap == --memory disables swap so OOM kills
    # cleanly instead of paging the whole host into oblivion.
    resource_args: list[str] = []
    if cfg.cpu_per_task > 0:
        resource_args += ["--cpus", str(cfg.cpu_per_task)]
    if cfg.mem_per_task_gb > 0:
        resource_args += [
            "--memory",
            f"{cfg.mem_per_task_gb}g",
            "--memory-swap",
            f"{cfg.mem_per_task_gb}g",
        ]

    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        handle.container_name,
        "--network",
        handle.network_name,
        "--label",
        workspace_label(cfg),
        "--user",
        f"{uid}:{gid}",
        "-w",
        "/workspace",
        *resource_args,
        *mount_args,
        *env_args,
        cfg.image_tag,
        "sleep",
        "infinity",
    ]
    r = _run(cmd)
    return r.stdout.strip()


def start_warm_cache_container(cfg: Config, *, label_suffix: str = "warm") -> str:
    """Start a transient container for sccache pre-warming.

    Stripped-down vs. `start_dev_container`: no postgres, no agent CLI
    auth mounts, no entrypoint sentinel — we just want a workspace +
    sccache mount so cargo can run inside. Container name is
    `qk-warm-<6hex>` for visibility in `docker ps`. Returns the
    container name (the caller passes it to `docker exec` + `docker rm`).

    The container starts with `sleep infinity` so the caller controls
    lifetime; teardown is the caller's responsibility (typically a
    `try/finally` in `quikode warm-cache`).
    """
    cfg.sccache_dir.mkdir(parents=True, exist_ok=True)
    container_name = f"qk-warm-{secrets.token_hex(3)}-{label_suffix}"

    # Bind the repo at /workspace RW so `git fetch` + `git checkout` can
    # update the worktree. We deliberately don't mount the parent .git
    # separately — we're working with the repo's full checkout, not a
    # task worktree.
    mounts = [
        ("type=bind", f"src={cfg.repo_path}", "dst=/workspace"),
        ("type=bind", f"src={cfg.sccache_dir}", "dst=/sccache"),
    ]
    mount_args: list[str] = []
    for m in mounts:
        mount_args += ["--mount", ",".join(m)]

    env = {
        "HOME": "/home/dev",
        "CARGO_TARGET_DIR": "/home/dev/cargo-target",
        # Keep the sccache env identical to task containers so the cache
        # entries are interchangeable.
        "RUSTC_WRAPPER": "sccache",
        "SCCACHE_DIR": "/sccache",
    }
    env_args: list[str] = []
    for k, v in env.items():
        env_args += ["-e", f"{k}={v}"]

    uid = os.getuid()
    gid = os.getgid()

    resource_args: list[str] = []
    if cfg.cpu_per_task > 0:
        resource_args += ["--cpus", str(cfg.cpu_per_task)]
    if cfg.mem_per_task_gb > 0:
        resource_args += [
            "--memory",
            f"{cfg.mem_per_task_gb}g",
            "--memory-swap",
            f"{cfg.mem_per_task_gb}g",
        ]

    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "--label",
        workspace_label(cfg),
        "--label",
        "qk_role=warm-cache",
        "--user",
        f"{uid}:{gid}",
        "-w",
        "/workspace",
        *resource_args,
        *mount_args,
        *env_args,
        cfg.image_tag,
        "sleep",
        "infinity",
    ]
    _run(cmd)
    return container_name


def teardown_warm_cache_container(container_name: str) -> None:
    """Stop + remove a warm-cache container. Idempotent — silently
    succeeds if the container is already gone."""
    _run(["docker", "rm", "-f", container_name], check=False)


def wait_dev_ready(handle: TaskContainer, timeout_s: int = 120) -> None:
    """Wait for the dev container's entrypoint to finish copying auth files.

    The entrypoint touches /tmp/qk-ready as its last step before exec'ing the CMD.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = subprocess.run(
            ["docker", "exec", handle.container_name, "test", "-f", "/tmp/qk-ready"],
            capture_output=True,
        )
        if r.returncode == 0:
            return
        time.sleep(0.5)
    raise TimeoutError(f"dev container {handle.container_name} not ready within {timeout_s}s")


def is_dev_container_running(handle: TaskContainer) -> bool:
    """True iff `handle.container_name` exists AND its `.State.Running` is true.

    Uses `docker inspect` once. Cheap (~50ms). Returns False on missing
    container, exited container, or any inspect error — caller should treat
    False as "needs recreation".
    """
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", handle.container_name],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0 and r.stdout.strip() == "true"


def ensure_dev_container_running(
    handle: TaskContainer, cfg: Config, worktree_path: Path, label: str | None = None
) -> bool:
    """Idempotent: if the dev container is missing or not running, recreate
    it (postgres + network + dev container + wait_dev_ready). Returns True
    iff a recreation actually happened.

    Recovery from the 2026-05-07 container-vanished cascade: when a worker's
    dev container is OOM-killed mid-task or destroyed by an out-of-band
    cleanup, the next subtask attempt's pre-flight calls this helper, which
    re-provisions before the agent runs. Without this, every retry re-issues
    `docker exec` against the corpse container, returns rc=1 / 119-byte
    "No such container" stderr, and burns the 50-attempt hard ceiling in
    ~60 seconds.

    The cheap path: `is_dev_container_running` returns True immediately
    when the container is healthy (one inspect call, ~50ms). Steady-state
    cost is negligible. Caller (worker) is responsible for capping
    consecutive recreations to detect a permanently broken provisioning
    path; see `quikode/workers/subtasks.py`.
    """
    if is_dev_container_running(handle):
        return False
    ws_label = label or workspace_label(cfg)
    # Tear down any partial state so the recreate path is clean. _run with
    # check=False makes the calls idempotent — missing entities are silent.
    _run(["docker", "rm", "-f", handle.container_name], check=False)
    _run(["docker", "rm", "-f", handle.pg_container_name], check=False)
    network_remove(handle.network_name)
    network_create(handle.network_name, label=ws_label)
    start_postgres(handle, label=ws_label)
    wait_postgres_healthy(handle)
    start_dev_container(handle, cfg, worktree_path)
    wait_dev_ready(handle, timeout_s=240)
    return True


def exec_in(
    handle: Any,
    cmd: list[str],
    log_path: Path | None = None,
    stdin: str | None = None,
    timeout: int | None = None,
) -> tuple[int, str, str]:
    """Run a command inside the dev container. Stream to log if path given. Returns (rc, stdout, stderr)."""
    full = ["docker", "exec", "-i", handle.container_name, *cmd]
    proc = subprocess.run(
        full,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as f:
            f.write(f"\n$ {shlex.join(full)}\n")
            f.write(proc.stdout)
            if proc.stderr:
                f.write("\n[stderr]\n")
                f.write(proc.stderr)
    return proc.returncode, proc.stdout, proc.stderr


def teardown(handle: TaskContainer) -> None:
    """Stop + remove containers + network. Idempotent."""
    for name in (handle.container_name, handle.pg_container_name):
        _run(["docker", "rm", "-f", name], check=False)
    network_remove(handle.network_name)


def list_quikode_containers(label: str | None = None) -> list[dict]:
    """List qk-* containers. If `label` (e.g. 'qk_workspace=abc123') given,
    filter to only that workspace's containers."""
    cmd = ["docker", "ps", "-a", "--filter", "name=qk-", "--format", "{{.ID}}\t{{.Names}}\t{{.Status}}"]
    if label:
        cmd += ["--filter", f"label={label}"]
    r = _run(cmd, check=False)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            out.append({"id": parts[0], "name": parts[1], "status": parts[2]})
    return out


def host_resources() -> dict:
    """Best-effort: return total cpu count + total memory bytes available to docker.

    On WSL or any setup where docker is the constraining sandbox, `docker info`
    is the truth — host cpu/mem may exceed what docker can actually allocate."""
    info = {"cpus": None, "mem_bytes": None}
    try:
        r = _run(["docker", "info", "--format", "{{json .}}"], check=False)
        if r.returncode == 0:
            d = json.loads(r.stdout)
            info["cpus"] = int(d.get("NCPU", 0)) or None
            info["mem_bytes"] = int(d.get("MemTotal", 0)) or None
    except Exception:
        pass
    if not info["cpus"]:
        try:
            info["cpus"] = os.cpu_count()
        except Exception:
            pass
    return info


def sample_container_stats(container_name: str) -> dict | None:
    """Snapshot current cpu% and mem-bytes for a container. Returns None on failure."""
    r = _run(
        [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}",
            container_name,
        ],
        check=False,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        cpu_pct, mem_usage, mem_pct = r.stdout.strip().split("|")
        # mem_usage is like "1.234GiB / 12GiB"
        used_str = mem_usage.split("/")[0].strip()
        return {
            "cpu_pct": float(cpu_pct.rstrip("%")),
            "mem_bytes": _parse_mem_string(used_str),
            "mem_pct": float(mem_pct.rstrip("%")),
        }
    except Exception:
        return None


def _parse_mem_string(s: str) -> int:
    """Parse '1.234GiB' / '512MiB' / '5.5MB' to bytes."""
    s = s.strip()
    units = {
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
        "TiB": 1024**4,
        "kB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "B": 1,
    }
    for unit, mult in units.items():
        if s.endswith(unit):
            try:
                return int(float(s[: -len(unit)]) * mult)
            except ValueError:
                return 0
    return 0


def cleanup_all_quikode(cfg: Config | None = None) -> int:
    """Remove qk-* containers + networks belonging to this workspace.

    With `cfg` provided, filters by `qk_workspace=<hash>` label so we don't
    touch other workspaces' containers. Without `cfg`, falls back to global
    cleanup (used by tests / older callers)."""
    label = workspace_label(cfg) if cfg else None
    n = 0
    for c in list_quikode_containers(label=label):
        _run(["docker", "rm", "-f", c["name"]], check=False)
        n += 1
    if label:
        nets = _run(
            ["docker", "network", "ls", "--filter", f"label={label}", "--format", "{{.Name}}"], check=False
        )
    else:
        nets = _run(["docker", "network", "ls", "--filter", "name=qk-", "--format", "{{.Name}}"], check=False)
    for net in nets.stdout.splitlines():
        if net.strip():
            _run(["docker", "network", "rm", net.strip()], check=False)
    return n
