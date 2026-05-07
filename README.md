# Quikode

Quikode is an event-driven task DAG runner for AI-assisted coding workspaces. It creates a fresh local state database, schedules ready DAG nodes, drives worker phases through a canonical FSM, and watches PR/CI/review signals until tasks are merged or explicitly stopped.

## Install

```bash
uv tool install --reinstall .
```

## Fresh Workspace

```bash
quikode init --repo /path/to/repo --dag /path/to/dag.json --profile tanren
quikode doctor
quikode run --max-parallel 4
```

For the Tanren profile, `init` seeds already-landed DAG nodes from deterministic evidence on `origin/main`. Test fixtures can pass `--no-seed-from-main`.

## Seed From Main

```bash
quikode seed-from-main
quikode seed-from-main --merged-nodes-file merged.json
```

Accepted evidence is exact and deterministic: DAG `merged_in_main: true`, DAG `status: "merged"`, a commit subject on `origin/main` matching `<node_id>:`, or an explicit JSON evidence file. Nodes without evidence remain unstarted and are scheduled only after their dependencies are complete.

## Validation

```bash
uv run ruff check quikode tests
uv run ruff format --check quikode tests
uv run ty check quikode tests
uv run pytest tests/ -q
```

## Canonical States

`pending`, `provisioning`, `planning`, `doing_subtask`, `checking_subtask`, `triaging_subtask`, `committing`, `pushing`, `local_ci_checking`, `pre_pr_auditing`, `fixup_planning`, `pr_opening`, `pending_ci`, `awaiting_review`, `merge_ready`, `triaging_feedback`, `addressing_feedback`, `rebasing_to_main`, `conflict_resolving`, `merged`, `blocked`, `failed`, `aborted`.

## Commands

Lifecycle: `run`, `plan`, `retry`, `resume`, `reset-retries`, `abort`.
Inspection: `status`, `watch`, `briefing`, `show`, `subtasks`, `explain`, `tail`, `logs`.
Workspace: `init`, `doctor`, `seed-from-main`, `reset`, `prune`, `disk-usage`.
Daemon: `daemon start`, `daemon stop`, `daemon status`.
TUI: `tui`.
