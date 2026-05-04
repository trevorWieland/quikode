# orientation — quikode for tanren

**You are an agent helping operate the quikode orchestrator against the
tanren codebase.** This document is your starting context. Read top-to
-bottom; everything else is a reference linked from here.

## What you're doing

- **quikode** is a Python orchestrator at `/home/trevor/github/quikode/`
  that drives AI coding agents (claude, codex, opencode) through a task
  DAG, one Docker container per task. It runs the planner → doer →
  checker → triage → commit → PR → review → merge loop end-to-end.
- **tanren** is the target repo at `/home/trevor/github/tanren/` — a
  ~233-node DAG of R-* and F-* tasks. R-0001 has merged; R-0002+ is
  open work. The DAG file is `tanren/docs/roadmap/dag.json`.
- **Your role** is to run quikode against tanren tasks: kick off runs,
  monitor progress, post review feedback when needed, merge PRs that
  pass review. The system handles the rest autonomously.

## How to start

The tanren workspace lives at `/home/trevor/github/quikode-runs/tanren/`.
All `quikode <subcommand>` calls below assume that's your cwd.

```bash
# 1. Confirm environment health
cd /home/trevor/github/quikode
source .venv/bin/activate
python -m pytest tests/ -q       # 697 tests, <45s — must pass clean
ruff check quikode/ tests/       # must be clean

# 2. Confirm host + agent CLIs are healthy
cd /home/trevor/github/quikode-runs/tanren
quikode doctor                   # docker, gh auth, claude/codex/opencode
quikode resources                # computed cap + host actuals

# 3. Snapshot the workspace state + verify notify delivery
quikode briefing                 # in-flight, awaiting, blocked, merges, cost
quikode notify-test              # verify ntfy.sh push reaches your phone

# 4. Start the daemon (Phase 3 / parallel-5 with stacking + notifications)
quikode daemon start --max-parallel 5 --retry-failed

# 5. Monitor (in another terminal)
quikode tui                      # mission-control; press `g` for DAG viewer
# or non-interactive
quikode watch --active
quikode tail <task-id>
```

For the rollout phasing (when to scale parallelism, enable stacking,
turn on auto-merge), see `docs/runbook-tanren-watch-points.md` →
"Tanren rollout phases". The tanren workspace is currently at Phase 3
(stacking on, parallel-5).

## Current state (as of 2026-05-04)

- All v3 base features (stacked diffs, review loop, auto-merge) shipped.
- v3 driven-run hardening landed in 2026-05-04 session: fixup
  decomposition (final-check / CI / review), priority pick,
  subtask-boundary yield, branch-divergence handling, stalled-future
  recovery, settled-task notifications (ntfy + slack), CI-failure-
  after-AWAITING_MERGE handling, review_rounds_max cap, per-task abort,
  idempotent _open_pr, ccusage cost sanity cap. See `docs/lessons-
  learned.md` "v3 driven-run findings (2026-05-04)" for the full list.
