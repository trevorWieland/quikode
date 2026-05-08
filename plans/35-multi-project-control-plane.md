# Plan 35 - multi-project control plane and project registry

## Goal

Move quikode from "one current directory = one project" to a durable control plane that can supervise many projects without losing the existing per-project worker semantics.

The control plane owns project discovery, lifecycle, global state, and the API boundary. Individual project runtimes still own their DAG, repo, worktrees, prompts, and task FSM.

## Current state

- `load_config()` walks upward to one `.quikode/config.toml`.
- `Config` contains a single `repo_path`, `dag_path`, `state_dir`, `worktree_root`, `log_dir`, resource config, and agent role config.
- Store rows assume `tasks.id` is globally unique inside one SQLite DB.
- `qk run`, `qk daemon`, `qk tui`, `qk show`, and recovery commands all operate on the implicit current workspace.

This works well for one run, but it cannot answer: "which project should get the next planner slot?"

## Design

Add a top-level control-plane config:

```toml
[control]
state_dir = ".quikode-control"
notification_profile = "default"

[[projects]]
id = "tanren"
root = "/Users/trevor/runs/tanren"
enabled = true
weight = 100

[[projects]]
id = "zaimu"
root = "/Users/trevor/runs/zaimu"
enabled = true
weight = 60
```

Add new types:

- `ProjectRef(id, root)`
- `TaskRef(project_id, task_id)`
- `ProjectRuntime(project_ref, cfg, dag, store)`
- `ControlStore`, initially SQLite, with project registry, global scheduler events, capacity samples, model usage windows, notifications, and daemon/API metadata.

Keep per-project SQLite stores in place for PR-A. The control store indexes project-level facts and references per-project rows by `TaskRef`. Avoid a risky multi-tenant migration until the scheduler and UI APIs are stable.

## CLI shape

New commands:

- `qk control init`
- `qk project add <id> --root <path>`
- `qk project list`
- `qk project pause <id>`
- `qk project resume <id>`
- `qk control run`
- `qk control daemon start|stop|status`

Existing per-project commands remain but gain `--project <id>` when run from a control root. If no control config exists, current single-project behavior continues during this transition.

## Implementation

1. Add `quikode/control_config.py` with Pydantic models for control config and project entries.
2. Add `quikode/control_store.py` with tables:
   - `projects(id, root, enabled, weight, created_at, updated_at)`
   - `project_heartbeats(project_id, ts, status_json)`
   - `scheduler_events(id, project_id, task_id, phase, decision_json, ts)`
   - `notifications(id, project_id, task_id, kind, status, payload_json, ts)`
3. Add `quikode/project_runtime.py` that loads one project safely and validates:
   - project root exists
   - `.quikode/config.toml` exists
   - repo path and DAG path load
   - SQLite store opens or can be initialized
4. Add `TaskRef` and `ProjectRef` helper types in a small module.
5. Add control CLI commands without changing the current worker path.
6. Add tests that create two project roots with independent configs/stores and prove the control plane can load both.

## Acceptance

- A control config can register two existing quikode workspaces.
- `qk project list` shows per-project state, config validity, and last heartbeat.
- `qk control run --dry-run` can load all enabled projects and print aggregate task counts.
- No existing single-project command breaks when run inside an old workspace.

## Follow-up dependency

Plan 36 consumes `ProjectRuntime` and `TaskRef` to build global scheduling candidates.

