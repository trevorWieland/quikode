# Plan 41 - multi-project TUI and dashboard redesign

## Goal

Build a clean, fast operator interface for global progress, project progress, and task/subtask drilldown.

The TUI should answer at a glance:

- Which projects are moving?
- Which projects are blocked or paused?
- What is running right now?
- What is constrained by CPU/memory/container capacity?
- What is constrained by model subscription windows?
- Which tasks are looping, failing, or retrying?
- Where should the operator intervene?

## Current state

- TUI polls one project store.
- Header shows one workspace, max parallel, resources, counts, and heartbeat.
- Main table shows non-terminal tasks for one project.
- Detail panel shows selected task subtasks and agent calls.
- DAG view is one project at a time.
- Settings modal exposes a small subset of single-project config.

## Design

Three-level navigation:

1. Global dashboard
   - project table
   - global queue
   - live containers/resources
   - model capacity by role/account
   - notifications/blocks feed
   - daemon/API status

2. Project dashboard
   - project progress, DAG revision, branch sync age
   - active/awaiting/blocked tasks
   - project DAG map
   - project resource/model usage
   - project-specific settings summary

3. Task/subtask dashboard
   - plan
   - subtasks
   - current agent call
   - retry reasons and failure signatures
   - progress checks
   - self-audit and witness results
   - logs and worktree/container metadata

## UX principles

- Default screen is operational, not decorative.
- Dense, stable tables for repeated monitoring.
- No nested card layouts.
- Status colors must mean the same thing everywhere.
- Every paused/blocked state has a reason and a next action.
- Every scheduler decision has an explanation drilldown.

## Data model

Replace single `PollSnapshot` with:

- `GlobalSnapshot`
- `ProjectSnapshot`
- `TaskSnapshot`
- `QueueSnapshot`
- `ResourceSnapshot`
- `ModelCapacitySnapshot`
- `NotificationSnapshot`

Snapshots can read directly from control SQLite/Postgres or through the API. Prefer API once plan 40 exists; allow direct store polling for local dev.

## Implementation

1. Add global TUI shell with left project rail, center content, right constraints/notifications panel.
2. Reuse existing task table/detail widgets after adding `project_id`.
3. Add model capacity panel.
4. Add global queue panel with scheduler decision reasons.
5. Add project drilldown route.
6. Add task drilldown route.
7. Move settings from "edit random config keys" to policy-aware panels:
   - project enable/weight
   - role model chains
   - notification sinks
   - resource limits
8. Keep slash commands but make them `project/task` aware.

## Acceptance

- From the first screen, user can see all projects, active tasks, blocked tasks, resource pressure, model pressure, and recent notifications.
- Selecting a project shows DAG progress and project tasks.
- Selecting a task shows retries, plan, agent calls, failures, and logs.
- A queued candidate can be inspected to see why it is waiting.
- Existing single-project TUI tests are either migrated or deleted; no duplicate UI path remains after cutover.