- 697 pytest tests pass, ruff lint+format clean.
- Tanren workspace runs at `--max-parallel 5` + `stacking_strategy =
  "within-milestone"` + `notify_settled_channel = "ntfy"`. R-0002 is
  the canonical review-loop validation handle (PR #143).
- Resource budget on the current host: 5-7 in-flight at most;
  cpu_per_task = 2, mem_per_task_gb = 12.

## Critical knowledge

These all blocked progress at some point and are fixed; don't
re-discover them. Full context in `CLAUDE.md`.

- **codex `--dangerously-bypass-approvals-and-sandbox` is mandatory
  inside docker.** Without it the checker silently falls back to a
  GitHub-API file fetch and gives bogus FAIL verdicts.
- **claude-code needs `~/.claude.json` at `$HOME` root** in addition
  to `~/.claude/`. Entrypoint copies both.
- **Auth dirs mount RO at `/host-auth/*`, copied to writable paths.**
  RW-mounting lets parallel containers fight over session DBs.
- **`bash -lc` strips Dockerfile `ENV PATH`.** Agent-CLI PATH
  additions live in `/etc/profile.d/quikode.sh` so login shells pick
  them up.
- **Worktrees need the parent repo's `.git/` mounted at the same
  absolute path inside the container** so the worktree pointer
  resolves.
- **`gh auth setup-git` runs in the entrypoint** for `git push` over
  HTTPS with `GITHUB_TOKEN`.
- **Each fresh quikode run uses a unique branch suffix**
  (`quikode/<task-id>-<6hex>`). Never reuse a branch name across runs.
- **Squash-merge with `--delete-branch` is tanren policy.** Stacked
  children pointing at a deleted parent branch get auto-closed by
  GitHub. Quikode's auto-recreation handles this — see
  `runbook-incident-response.md` "PR auto-closed".
- **`git rebase --onto origin/main <parent_sha>`** is how stacked
  children rebase after parent merge (drops the parent's now-squashed
  history).
- **Review-thread comments only.** Bare review-body or issue comments
  are intentionally ignored. Post inline on a specific line via
  `gh api -X POST /repos/<o>/<r>/pulls/<n>/comments`.
- **Validation runs require `--max-parallel >= 2`.** Stacked diffs and
  sibling-conflict paths only fire under parallelism.
- **Use the daemon, not bare `quikode run`** for any real tanren work
  — the supervisor restarts on crash with backoff.
- **Don't push `--max-parallel` past 7 without checking
  `quikode resources`** — tanren `cargo build` peaks at ~3GB per
  container, so headroom matters. The current ceiling is ~5-7 on
  a 78GB host; SQLite contention also rises non-linearly past 7.
- **`cfg.notify_settled_channel = "ntfy"`** is configured for the
  tanren workspace. Don't disable without checking with the user —
  they rely on the phone push for review-ready signals.
- **`cfg.review_rounds_max = 15`** caps codex find-everything-forever
  cycles. A task hitting this BLOCKs with "manual merge/close
  required" — that's the operator's signal to make a final call.

## Where to look

| You want to | Read |
|---|---|
| Run a tanren task | `docs/runbook-operations.md` + `docs/runbook-tanren-watch-points.md` |
| Something broke | `docs/runbook-incident-response.md` |
| What's the architecture | `docs/architecture.md` |
| What's still on the table | `docs/future-work.md` |
| Empirical observations | `docs/lessons-learned.md` |
| Bootstrap a new agent's deeper context | `CLAUDE.md` |
| TUI design / panels / keybindings | `docs/design-tui.md`, `docs/design-tui-dag-viewer.md` |

## Conventions

- Don't merge tanren PRs without review (unless `auto_merge_when_clean
  = true` is explicitly set, and only at Phase 4+).
- Don't break the running orchestrator — your code edits affect the
  *next* run, not the current one.
- 697 tests + ruff strict — run them after any `quikode/` change.
- No in-function imports (PLC0415 enforced). Every `import` lives at
  module top.
- Use Pydantic for agent-emitted shapes; TypedDict for SQLite rows.
- Use the daemon (`quikode daemon start`) not bare `quikode run` for
  any real tanren work — supervisor restarts on crash.
- Don't reuse branch names across runs. Branch suffix is auto-unique.
- Workspace-scoped containers carry a `qk_workspace=<8hex>` label
  derived from the state-dir path. `clean-containers` filters by it,
  so parallel workspaces are safe.

## Models in use

(Configured per-workspace in `.quikode/config.toml`; project-wide
defaults below.)

| Phase | CLI | Default model |
|---|---|---|
| Planner | claude | claude-opus-4-7 |
| Doer | opencode | zai-coding-plan/glm-5.1 |
| Checker | codex | gpt-5.3-codex |
| Triage | claude | claude-opus-4-7 |
| Conflict resolver | claude | claude-opus-4-7 |
| Intent reviewer | claude | claude-haiku-4-5-20251001 |
| Progress | claude | claude-haiku-4-5-20251001 |

The user keeps glm-5.1 as doer to balance subscription usage across
providers. Don't swap models without asking.

## When to ask vs act

**Act autonomously:**

- Starting a run, monitoring, posting review comments, merging PRs
  that look right.
- Fixing quikode bugs you encounter (write a test, edit the code, run
  pytest + ruff, commit).
- Advancing through rollout phases when the documented success
  criteria are met.
- Handling BLOCKED / FAILED tasks per `runbook-incident-response.md`
  (retry, resume, unblock).
- Reverting fixture `main` between fixture validation runs (per
  `runbook-operations.md` "Fixture between-run reset").

**Ask the user:**

- Changing tanren's `main` branch (force-push, history rewrite, any
  destructive op).
- Enabling auto-merge for tanren for the first time (Phase 4
  transition).
- Advancing to Phase 5 (scaling up parallelism past 3).
- Anything that touches multi-workspace coordination.
- Anything that costs >$5 to recover from if wrong.
- Swapping doer / planner / checker models away from the configured
  defaults.
- Re-attempting R-0001 from scratch — it's already merged. Check
  `quikode show R-0001` first.
