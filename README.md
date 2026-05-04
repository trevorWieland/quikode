# quikode

Quick coder loop. Spin up parallel Docker dev environments per task, drive AI coding agents (claude-code, codex, opencode) through plan → do → check → commit → PR → review-loop → merge for each node in a task DAG.

## Status

**v0.3 (v3)** — full v3 feature set landed. Built for driving the [tanren](https://github.com/trevorWieland/tanren) DAG, but the core is generic.

- Loop validated on the FastAPI fixture (3 consecutive clean cycles, real PRs opened)
- Loop validated mechanically on tanren R-0001 (planner→doer→checker→triage with real verification)
- v2: subtask breakdown, per-task resource caps, smart conflict resolution, intent-gap detection, stacked diffs (off / within-milestone / aggressive)
- v3: per-subtask commits + push, progress-check agent, daemon supervisor with crash-restart, AWAITING_MERGE polling + GraphQL review threads, mid-flight parent-merge handling, `git rebase --onto` for stacked children, orphan-task recovery on daemon restart, opt-in auto-merge, DAG viewer in the TUI
- **581 pytest tests** (runs in <2s)

## Documentation

- **[`CLAUDE.md`](CLAUDE.md)** — read first if you're a future session bootstrapping cold
- [`docs/architecture.md`](docs/architecture.md) — components, FSM, branching model
- [`docs/runbook-operations.md`](docs/runbook-operations.md) — daily ops: starting runs, monitoring, reviewing PRs
- [`docs/runbook-incident-response.md`](docs/runbook-incident-response.md) — when things break: symptom → recovery
- [`docs/runbook-tanren-watch-points.md`](docs/runbook-tanren-watch-points.md) — tanren-specific gotchas
- [`docs/future-work.md`](docs/future-work.md) — open candidates with priority indicators
- [`docs/lessons-learned.md`](docs/lessons-learned.md) — empirical observations
- [`docs/design-v2.md`](docs/design-v2.md), [`docs/design-stacked-diffs-fix.md`](docs/design-stacked-diffs-fix.md), [`docs/design-per-subtask-commit.md`](docs/design-per-subtask-commit.md) — historical design refs (all IMPLEMENTED)
- [`docs/design-tui.md`](docs/design-tui.md), [`docs/design-tui-dag-viewer.md`](docs/design-tui-dag-viewer.md) — TUI design
- [`docs/archive/`](docs/archive/) — superseded planning + session snapshots

## How it works

For each ready task in the DAG:

1. `git worktree add` → fresh branch (`quikode/<task-id>-<6hex>`) off `main` (or off a parent's PR branch when stacking).
2. `docker run` the prebuilt dev image with the worktree + agent CLI auth dirs mounted.
3. **planner** agent (claude-opus) emits structured JSON: a list of subtasks with deps, files to touch, acceptance criteria.
4. For each subtask in topological order:
   - **doer** (opencode glm-5.1) implements the slice
   - **checker** (codex) walks the playbook (real CI, HTTP probes, CLI invocations)
   - **triage** (claude-opus) on FAIL → loop
   - **progress-check** (claude-haiku) intermittently judges whether the subtask is making progress, has flatlined, or it's too early to tell — BLOCKs the subtask on consecutive flatlines
   - on PASS: `git commit` (running pre-commit hooks per slice) + `git push`
5. **final checker** runs the whole-spec playbook after all subtasks pass.
6. `gh pr create` opens the PR; the daemon's review-watcher polls it every `review_poll_interval_s`.
7. CI failure / new review thread → triage → re-do (RESPONDING_TO_REVIEW reuses the existing worktree/branch/PR).
8. When AWAITING_MERGE + MERGEABLE + checks SUCCESS + threads resolved + age ≥ `auto_merge_min_age_s`: optional auto-merge (opt-in). Otherwise, ring a bell and wait.
9. On a parent's merge: stacked children are auto-rebased onto main with `git rebase --onto <parent_sha>` and their PRs are recreated against main.

State lives in SQLite (`.quikode/quikode.db`). The daemon supervisor restarts the orchestrator on crash with exponential backoff (`60s → 5m → 30m`). Orphan tasks (left in active states by an SIGTERM mid-step) are recovered on the next `quikode run` startup.

## Install

```bash
uv tool install --editable .
quikode --help
```

## Quick start

```bash
quikode init --repo ../tanren --dag ../tanren/docs/roadmap/dag.json
quikode doctor
quikode build-image --flavor tanren    # rust+node+pg+agent CLIs (or --flavor python)
quikode plan                           # preview ready tasks; nothing is launched
quikode daemon start --max-parallel 5  # supervised orchestrator (foreground)
quikode briefing                       # one-shot wake-up snapshot
quikode tui                            # live mission-control TUI; press `g` for DAG viewer
quikode show <id>                      # full state + artifacts for a task
quikode export <id> -o file.md         # bundle plan + verdict + diff for review
quikode daemon stop                    # SIGTERM, clean wind-down
```

## Commands

| Command | Purpose |
|---|---|
| `init` | Write `.quikode/config.toml` pointing at a repo + dag |
| `doctor` | Check docker, gh auth, agent CLIs, paths, image presence |
| `build-image --flavor [tanren\|python]` | Build the dev container |
| `plan [--only ID] [--milestone M] [--layers]` | Preview scope + ready tasks; non-launching |
| `run [--only ID] [--milestone M] [--max-parallel N] [--retry-failed]` | Start the orchestrator (foreground, no supervisor) |
| `daemon start [...]` | Same flags as `run`, but wrapped in a crash-restart supervisor |
| `daemon stop [--timeout-s 30]` | SIGTERM the supervisor; SIGKILL after timeout |
| `daemon status [--json]` | Daemon liveness + heartbeat freshness (exit 0 alive+fresh, 1 down, 2 stale) |
| `status` / `watch [--active]` | One-shot table / live table |
| `briefing` | One-shot wake-up snapshot: in-flight, awaiting, blocked, recent transitions, cost |
| `dag-stats [--by milestone\|layer]` | Per-group breakdown |
| `ready` | List unblocked tasks |
| `explain <id>` | Why is this task blocked? Who depends on it? |
| `show <id> [--full]` | Latest planner / checker / triage artifacts + state timeline + costs |
| `subtasks <id>` | Subtask state breakdown for a task |
| `export <id> -o file.md` | Bundle every artifact + full diff for human review |
| `tail <id>` / `logs <id>` | Tail task log / print its path |
| `retry <id>` | Reset BLOCKED/FAILED/ABORTED → PENDING (cleans worktree) |
| `resume <id>` | Re-pend a task with `resume_from_existing_subtasks=1` (reuses existing plan + done subtasks) |
| `unblock <id>` | Print intervention info for a BLOCKED task (no state change) |
| `mark-merged <id ...>` | Manually mark already-complete tasks as MERGED |
| `abort <id> [--reason ...]` | Per-task abort: marks ABORTED + tears down only `qk-<task-slug>-*` containers |
| `notify-test` | Send a test ntfy/Slack push using the workspace's `notify_settled_*` config |
| `demo <id>` | Materialize a task's PR branch in `<repo-parent>/<repo>-demo` for hands-on testing |
| `reset [--close-prs]` | Tear down everything + drop state |
| `prune [--sccache-max-gb N]` | Trim sccache + remove worktrees of terminal tasks |
| `disk-usage` | What quikode is using on disk |
| `clean-containers` | Remove stranded `qk-*` containers |
| `dev-test` | One-shot fixture validation |
| `tui` | Launch the mission-control TUI |

## Tests + lint

```bash
source .venv/bin/activate
python -m pytest tests/ -q       # 697 tests, <45s
ruff check quikode/ tests/       # strict; see pyproject [tool.ruff.lint]
ruff format quikode/ tests/      # consistent formatting
ty check quikode/                # alpha typechecker, advisory
```

## State machine (high-level)

```
PENDING → PROVISIONING → PLANNING ─[emits structured Plan JSON]─►
       │
       ▼
DOING_SUBTASK[i] ↔ CHECKING_SUBTASK[i] ↔ TRIAGING_SUBTASK[i]
       │ (subtask PASS) → per-subtask COMMITTING/PUSHING
       │ (all subtasks done)
       ▼
FINAL_CHECKING ↔ TRIAGING ↔ DOING (whole-spec fixup)
       │ (verdict PASS, ci pass)
       ▼
COMMITTING → PUSHING → PR_OPENING → POLLING_CI → AWAITING_MERGE
                                                       │
                          ┌────────────────────────────┼──────────────────┐
                  new review thread          parent merged           green + clean
                          │                          │                    │
                          ▼                          ▼                    ▼
                RESPONDING_TO_REVIEW         REBASING_TO_MAIN     auto-merge or human
                          │                          │                    │
                          └─────► AWAITING_MERGE ◄───┘                    ▼
                                                                       MERGED
```

Other transitions:
- `REBASING ↔ CONFLICT_RESOLVING` — when a worker's branch needs rebase before push.
- `INTENT_REVIEWING → REPLANNING` — Phase B drift handling.
- Any state → `BLOCKED` on retry budget exhaustion, `FAILED` on uncaught exception, `ABORTED` on user `quikode abort`.

Full FSM with all transitions + checkpoints is in [`docs/architecture.md`](docs/architecture.md).

## Build cache (rust)

The tanren-flavored image installs `sccache`. The orchestrator bind-mounts a single host-side cache dir (`.quikode/sccache`) into every task container. Cargo invocations across all parallel containers share the artifact cache; each task still gets its own ephemeral `target/` directory.

## Layout

```
quikode/
├── CLAUDE.md                    # bootstrap entry
├── README.md                    # this file
├── docs/
│   ├── architecture.md          # current component map + FSM
│   ├── runbook-operations.md
│   ├── runbook-incident-response.md
│   ├── runbook-tanren-watch-points.md
│   ├── future-work.md
│   ├── lessons-learned.md
│   ├── design-v2.md             # historical (IMPLEMENTED)
│   ├── design-stacked-diffs-fix.md
│   ├── design-per-subtask-commit.md
│   ├── design-tui.md
│   ├── design-tui-dag-viewer.md
│   └── archive/                 # superseded + session snapshots
├── pyproject.toml
├── prompts/                     # editable Jinja2 templates
│   ├── planner.md doer.md checker.md triage.md
│   ├── subtask-doer.md subtask-checker.md subtask-triage.md
│   ├── progress.md final-checker.md ...
├── docker/
│   ├── Dockerfile               # tanren flavor: rust + node + pg + agent CLIs
│   ├── Dockerfile.python        # python flavor for fixture
│   ├── entrypoint.sh
│   └── build.sh
├── quikode/                     # package source
│   ├── cli.py                   # Typer commands (init/doctor/run/daemon/show/tui/...)
│   ├── orchestrator.py          # threadpool + review-watcher + auto-rebase + auto-merge
│   ├── daemon.py                # crash-restart supervisor
│   ├── worker.py                # per-task FSM driver
│   ├── agents/                  # claude/codex/opencode wrappers + ccusage
│   ├── docker_env.py            # per-task container + postgres lifecycle
│   ├── github.py / github_graphql.py # gh CLI + GraphQL review threads
│   ├── prompts.py               # Jinja rendering
│   ├── state.py                 # SQLite store + State enum + orphan recovery
│   ├── config.py                # Pydantic Config (tui re-uses field metadata)
│   ├── dag.py                   # tanren dag.json loader
│   ├── worktree.py              # git worktree management
│   ├── subtask_schema.py        # Pydantic Plan / Subtask
│   ├── types.py                 # TypedDict rows + Verdict StrEnums
│   └── tui/                     # Textual mission-control TUI
└── tests/                       # pytest suite (581 tests)
```

`.quikode/` (gitignored, per workspace) — runtime state: SQLite db, per-task logs, worktrees, sccache, daemon.pid, orchestrator.heartbeat.

## Models in use

(Configured per-workspace in `.quikode/config.toml`; project-wide defaults below.)

| Phase | CLI | Default model |
|---|---|---|
| Planner | claude | claude-opus-4-7 |
| Doer | opencode | zai-coding-plan/glm-5.1 |
| Checker | codex | gpt-5.3-codex |
| Triage | claude | claude-opus-4-7 |
| Conflict resolver | claude | claude-opus-4-7 |
| Intent reviewer | claude | claude-haiku-4-5-20251001 |
| Progress | claude | claude-haiku-4-5-20251001 |
