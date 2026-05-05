# bootstrap notes for future Claude sessions

Read top-to-bottom; pick up the project cold.

## What this is

**quikode** is a Python orchestrator (CLI + Textual TUI) that drives AI coding agents (claude-code, codex, opencode) through a task DAG, one Docker container per task: planner → doer → checker → triage → commit → pre-PR audit → PR → review-loop → merge. Built to drive [tanren](https://github.com/trevorWieland/tanren) (~233 nodes), but the core is generic.

Hot files:
- `quikode/worker.py` — per-task FSM driver
- `quikode/orchestrator.py` — threadpool scheduler + review-watcher + auto-rebase + auto-merge
- `quikode/daemon.py` — supervisor (crash-restart, heartbeat watchdog, --detach)
- `quikode/state.py` — `State` enum + SQLite store + orphan-recovery
- `quikode/scope_review.py` — advisory diff-vs-declared lane judge for `commit_subtask`
- `quikode/pre_pr_audit.py` — 4-stage gate (local-CI / rubric / standards / behavior)
- `quikode/cli.py` — Typer commands
- `quikode/config.py` — Pydantic `Config` (every knob with `ge=`/`le=` bounds)

## Reinstall workflow — read first

`quikode daemon start` runs the version installed via `uv tool install`, NOT the source checkout. Editing `quikode/*.py` in this repo has zero effect on a running daemon until reinstall. Prompts at `prompts/*.md` ARE picked up immediately because they're loaded from the repo's path; ALL Python code edits need:

```bash
./scripts/reinstall.sh        # tests + lint + format + uv tool install --reinstall .
./scripts/reinstall.sh --skip-tests   # reinstall only, when you've already tested
```

`pyproject.toml` declares `[tool.hatch.build.targets.wheel.force-include]` for `prompts/` and `docker/` so they ship inside the wheel. Don't break that.

After reinstall, restart the daemon from the workspace dir:

```bash
cd <workspace> && quikode daemon stop && quikode daemon start --detach --max-parallel N --retry-failed
```

## Where to look

| Doc | Purpose |
|---|---|
| `README.md` | User-facing command reference + quick start |
| `docs/architecture.md` | Components, FSM, data flow, branching model |
| `docs/runbook-operations.md` | Daily ops |
| `docs/runbook-incident-response.md` | Symptom → recovery for common breakage modes |
| `docs/runbook-tanren-watch-points.md` | Tanren-specific gotchas (BDD, sccache, R-* history) |
| `docs/future-work.md` | Open candidates with priority indicators |
| `docs/lessons-learned.md` | Empirical observations from real runs |
| `docs/archive/` | Superseded design docs |

## Where things live

- Source: `/home/trevor/github/quikode/`
- Tests: `tests/` — **790 tests**, ~60s
- Tanren clone: `/home/trevor/github/tanren/`
- Workspaces: `/home/trevor/github/quikode-runs/{tanren,fixture}/`

## Current state (as of 2026-05-05)

Active features (the layered gate, audit pipeline, soft-resume retry, scope review etc):

- **v3.5 state vocab**: `PENDING_CI`, `AWAITING_REVIEW`, `MERGE_READY`, `TRIAGING_FEEDBACK`, `ADDRESSING_FEEDBACK`. `AWAITING_MERGE` / `RESPONDING_TO_REVIEW` removed; `Store._migrate` rewrites legacy values.
- **v3.6 pre-PR pipeline**: 4-stage gate between `_final_check_loop` and `_open_pr`. Stages run per cycle, failures bundle → `_run_fixup_round(kind="fixup-pre-pr-audit")` → subtask loop → re-enter. Cap `cfg.pre_pr_audit_max_cycles=10`. Each gate owns a distinct dimension; prompts forbid out-of-scope checks (rubric ≠ build, standards = repo-doc-cited, behavior = empirical witnesses only).
- **v3.7 layered subtask gate**: `cfg.subtask_check_command` (default `just check`) runs as Layer-1 inside `_check_subtask` BEFORE the LLM checker. Fail → synthesized `_CheckerOutcome(FAIL)` with command output as triage feedback; pass → LLM acceptance check. Doer prompt explicitly tells the implementor it'll be judged this way and to run the command itself.
- **v3.7 advisory scope review**: `commit_subtask` runs `git add -A` then compares actual diff to `subtask.files_to_touch`. On drift, `cfg.progress` (codex gpt-5.4-mini) judges legitimate (auto-gen outputs, refactor splits, companion tests) vs overreach. Legit → record `accepted_files` and commit; overreach → `git reset HEAD --` + synthesized FAIL. Default-LEGITIMATE on agent failure so reviewer infra issues don't block commits.
- **v3.6 BLOCKED-as-bug forensics**: every transition into `BLOCKED` auto-captures a snapshot via `Store.capture_block_forensics`. Surfaced in `quikode unblock <id>`. Framing: a BLOCK is a quikode failure to detect/work around something earlier, not a graceful gate.
- **Multi-parent stacking**: synthetic merge-base branch (`quikode/<id>-base-<6hex>`) when a child has 2+ parents. `cfg.stacking_readiness="settled"` gates stacking on parent quietly settled in the audit pipeline.
- **Soft-resume `--retry-failed`**: preserves branch + worktree + subtasks + audit artifacts. The destructive escape hatch is `quikode retry <id>` (operator-explicit). `--retry-failed` should never destroy work.
- **Daemon**: `--detach` (fork+setsid+stdio redirect) for SIGHUP resilience; heartbeat watchdog SIGTERMs after 600s stale; failsafe SIGKILL on supervisor stop.
- **Per-subtask commits**: each subtask commits independently; pre-commit hooks fire per slice. Idempotent re-entry (existing branch + worktree + git-registered → reuse). `subtasks.accepted_files` records reviewer-evolved lane.
- **TUI**: proportional layout (`1fr` / `2fr` / `3fr` weights, no hardcoded heights except header/footer/Input). DAG viewer at `g`.
- **Settled-task notifications** (tanren config): ntfy.sh ping when `AWAITING_REVIEW`/`MERGE_READY` quiet for `cfg.notify_settled_after_s`.

## Critical knowledge that's not obvious

These blocked progress and have been fixed; don't re-discover.

1. `codex --dangerously-bypass-approvals-and-sandbox` is mandatory inside docker (bwrap user-namespace fallback to GitHub-API silently emits FAIL).
2. `git rebase --continue` inside container needs `git -c core.editor=true rebase --continue` (`worker.py:_resolve_one_conflict_step`).
3. `git rebase --onto <parent_sha>` (not `origin/main`) for stacked children after parent squash-merge — capture `parent_sha` BEFORE fetching (`worker.py:run_rebase_to_main`).
4. GitHub auto-closes child PR on parent's `--delete-branch` merge → rebase worker creates a fresh PR pointing at main.
5. Worker checkpoints (6 sites) read `tasks.needs_parent_rebase` for mid-flight parent-merge handling.
6. Lefthook v2 takes `--files-from-stdin` (not `--files <list>`); the pre-commit gate uses stdin form (`worker.py:_pre_commit_gate`).
7. `urllib.request` headers are latin-1; ntfy uses the `Tags` header (e.g. `white_check_mark`) for emoji rendering (`notify.py:ntfy_send`).
8. Daemon SIGTERM may not propagate to inner orchestrator → supervisor schedules failsafe SIGKILL via `threading.Timer` (`daemon.py:_schedule_failsafe_kill`).
9. Local subtask-attempt counter resets on daemon restart but `subtasks.retries` is cumulative — seed `attempt = subtasks.retries` so progress-check cadence keeps firing across restarts.
10. `Subtask` Pydantic model uses `extra="forbid"`. The `addresses_findings` field on the model is required for fixup-pre-pr-audit slices that map findings → subtasks. Adding new fields to the planner output requires schema updates first; otherwise StrictUndefined / extra="forbid" will reject the plan and BLOCK the task.
11. ccusage occasionally returns cumulative-since-install instead of per-call delta. Sanity cap `_MAX_PER_CALL_COST_USD = 50` zeros wildly-out-of-range values.
12. The `commit_subtask` gate was previously strict (`git add -- <files_to_touch>`); now uses `git add -A` + advisory scope review. Planner-declared ghost paths (e.g. `messages.ts` when Paraglide generates `messages.js`) no longer block commits.

## Conventions when editing

- Run tests after any `quikode/` change: **790 tests**, ~60s. `./scripts/reinstall.sh` runs lint + format + tests + reinstall in one shot.
- Don't break the running orchestrator. The in-memory module is already loaded — your edits affect the NEXT restart, AFTER reinstall.
- Don't merge tanren PRs from quikode autonomously unless config explicitly opts in (`cfg.auto_merge_when_clean=True`).
- **No in-function imports** (`PLC0415` enforced).
- TypedDict for SQLite rows; Pydantic for agent-emitted shapes (`Plan`, `Subtask`, `FixupPlan`, `AgentResult`, `IntentReviewOutcome`, `ScopeReviewResult`).
- Default to no comments; only add when the WHY is non-obvious. Don't reference current task/fix in code comments — they belong in PR descriptions and rot.
- Multi-paragraph docstrings are an antipattern. One short line max for module / function docs unless the function is genuinely subtle.

## Models in use

(Per-workspace in `.quikode/config.toml`; project-wide defaults below.)

| Phase | CLI | Model |
|---|---|---|
| Planner | codex | gpt-5.5 |
| Doer | opencode | zai-coding-plan/glm-5.1 |
| Checker | codex | gpt-5.3-codex |
| Triage | codex | gpt-5.5 |
| Conflict resolver | codex | gpt-5.5 |
| Intent reviewer | codex | gpt-5.4-mini |
| Progress / scope review | codex | gpt-5.4-mini |

## Running things

```bash
# tests + reinstall in one shot
./scripts/reinstall.sh

# fixture smoke (~4 min)
cd /home/trevor/github/quikode-runs/fixture
quikode reset --yes --close-prs
quikode run --max-parallel 1

# tanren run under daemon supervisor (recommended)
cd /home/trevor/github/quikode-runs/tanren
quikode briefing
quikode daemon start --detach --max-parallel 5 --retry-failed   # --detach so SIGHUP doesn't kill it
quikode daemon status                                            # heartbeat freshness
quikode tui                                                      # press `g` for DAG viewer
quikode daemon stop                                              # clean SIGTERM-driven shutdown
```

Tanren workspace runs at `--max-parallel 5` with stacking (`within-milestone`), priority-pick, subtask-boundary yield, ntfy notifications, and `subtask_check_command = "just check"`. Resource budget allows ~5-7 in-flight; CPU is the binding constraint.
