# Execution Backends

Phase 2 introduces `quikode.execution` as the boundary between worker logic and
the runtime that hosts a task sandbox. Docker remains the only production
backend.

## Supported Backends

- `docker`: default production backend. Provisions one Docker dev container per
  task and, when configured, one Postgres sidecar on the same Docker network.
- `fake`: test backend. Records lifecycle and exec calls without invoking
  Docker.

Future documented values are not implemented yet:

- `ssh-docker`: one remote host runs multiple Docker task sandboxes.
- `vm-sandbox`: each VM is itself a task sandbox.

## Contract

Workers interact with `ExecutionBackend`:

- `provision(task_id, worktree_path, host=None) -> ExecutionSandbox`
- `ensure_running(sandbox, worktree_path) -> bool`
- `exec(sandbox, cmd, log_path=None, stdin=None, timeout=None) -> (rc, stdout, stderr)`
- `teardown(sandbox)`
- `cleanup()`
- `list_units()`
- `sample_resources(unit_id)`
- `host_resources()`

`ExecutionSandbox` is the per-task identity. Worker code should treat it as an
opaque sandbox handle; Docker-specific container fields are compatibility only.

## Credential Bundle

`build_credential_bundle(cfg)` normalizes local credential configuration into a
declarative package:

- path sources for Claude, Codex, opencode, and optional `claude.json`
- an env source naming the GitHub token environment variable
- intended install paths inside the sandbox

The Docker backend consumes path sources as read-only bind mounts at the current
`/host-auth/*` locations and injects the resolved GitHub token as
`GITHUB_TOKEN` and `GH_TOKEN`. Remote backends should consume the same bundle as
upload/install actions, preserving worker behavior.

## Remote Backend Shapes

Mode A, shared remote Docker host:

- `ExecutionHost(kind="remote-vm-host")` identifies the host.
- `provision` creates a Docker sandbox unit on that host.
- `list_units` and `sample_resources` report per-container data.
- credentials are uploaded to the host, then mounted or copied into each
  sandbox.

Mode B, VM as sandbox:

- `ExecutionHost(kind="vm-sandbox")` identifies the VM.
- `provision` creates or claims the VM and installs the worktree plus
  credentials directly.
- `ensure_running` verifies VM reachability and task process readiness.
- `list_units` and `sample_resources` report per-VM data.

Both modes must preserve the worker-facing exec return shape and timeout,
stdin, and log-path behavior.
