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

## v3 state (what's landed as of 2026-05-04)

### Carryforward from v3 base (2026-05-03 and earlier)

- **Per-subtask commits + push** — each subtask commits independently; pre-commit hooks fire per slice (`worker.py:_run_subtask_set`, `tests/test_per_subtask_commit.py`).
- **Progress-check agent** — claude-haiku monitors struggling subtasks; flatlines BLOCK them after `cfg.subtask_flatline_block_count` consecutive verdicts (`tests/test_progress_check.py`).
- **AWAITING_MERGE polling + GraphQL review threads** — daemon polls open PRs every `cfg.review_poll_interval_s`, kicks off RESPONDING_TO_REVIEW worker (`orchestrator.py:_poll_review_threads`).
- **`git rebase --onto <parent_sha>`** for stacked children when their parent squash-merges to main (`worker.py:run_rebase_to_main` and `_rebase_to_base_branch`).
- **Daemon supervisor** — `quikode daemon start` wraps `quikode run` in a crash-restart loop with exponential backoff (`daemon.py:supervise`).
- **Orphan task recovery** on every restart (`state.py:Store.recover_orphan_tasks`).
- **Mid-flight parent-merge handling** — `tasks.needs_parent_rebase=1` flag set by orchestrator; worker checkpoints handle inline at 6 sites.
- **PR auto-recreation** when GitHub auto-closes a child PR on parent's `--delete-branch` merge.
- **Auto-merge (opt-in)** via `cfg.auto_merge_when_clean=True`.
- **DAG viewer** — `quikode tui`, press `g`.
- **ccusage cost capture** across claude/codex/opencode.
- **Workspace-scoped containers** with `qk_workspace=<8-hex>` label.

### Landed in 2026-05-04 driven session

- **Fixup decomposition** — `_run_fixup_round(kind=...)` invokes the fixup planner (claude-opus) to break final-check / CI / review failures into 1-5 mini-subtasks instead of a monolithic `_do(attempt=200+)` call. Each slice runs through the per-subtask doer/checker/commit gate. `cfg.fixup_max_rounds=3` caps. (`worker.py:_run_fixup_round`)
- **CI-failure-after-AWAITING_MERGE handler** — `_poll_review_threads` dispatches a CI-fix cycle when `pr_status.checks_status=='failure'`, mirroring the review-response path with `kind='fixup-ci'`. (`orchestrator.py:_schedule_ci_fix_response`, `worker.py:run_ci_fix_response`)
- **Branch divergence handling** — `_handle_branch_divergence_if_needed` at every subtask boundary detects upstream commits on the child's branch. Pure FF → reset --hard. Force-push → BLOCK. Diverged → auto-rebase via `_rebase_diverged_branch` with conflict-resolver fallback. (`worker.py`, `tests/test_branch_divergence_recovery.py`)
- **Stalled-future auto-recovery** — `_check_stalls` watches `responding_to_review` futures with no agent activity for >`stall_warn_seconds`, force-cancels + resets the task to AWAITING_MERGE for re-dispatch. Closed the pool-slot leak that caused multiple runaway scenarios. (`orchestrator.py:_check_stalls`)
- **Priority pick at slot-free** — `_pick_next` scores candidates via shared `scheduler.score_candidate` (stacked +50, dependents ×5, id-tiebreak). Replaces sorted-by-id picking. (`scheduler.py`, `tests/test_priority_pick.py`)
- **Subtask-boundary yield** (opt-in) — workers can surrender their slot to higher-priority queued candidates at safe boundaries. `cfg.preempt_at_subtask_boundary=True` + `cfg.preempt_yield_threshold` (default 200). (`worker.py:_maybe_yield_at_boundary`)
- **review_response_extra_slots** — review futures can exceed `max_parallel` by N (default 1) so reviews don't starve when the regular pool is saturated.
- **review_rounds_max cap** (default 15) — prevents codex find-everything-forever runaway. BLOCKs the task with a manual-merge/close note.
- **Settled-task notifications** (`cfg.notify_settled_channel = "ntfy" | "slack" | "both"`) — pings the operator when AWAITING_MERGE has been quiet for `cfg.notify_settled_after_s`. ntfy backend is the recommended primary (zero-auth, free, iOS/Android push). `quikode notify-test` verifies setup. (`quikode/notify.py`, `orchestrator.py:_maybe_notify_settled`)
- **Per-task abort** — `quikode abort` only kills containers matching the target task's slug, not workspace-wide.
- **Idempotent `_open_pr`** — re-entry on a task with existing pr_number/pr_url skips `gh pr create` instead of failing with "PR already exists".
- **Subtask transient cap** — `_check_subtask` returns `(verdict, text, transient)`; the loop free-retries on transient failures (container vanished, fast-fail) and BLOCKs after `cfg.subtask_transient_max_retries` consecutive transients.
- **Progress-check cadence seeded from retries** — local attempt counter starts at `subtasks.retries` so cadence keeps firing across daemon restarts.
- **ccusage cost sanity cap** ($50/call) — discards parser-misattribution outliers without poisoning the briefing total.
- **`--reason` flag** on retry/resume/abort — recorded in state_log.
- **STACK_READY includes PROVISIONING + FIXUP_PLANNING** — children don't briefly lose stacking eligibility during parent's transient states.
- **Resume re-pends cascade-skipped subtasks** — `quikode resume` un-skips downstream slices marked SKIPPED by the post-block sweep.
- **`quikode show` enrichments**: per-subtask cost rollup, progress-check verdict surfacing, review-thread categorization (addressed-by-commit vs auto-resolved-upstream vs unresolved).
- **TUI fixes** — pending hidden from primary table, viewport auto-scrolls to active subtask via `app.call_after_refresh`, detail panel given 2/3 height.
- **Daemon stop reliability** — supervisor schedules failsafe SIGKILL via `threading.Timer` if the inner orchestrator ignores SIGTERM within `CHILD_TERM_TIMEOUT_S`. Closed the orphaned-inner-orchestrator runaway class.
- **Lefthook v2 (`--files-from-stdin`)** + **python3** baked into dev image. Image rebuild required to pick up.

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
- Tests: `tests/` (pytest, `python -m pytest tests/ -q`) — **697 tests**, runs in <45s
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
12. Worker checkpoints for mid-flight parent-merge handling: 6 sites read `tasks.needs_parent_rebase` and run rebase + retarget inline before continuing. Sites: per-subtask in `_run_subtask_set`, entry to `_final_check_loop`, `_commit_push`, `_open_pr`, each iteration of `_poll_pr_loop`. The branch-divergence checkpoint is colocated.
13. Orphan-task recovery on daemon restart is mandatory. Worker SIGTERM mid-step otherwise leaves the task stuck in DOING_SUBTASK / CHECKING / etc. forever. See `state.py:Store.recover_orphan_tasks`.
14. Smart rebase scheduling: `_schedule_rebases_for_merged_parent` only triggers child rebase when child's PR is CONFLICTING or its base branch is deleted — not on every parent state-change. Avoids rebase storms.

