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

Profiles provide project defaults for the base branch, dev image, local validation
commands, resource sizing, and local Postgres settings. Built-ins include
`tanren`, `zaimu`, `rust-just`, `generic-rust`, and `generic-python`.

For Zaimu:

```bash
quikode init --repo ../zaimu --dag ../zaimu/docs/roadmap/dag.json --profile zaimu
quikode doctor
quikode build-image --flavor rust
quikode run --max-parallel 2
```

`init` seeds already-landed DAG nodes from deterministic evidence on the
configured base branch (`origin/main` for Tanren, `origin/dev` for Zaimu). Test
fixtures can pass `--no-seed-from-base`; `--no-seed-from-main` remains as a
compatibility alias.

## Seed From Base

```bash
quikode seed-from-base
quikode seed-from-base --merged-nodes-file merged.json
quikode seed-from-main
```

Accepted evidence is exact and deterministic: DAG `merged_in_main: true`, DAG
`status: "merged"`, a commit subject on the configured base branch matching
`<node_id>:`, or an explicit JSON evidence file. Nodes without evidence remain
unstarted and are scheduled only after their dependencies are complete.

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
Inspection: `status`, `watch`, `briefing`, `show`, `subtasks`, `explain`, `tail`, `logs`, `monitor`.
Workspace: `init`, `doctor`, `seed-from-base`, `seed-from-main`, `reset`, `prune`, `disk-usage`.
Daemon: `daemon start`, `daemon stop`, `daemon status`.
TUI: `tui`.
