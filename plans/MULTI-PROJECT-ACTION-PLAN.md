# Multi-project action plan

## Target

Quikode should become a long-running multi-project control plane: one daemon can supervise many project DAGs, schedule work by a unified priority policy, share local and remote execution capacity, manage model subscription windows and failover chains, surface blocks and constraints through notifications, and expose the same state through a polished TUI and future API/Web UI.

## Sequencing

### 1. Establish the control plane

Start with `plans/35-multi-project-control-plane.md`.

Deliver a project registry and control store while keeping each existing project workspace intact. The first PR should not change task execution or scheduling. It should prove the system can load, validate, list, pause, and resume multiple registered quikode projects.

### 2. Convert local scheduling into typed candidates

Start `plans/36-unified-global-scheduler.md` inside one project before scheduling globally.

Every runnable unit becomes a candidate: task start, review fix, CI fix, rebase, merge-node refresh, retry, and resume. This removes hidden bypass paths and gives each candidate an explainable priority decision.

### 3. Run the global scheduler

Use the candidate API from step 2 across all registered projects.

The global scheduler becomes the only authority for dispatch. It accounts for project weight, fairness debt, phase urgency, DAG criticality, stale work, retry health, resources, and model capacity.

### 4. Add shared execution resources

Implement `plans/37-shared-resource-fabric.md`.

Replace per-project `max_parallel` thinking with global host capacity, live container inventory, reservations, placement constraints, and an execution-host model that can later support remote Docker and VM sandboxes.

### 5. Add model capacity, failover, and role pausing

Implement `plans/38-model-capacity-failover-and-pausing.md`.

Replace single model choices with per-role CLI/model chains. Track real provider window data where available, record request/token/cost telemetry, enforce reserve floors, fail down through the chain, recover upward when capacity returns, and pause only affected agent roles when no option is available.

### 6. Add notifications and API foundation

Implement `plans/40-notifications-api-and-web-control-plane.md`.

Unify notifications for review-ready, blocked, failed, model-paused, resource-exhausted, daemon, and DAG-sync events. Add an API service so remote monitoring and a later Web UI have a stable backend.

### 7. Add live DAG sync

Implement `plans/39-live-dag-sync-and-long-haul-reliability.md`.

Each project should refresh its DAG from the configured primary branch during long runs, version DAG revisions, and safely migrate new, changed, retired, or orphaned nodes.

### 8. Rebuild the TUI around multi-project operation

Implement `plans/41-multi-project-tui-and-dashboard-redesign.md`.

The first screen should show all projects, global queue, resource pressure, model pressure, active work, blocked work, notifications, and daemon health. Drilldown flows move from project to task to subtask detail.

### 9. Prune legacy modes

Implement `plans/42-intended-mode-and-configuration-pruning.md`.

Once the new path works, remove legacy choices that are no longer real options: non-aggressive stacking, review bypass slots, inert preemption, one-off model assignments, stale compatibility keys, and user-facing config that has a universal right answer.

## First implementation brief

Build the multi-project control-plane foundation.

Scope:

- Add `ProjectRef` and `TaskRef`.
- Add control-plane config loading.
- Add a control SQLite store with project registry and heartbeat tables.
- Add `ProjectRuntime` to load one existing project workspace.
- Add `qk control init`.
- Add `qk project add`, `qk project list`, `qk project pause`, and `qk project resume`.
- Add a dry-run control command that loads all enabled projects and prints aggregate state counts.
- Add tests using two temporary project roots with independent configs and stores.

Non-goals for the first PR:

- Do not change `qk run` scheduling.
- Do not change `TaskWorker`.
- Do not migrate per-project SQLite schemas to multi-tenant storage.
- Do not implement resource or model capacity enforcement yet.
- Do not redesign the TUI yet.

Success criteria:

- Existing single-project commands still work.
- A control root can register two projects.
- The control plane can load both projects and report their health.
- Project pause/resume affects only control-plane eligibility, not project task state.
- The code introduces `TaskRef` early enough that later scheduler work does not need another broad naming refactor.

