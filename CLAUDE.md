# bootstrap notes for future Claude sessions

Entry point for picking up this project cold. Read top-to-bottom.

## What this is

**quikode** is a Python orchestrator (CLI + Textual TUI) that drives AI
coding agents — claude-code, codex, opencode — through a task DAG, one
Docker container per task, planner → doer → checker → triage → commit →
PR → review-loop → merge. Built to drive the [tanren](https://github.com/trevorWieland/tanren)
roadmap (~233 nodes), but the core is generic.

Hot files:
- `quikode/worker.py` — per-task FSM driver (~2.6k LOC)
- `quikode/orchestrator.py` — threadpool scheduler + review-watcher + auto-rebase + auto-merge
- `quikode/daemon.py` — supervisor that restarts the orchestrator on crash with backoff
- `quikode/state.py` — `State` enum + SQLite store + orphan-recovery
- `quikode/cli.py` — Typer commands (init / doctor / run / daemon / show / briefing / unblock / resume / demo / tui / ...)
- `quikode/config.py` — Pydantic `Config` (every knob, with `ge=`/`le=` bounds the TUI re-uses)

## v3 state (what's landed as of 2026-05-03)

- **Per-subtask commits + push** — each subtask commits independently; pre-commit hooks fire per slice (`worker.py:_subtask_loop`, `tests/test_per_subtask_commit.py`).
- **Progress-check agent** — claude-haiku monitors struggling subtasks and BLOCKs on flatline (`config.subtask_progress_check_*`, `tests/test_progress_check.py`).
- **AWAITING_MERGE polling + GraphQL review threads** — daemon's review-watcher polls open PRs, surfaces new unresolved threads via `github_graphql.get_review_threads`, kicks off RESPONDING_TO_REVIEW worker (`orchestrator.py:_poll_review_threads`).
- **`git rebase --onto <parent_sha>`** for stacked children when their parent squash-merges to main (`worker.py:run_rebase_to_main` and `_rebase_to_base_branch`).
- **Daemon supervisor** — `quikode daemon start` wraps `quikode run` in a crash-restart loop with exponential backoff (`daemon.py:supervise`).
- **Orphan task recovery** — on every `quikode run` startup, tasks stuck in active states are reset to PENDING (with `resume_from_existing_subtasks=1`) or AWAITING_MERGE (`state.py:Store.recover_orphan_tasks`).
- **Mid-flight parent-merge handling** — `tasks.needs_parent_rebase=1` flag set by orchestrator; worker checkpoints handle it inline at 5 sites (per-subtask, final-check, commit_push, open_pr, poll_pr_loop).
- **PR auto-recreation** — when GitHub auto-closes a child PR on parent's `--delete-branch` merge, the rebase worker creates a fresh PR pointing at main.
- **Auto-merge (opt-in)** — `cfg.auto_merge_when_clean=True` merges PRs that are OPEN + MERGEABLE + checks SUCCESS + threads resolved + age ≥ `auto_merge_min_age_s` (`orchestrator.py:_attempt_auto_merge`).
- **DAG viewer** — `quikode tui` then press `g` for a whole-project visualization (`design-tui-dag-viewer.md` v1).
- **ccusage cost capture** — uniform token/cost across claude/codex/opencode via the ccusage npm packages (`quikode/agents/ccusage.py`).
- **Workspace-scoped containers** — `qk_workspace=<8-hex>` label so parallel workspaces don't tear each other down.

## Where to look

| Doc | Purpose |
|---|---|
| `README.md` | User-facing command reference + quick start |
| `docs/architecture.md` | Components, FSM, data flow, branching model |
| `docs/runbook-operations.md` | Daily ops: starting runs, monitoring, reviewing PRs, stopping cleanly |
| `docs/runbook-incident-response.md` | Symptom → recovery for the common breakage modes |
| `docs/runbook-tanren-watch-points.md` | Tanren-specific gotchas (BDD, sccache, R-0001 history) |
| `docs/future-work.md` | Open candidates with priority indicators |
| `docs/lessons-learned.md` | Empirical observations from real runs |
| `docs/design-v2.md` | Historical: v2 design (subtasks, conflicts, intent, stacking) — IMPLEMENTED |
| `docs/design-stacked-diffs-fix.md` | Historical: v3 stacked-diffs comprehensive fix — IMPLEMENTED |
| `docs/design-per-subtask-commit.md` | Historical: per-subtask commit design — IMPLEMENTED |
| `docs/design-tui.md` | TUI design (mostly implemented; some v1.1 items pending) |
| `docs/design-tui-dag-viewer.md` | DAG viewer design — IMPLEMENTED v1 |
| `docs/archive/` | Session snapshots + superseded planning docs |

## Where things live on disk

- Source: `/home/trevor/github/quikode/`
- Tests: `tests/` (pytest, `python -m pytest tests/ -q`) — **581 tests**, runs in <2s
- Target repo for tanren runs: `/home/trevor/github/tanren/`
- Workspaces: `/home/trevor/github/quikode-runs/{tanren,fixture}/` (each holds its own `.quikode/`)
- Fixture (proof-of-life FastAPI app): `/home/trevor/github/quikode-fixture/`

## Critical knowledge that's not obvious from reading the source

These all blocked progress at some point and have been fixed; don't re-discover them.

**Container / agent-CLI gotchas (from v0.1):**

1. `codex --dangerously-bypass-approvals-and-sandbox` is mandatory inside docker. Without it, codex's bwrap inner sandbox can't create user namespaces in unprivileged containers, silently falls back to a GitHub-API file fetch, and emits bogus FAIL verdicts.
2. claude-code needs `~/.claude.json` at `$HOME` root in addition to `~/.claude/`. The entrypoint copies both.
3. Auth dirs must be mounted RO at `/host-auth/*` and copied to writable container locations. RW-mounting lets parallel containers fight over each agent's session DB.
4. `bash -lc` strips Dockerfile `ENV PATH`. Install agent-CLI PATH additions via `/etc/profile.d/quikode.sh`.
5. Worktrees need the parent repo's `.git/` mounted at the same absolute path inside the container.
6. `gh auth setup-git` runs in the entrypoint (with `/tmp/.git-credentials` fallback) so `git push` works over HTTPS with `GITHUB_TOKEN`.
7. Each fresh quikode run uses a unique branch suffix (`quikode/<task-id>-<6hex>`). Never reuse a branch name across runs.

**v3 stacked-diff gotchas (from the 3-run E2E):**

8. `git rebase --continue` inside a container needs `git -c core.editor=true rebase --continue`. Without it: `Terminal is dumb, but EDITOR unset`. See `worker.py:_resolve_one_conflict_step`.
9. Multi-conflict rebases iterate. A 4-commit rebase can hit conflict on commit 1, then on commit 2 after `--continue`. The resolver loops on `_rebase_in_progress()` until done or cap. See `worker.py:_spawn_conflict_resolver`.
10. `git rebase --onto <parent_sha>` (not plain `git rebase origin/main`) is mandatory for stacked children. After parent's squash-merge, the parent's commits are folded — replaying them creates duplicate-commit conflicts. The local parent ref persists post-deletion; capture `parent_sha` before fetching. See `worker.py:run_rebase_to_main`.
11. GitHub auto-closes a child PR when the parent merges with `--delete-branch`. The rebase worker creates a fresh PR pointing at main, not just `gh pr edit --base main`. See `worker.py:run_rebase_to_main` (PR-recreation branch).
12. Worker checkpoints for mid-flight parent-merge handling: 5 sites read `tasks.needs_parent_rebase` and run rebase + retarget inline before continuing. Sites: per-subtask in `_subtask_loop`, entry to `_final_check_loop`, `_commit_push`, `_open_pr`, each iteration of `_poll_pr_loop`.
13. Orphan-task recovery on daemon restart is mandatory. Worker SIGTERM mid-step otherwise leaves the task stuck in DOING_SUBTASK / CHECKING / etc. forever. See `state.py:Store.recover_orphan_tasks`.
14. Smart rebase scheduling: `_schedule_rebases_for_merged_parent` only triggers child rebase when child's PR is CONFLICTING or its base branch is deleted — not on every parent state-change. Avoids rebase storms.

## Active work / context

- **R-0001** is still the canonical first-real-task validation. PR #120 has been the primary handle as of 2026-05-03. Do NOT re-attempt R-0001 from scratch — check `quikode show R-0001` first.
- **F-0002** (stdio→HTTP MCP migration in tanren) was done by the user directly outside quikode.
- The user keeps **glm-5.1 as doer** to balance subscription usage across providers. Subtask breakdown + progress-check agent are the convergence mitigation.
- The user reviews tanren PRs; quikode does not auto-merge tanren unless `cfg.auto_merge_when_clean=True` is explicitly set in that workspace's config.

## Conventions when editing this codebase

- Run `python -m pytest tests/ -q` after any change touching `quikode/`. **581 tests**; runs in <2s.
- Run `ruff check quikode/ tests/` and `ruff format --check quikode/ tests/` before committing. Strict ruleset enabled — see `pyproject.toml [tool.ruff.lint]`.
- `ty check quikode/` is configured but ty is alpha; treat advisory.
- Don't break the running orchestrator. If quikode is mid-run when you edit, the in-memory module is already loaded — your edits affect the *next* run.
- Don't merge tanren PRs from quikode autonomously unless config explicitly opts in.
- **No in-function imports** (`PLC0415` enforced). Every `import` lives at module top.
- TypedDict for SQLite rows (`TaskRow`, `SubtaskRow`, `ReviewThreadRow`, etc.). Pydantic for agent-emitted shapes (`Plan`, `Subtask`, `AgentResult`, `IntentReviewOutcome`).

## Running things

```bash
# tests
cd /home/trevor/github/quikode && source .venv/bin/activate
python -m pytest tests/ -q

# fixture smoke (~4 min, validates the loop without burning a tanren cycle)
cd /home/trevor/github/quikode-runs/fixture
quikode reset --yes --close-prs
quikode run --max-parallel 1

# tanren run under daemon supervisor (recommended)
cd /home/trevor/github/quikode-runs/tanren
quikode briefing
quikode daemon start --max-parallel 3 --retry-failed
quikode daemon status         # heartbeat freshness
quikode tui                   # mission-control dashboard; press `g` for DAG viewer
quikode daemon stop           # SIGTERM-driven clean shutdown
```

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
