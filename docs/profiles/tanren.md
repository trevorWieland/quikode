# Tanren Profile

Use `--profile tanren` for the Tanren workspace.

Profile-owned defaults:

- image: `quikode-tanren-dev:latest`
- base branch: `main`
- local CI: `just ci`
- subtask check: `just check`
- pre-commit runner: `auto`
- database: per-task Postgres sidecar with DB `tanren`
- resources: 4 CPU, 12 GB per task, 4 CPU and 16 GB reserved for host work
- merge policy: squash merge with branch deletion

## BDD Conventions

Tanren BDD feature files live under `tests/bdd/features`. Behavior-proof tag
validation is part of `just check` and `just ci`.

When a task touches behavior evidence, the plan should include BDD subtasks
late in the subtask order, after the implementation surfaces they witness
exist.

## Validation

Useful commands inside the task container:

```bash
just check
just ci
```

Targeted BDD diagnosis:

```bash
just check-bdd-tags
```

## Archived Branches

Old failed Tanren work can be inspected through:

```bash
quikode archive show <id>
quikode archive branch <id>
```

Archived branches are references only. Fresh strict reruns are the default.
