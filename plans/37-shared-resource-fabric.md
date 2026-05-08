# Plan 37 - shared resource fabric for local Docker, remote Docker, and VM sandboxes

## Goal

Track and allocate compute resources across all projects and prepare the execution layer for remote Docker hosts and VM sandboxes.

The scheduler should know not only `max_parallel`, but actual CPU, memory, host placement, running containers, and failure domains.

## Current state

- `Config` has per-project `cpu_per_task`, `mem_per_task_gb`, `host_reserved_*`, and `max_parallel_auto`.
- `ExecutionBackend` already has `ExecutionHost`, `ExecutionSandbox`, `ExecutionUnit`, `list_units()`, `sample_resources()`, and `host_resources()`.
- Docker is the only production backend; remote shapes are documented but not implemented.
- Resource sampling is stored per project in `container_stats`.

## Design

Add a global resource manager:

```python
ResourcePool(
    hosts=[ExecutionHost],
    capacities={cpu, memory, disk, gpu?},
    reservations=[ResourceReservation],
    live_units=[ExecutionUnit]
)
```

Resource allocation becomes a two-step contract:

1. Scheduler asks `ResourceManager.can_place(candidate)`.
2. On dispatch, scheduler creates a short-lived reservation and passes selected `ExecutionHost` into worker provisioning.

This prepares the system for:

- local Docker only
- one or more remote Docker hosts over SSH
- VM-per-sandbox execution
- mixed pools, where different projects or roles have placement constraints

## Schema

Control store tables:

- `execution_hosts(id, kind, address, labels_json, enabled, capacity_json, observed_at)`
- `resource_samples(id, host_id, unit_id, project_id, task_id, cpu_pct, mem_bytes, disk_bytes, ts)`
- `resource_reservations(id, project_id, task_id, phase, host_id, cpu, mem_bytes, expires_at, status)`

## Implementation

1. Add `ResourceRequest(cpu, mem_gb, disk_gb, labels, backend)`.
2. Add `ResourceManager` with local Docker implementation first.
3. Move max-parallel auto computation from per-project CLI into resource manager.
4. Record all running containers across projects into the control store.
5. Add host labels and project placement constraints:
   - `requires = ["linux", "docker"]`
   - `avoid_hosts = []`
   - `preferred_hosts = []`
6. Extend `DockerExecutionBackend.provision(..., host=...)` so local host remains the default, but remote host is a real parameter.
7. Add no-op `RemoteDockerExecutionBackend` contract tests before real SSH implementation.

## Acceptance

- With two projects, global scheduling respects total CPU/memory caps instead of each project independently filling slots.
- A task reservation is released on worker completion, crash, or daemon restart.
- Orphaned containers from any project are visible in the global resource view.
- `qk control resources --json` reports host capacity, reservations, live units, and per-project usage.

## Notes

This plan should land before model-capacity enforcement becomes a scheduler gate. Resource and model availability are both capacity dimensions, and the scheduler should treat them similarly.