**v3 driven-run gotchas (from the 2026-05-04 session):**

15. Lefthook v1's `--files <list>` flag is rejected by lefthook v2 ("flag provided but not defined"). v2 takes `--files-from-stdin` (and individual `--file` repeats). The pre-commit gate uses stdin form. See `worker.py:_pre_commit_gate`.
16. `urllib.request` headers are encoded as latin-1 — emoji like ✅ in Title raise `UnicodeEncodeError`. ntfy uses the `Tags` header (e.g. `white_check_mark`) for emoji rendering on the client side. See `notify.py:ntfy_send`.
17. Daemon SIGTERM doesn't reliably propagate to the inner orchestrator — supervisor exits, inner keeps running, then `cleanup_all_quikode` from a fresh daemon kills containers under the orphan, which loops on "container not found". Failsafe SIGKILL via threading.Timer in the supervisor's signal handler. See `daemon.py:_schedule_failsafe_kill`.
18. The legacy `quikode abort` called `cleanup_all_quikode` workspace-wide, killing ALL containers. Per-task abort matches by `qk-<task-slug>-*` prefix. See `cli.py:abort`.
19. Review-response futures could silently leak (provisioning fails before any agent_call, future stays "running" forever holding a slot). `_check_stalls` watches `responding_to_review` for >`stall_warn_seconds` of zero agent activity and force-cancels + re-pends to AWAITING_MERGE.
20. Local subtask-attempt counter resets to 0 on daemon restart, but `subtasks.retries` is cumulative. Progress-check cadence (fires at attempt == cfg.subtask_progress_check_after, then every N) used the local counter and never fired at high attempts when a subtask spanned multiple restarts. Fix: seed `attempt = subtasks.retries` on resume.
21. `gh pr create` fails non-recoverably when a PR already exists for the branch. `_open_pr` checks for existing pr_number/pr_url and skips re-creation.
22. ccusage's session-aggregate occasionally returns cumulative-since-install instead of a per-call delta when the snapshot baseline is missing. Symptom: a single call reported $292.89. Sanity cap (`_MAX_PER_CALL_COST_USD = 50`) zeros wildly-out-of-range values with a warning log.
23. CI-failure-after-AWAITING_MERGE was unhandled by the daemon's review-watcher. v2-style worker handled CI fail inline in `_poll_pr_loop`; v3 needs `_schedule_ci_fix_response` because the worker has handed off. See `orchestrator.py`.
24. Codex-style reviewers can find new nits indefinitely (R-0002 hit round 10 with $30+ in cycles). `cfg.review_rounds_max=15` BLOCKs the task with a "manual merge/close required" note when the cap is hit.
25. **Foreground daemon dies on shell SIGHUP** with no supervisor exit log — a closed terminal silently kills `quikode daemon start` and leaves containers orphaned (still doing work nobody reads). Use `quikode daemon start --detach` (added 2026-05-04 evening): forks, calls `os.setsid()`, redirects stdio at the daemon log so the supervisor outlives the launching shell. See `daemon.py:detach_into_background`.
26. **Supervisor only restarts on crash, not hang** — pre-watchdog, a hung inner orchestrator (lock contention, blocked subprocess.wait) left `child.wait()` blocking forever while heartbeat went cold. The watchdog (`daemon.py:_wait_with_watchdog`) reads heartbeat every 5s and SIGTERMs after two consecutive stale reads beyond `cfg.daemon_heartbeat_stale_kill_s` (default 600s). The crash path then handles backoff + restart. Set `daemon_heartbeat_stale_kill_s = 0` to disable.
27. **Speculative stacking churns children on every parent fixup round** — pre-readiness-gate, a child stacked the moment its parent opened a PR; codex auto-reviews on R-0002 hit round 11+, each round forcing a re-rebase on every child. `cfg.stacking_readiness="settled"` (with `cfg.stack_settle_quiet_s`) gates stacking on parent being quietly in AWAITING_MERGE for the quiet window. Default stays `"speculative"` for back-compat. See `scheduler.is_parent_stack_ready`.
28. **Picker ignored in-progress state across restarts** — score_candidate was pure (stacked +50, dependents ×5, id tiebreak) so an orphan-recovered task at 9/10 subtasks done scored identically to a fresh PENDING root. Resume-boost adds +25 max from subtask completion fraction + 15 if PR open, capped at +40 (still loses to a 9+ dependent fresh root). See `scheduler._resume_signals` and `scheduler.score_candidate`.

