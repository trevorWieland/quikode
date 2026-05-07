# Zaimu Profile

Use `--profile zaimu` for the Zaimu workspace.

Profile-owned defaults:

- base branch: `dev`
- image: `quikode-zaimu-dev:latest`
- local CI: `just ci`
- subtask check: `just check`
- pre-commit runner: `auto`
- database: per-task Postgres sidecar with DB `zaimu`
- resources: 3 CPU, 8 GB per task, 2 CPU and 8 GB reserved for host work,
  with `max_parallel_auto` enabled
- merge policy: squash merge with branch deletion

## Validation

Useful commands inside the task container:

```bash
just check
just ci
```

On smaller laptops, start with:

```bash
quikode run --max-parallel 1
```

or leave `[resources].max_parallel_auto = true` enabled and let quikode compute
a safe local cap.
