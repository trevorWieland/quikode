# Rust Just Profile

Use `--profile rust-just` for Tanren-style DAG projects that use a Rust stack,
`just check` for fast local validation, and `just ci` for the full PR gate.

Profile-owned defaults:

- base branch: `main`
- image: `quikode-rust-just-dev:latest`
- local CI: `just ci`
- subtask check: `just check`
- pre-commit runner: `auto`
- database: per-task Postgres sidecar with DB `app`
- resources: 3 CPU, 8 GB per task, 2 CPU and 8 GB reserved for host work,
  with `max_parallel_auto` enabled

Override `base_branch`, `postgres_db`, and `database_url` in
`.quikode/config.toml` when a project uses a different target branch or
database name.