## Active work / context

- **R-0002** was the primary review-loop validation handle in the 2026-05-04 session. PR #143. Hit round 10 of fixup-review before being shipped. Subsequent runs will be the regression bed for the new code paths.
- **F-0002** (stdio→HTTP MCP migration in tanren) was done by the user directly outside quikode.
- The user keeps **glm-5.1 as doer** to balance subscription usage across providers. Subtask breakdown + progress-check agent are the convergence mitigation.
- The user reviews tanren PRs; quikode does not auto-merge tanren unless `cfg.auto_merge_when_clean=True` is explicitly set in that workspace's config.
- **Settled-task notifications** are configured for the tanren workspace via ntfy.sh — pings the operator when AWAITING_MERGE has been quiet for 30min. Don't disable without checking with the user.

## Conventions when editing this codebase

- Run `python -m pytest tests/ -q` after any change touching `quikode/`. **709 tests**; runs in <50s.
- Run `ruff check quikode/ tests/` and `ruff format --check quikode/ tests/` before committing. Strict ruleset enabled — see `pyproject.toml [tool.ruff.lint]`.
- `ty check quikode/` is configured but ty is alpha; treat advisory.
- Don't break the running orchestrator. If quikode is mid-run when you edit, the in-memory module is already loaded — your edits affect the *next* run.
- Don't merge tanren PRs from quikode autonomously unless config explicitly opts in.
- **No in-function imports** (`PLC0415` enforced). Every `import` lives at module top.
- TypedDict for SQLite rows (`TaskRow`, `SubtaskRow`, `ReviewThreadRow`, etc.). Pydantic for agent-emitted shapes (`Plan`, `Subtask`, `FixupPlan`, `AgentResult`, `IntentReviewOutcome`).

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
# --detach so a shell hangup doesn't take the supervisor with it
quikode daemon start --detach --max-parallel 5 --retry-failed
quikode daemon status         # heartbeat freshness
quikode tui                   # mission-control dashboard; press `g` for DAG viewer
quikode notify-test           # verify settled-task ntfy delivery
quikode daemon stop           # SIGTERM-driven clean shutdown
```

The tanren workspace currently runs at `--max-parallel 5` with stacking
(`within-milestone`), priority-pick, subtask-boundary yield, and ntfy
notifications enabled (see `.quikode/config.toml`). Resource budget
allows ~5-7 in-flight tasks; CPU is the binding constraint.

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
