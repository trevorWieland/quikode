# Plan 39 - live DAG sync and long-haul reliability

## Goal

Let quikode run for days, weeks, or months while each project DAG evolves on its primary branch.

A DAG loaded at daemon start must not become permanent truth. The project runtime should periodically refresh the DAG from the configured primary branch, detect node additions/updates/removals, and migrate scheduler state safely.

## Current state

- `DAG.load(cfg.dag_path)` reads a local file.
- The orchestrator holds a `DAG` instance for the life of the process.
- State rows are seeded from the configured base branch at init/seed time.
- The TUI DAG view reloads the DAG by mtime, but the scheduler does not.

## Design

Each project gets a `DagSyncPolicy`:

```toml
[dag_sync]
enabled = true
source = "primary-branch"
remote = "origin"
branch = "main"
path = "docs/roadmap/dag.json"
poll_interval_s = 600
```

Sync loop:

1. Fetch primary branch.
2. Read DAG file from the remote/base ref without mutating in-flight worktrees.
3. Validate schema and topological correctness.
4. Diff against current DAG snapshot.
5. Apply safe migration.
6. Record `dag_revisions`.

## Migration rules

- New node: create pending row when dependencies permit.
- Changed title/scope/evidence/playbook for pending node: update current DAG snapshot.
- Changed node while active: do not rewrite the running prompt mid-agent-call. Mark `needs_dag_refresh_review`; worker picks up at safe checkpoint.
- Changed dependencies for not-started node: update eligibility.
- Removed node:
  - if pending and no work: mark `retired`.
  - if active/post-PR: keep row and mark `orphaned_from_dag`; require policy/manual action.
  - if merged: preserve historical row.
- Node ID rename is not inferred automatically; support explicit alias metadata later.

## Schema

Control or project store tables:

- `dag_revisions(project_id, revision_id, source_ref, sha, path, loaded_at, summary_json)`
- `dag_node_snapshots(project_id, revision_id, node_id, node_json)`
- task fields: `dag_revision_id`, `dag_status = current | changed | retired | orphaned_from_dag`

## Long-haul reliability requirements

This plan also adds run-duration hygiene:

- periodic DB integrity checks
- bounded state-log and artifact compaction/export policy
- heartbeat generation IDs so stale daemon generations cannot be confused
- graceful rolling restart of project runtimes
- idempotent recovery after host reboot
- control-plane audit log for every automated migration

## Acceptance

- A test changes the DAG file on primary branch, syncs, and proves a new node becomes schedulable without restarting the daemon.
- A test changes dependencies of a pending node and proves candidate eligibility changes.
- A test changes an active node and proves the task is not interrupted mid-call but is flagged for safe-checkpoint review.
- TUI/API show project DAG revision, sync age, migration summary, and any orphaned/retired nodes.

