# quikode architecture

Reference for the current shape of the system (v3, all phases landed).
For day-to-day ops, see `runbook-operations.md`. For empirical findings
that drove each piece, see `lessons-learned.md`.

## Top-level shape

```
        ┌─────────────────────────────────────────────────────────────┐
        │  quikode/daemon (supervisor)                                │
        │   crash-restart with exponential backoff (60s/5m/30m)       │
        │   writes daemon.pid; spawns child quikode run               │
        └────────────────┬────────────────────────────────────────────┘
                         │ spawns / SIGTERMs
                         ▼
        ┌─────────────────────────────────────────────────────────────┐
        │  quikode (host process: orchestrator)                       │
        │  ┌───────────┐  ┌─────────────────┐  ┌──────────────────┐  │
        │  │ CLI/Typer │→ │  Orchestrator   │→ │  TaskWorker      │  │
        │  │ (or TUI)  │  │ (threadpool +   │  │ (one per task)   │  │
        │  └───────────┘  │  review-watcher)│  └────────┬─────────┘  │
        │                 │ + auto-rebase   │           │             │
        │                 │ + auto-merge    │           │             │
        │                 └────────┬────────┘           │             │
        │                          ↑                    │             │
        │                  ┌───────┴────────┐           ▼             │
        │                  │ SQLite Store   │    ┌──────────┐         │
        │                  │ tasks/subtasks │    │ Agents   │         │
        │                  │ state_log      │    │ wrappers │         │
        │                  │ artifacts      │    │ (claude/ │         │
        │                  │ agent_calls    │    │  codex/  │         │
        │                  │ review_threads │    │  opencode│         │
        │                  │ progress_checks│    │  +ccusage│         │
        │                  │ intent_reviews │    └─────┬────┘         │
        │                  │ container_stats│          │              │
        │                  └────────────────┘          │ docker exec  │
        └────────────────────────────────────────────────────────────┘
                                                       │
                                                       ▼
                          ┌──────────────────────────────────────┐
                          │  per-task dev container (qk-...)     │
                          │   /workspace = git worktree (rw)     │
                          │   /host-auth/* (ro) → copied to $HOME│
                          │   postgres sidecar on isolated net   │
                          │   shared sccache mount (rw)          │
                          │                                      │
                          │   [agent CLI runs here]              │
                          └──────────────────────────────────────┘
```

## Components

### CLI (`quikode/cli.py`)

Typer-based, ~30 commands. Exposed as `quikode` and `qk`. Reads
`.quikode/config.toml` from cwd or any ancestor.

Notable command groups:
- `init` / `doctor` / `build-image` — setup
- `run` / `daemon start|stop|status` — orchestrator drivers
- `briefing` / `status` / `watch` / `show` / `subtasks` / `tail` / `logs` / `dag-stats` / `ready` / `explain` — read-only views
- `retry` / `resume` / `unblock` / `abort` / `mark-merged` — task interventions
- `demo` / `export` — review aids
- `reset` / `prune` / `clean-containers` / `disk-usage` — maintenance
- `tui` — Textual mission-control

### Orchestrator (`quikode/orchestrator.py`)

DAG-aware scheduler running up to `max_parallel` workers in a
`ThreadPoolExecutor`. Each tick:

1. **Pick next ready task** — `scheduler.collect_pick_candidates` + `scheduler.score_candidate`. Score = `stacked_boost(50) + unblock_boost(5×deps) − id_penalty`. Replaces the older topo-only picker; same DAG constraints (stacking depth/breadth caps, dependency satisfaction) but priorities high-fan-out / stacked candidates over leaf roots when slots free up.
2. **Submit worker future** (`_run_one`).
3. **Poll review threads** (`_poll_review_threads`) — every `cfg.review_poll_interval_s`, fetch `gh pr view --json statusCheckRollup,...` + `github_graphql.get_review_threads(repo, pr)` for every AWAITING_MERGE PR. Diff against `review_threads` table. New unresolved threads → `_schedule_review_response` → spawns RESPONDING_TO_REVIEW worker. CI flipped to FAILURE post-AWAITING_MERGE → `_schedule_ci_fix_response` → `worker.run_ci_fix_response`. Settled-task ping → `_maybe_notify_settled` (per-task, once, after `cfg.notify_settled_after_s` quiet).
4. **Auto-merge** (`_attempt_auto_merge`) — opt-in via `cfg.auto_merge_when_clean`. Gates on PR OPEN+MERGEABLE+checks SUCCESS+threads resolved+age. Merges via `gh pr merge --squash --delete-branch`; sets `tasks.auto_merged=1`.
5. **Schedule rebases for merged parents** (`_schedule_rebases_for_merged_parent`) — for each child of a just-merged parent, decide between:
   - Non-active child + child PR is CONFLICTING / base branch deleted → submit `_run_rebase_to_main_one` future.
   - Active child → set `tasks.needs_parent_rebase=1`. Worker checkpoints handle rebase inline.
6. **Sample container stats** (`_sample_container_stats`) — `docker stats` snapshot every `cfg.container_stats_sample_seconds`, persisted to `container_stats`.
7. **Stall-warn + stalled-future recovery** (`_check_stalls`) — DOING tasks with quiet worktree mtime past `cfg.stall_warn_seconds` log a warning. `responding_to_review` futures with zero agent_call activity for >`cfg.stall_warn_seconds` are force-cancelled and reset to AWAITING_MERGE for re-dispatch (closes pool-slot leak class).
8. **Heartbeat** (`_write_heartbeat`) — JSON to `.quikode/orchestrator.heartbeat` so the supervisor + daemon-status see liveness.

### Daemon supervisor (`quikode/daemon.py`)

Wraps `quikode run` as a subprocess; restarts on crash with exponential
backoff (`cfg.daemon_backoff_schedule_s`, default `[60, 300, 1800]`).
Resets backoff if the child ran ≥ `cfg.daemon_min_run_for_backoff_reset_s`
(default 300s) before crashing. On clean exit (rc=0) the supervisor
exits 0. On SIGTERM / SIGINT, forwards SIGTERM to child, waits up to
30s, then SIGKILL. Writes `.quikode/daemon.pid`.

The supervisor never writes the heartbeat itself — that's the inner
orchestrator's job, so stale-heartbeat detection still works when the
child hangs.

### Worker (`quikode/worker.py`)

One per task. Drives the FSM through one full lifecycle:

- `_provision()` — git worktree + branch + dev container + postgres sidecar + `/tmp/qk-ready` poll
- `_plan()` — planner agent emits structured Plan JSON
- `_subtask_loop()` — for each subtask in topological order:
  - doer → checker → triage cycle (with progress-check agent at intervals)
  - on PASS: `git commit` (running pre-commit hooks per slice) + `git push`
  - parent-merge checkpoint: read `tasks.needs_parent_rebase`, run inline rebase if set
  - branch-divergence checkpoint: `_handle_branch_divergence_if_needed` detects FF / force-push / diverged states each subtask boundary and acts accordingly
  - opt-in slot yield: when `cfg.preempt_at_subtask_boundary=true` and a higher-priority candidate is queued, `_maybe_yield_at_boundary` surrenders the slot
- `_final_check_loop()` — whole-spec checker after all subtasks pass; on fail, `_invoke_fixup_planner` decomposes the failure into 1-5 `kind='fixup-final'` mini-subtasks driven through the same per-slice loop. Bounded by `cfg.fixup_max_rounds` (default 3); falls back to legacy whole-spec doer if planner fails.
- `_commit_push()` — final commit/push (no-op if all subtasks already pushed)
- `_open_pr()` — idempotent `gh pr create` (skipped if `pr_number`/`pr_url` already set; early draft PR opens after S-01)
- `_poll_pr_loop()` — gates on CI / mergeable / review state
- `run_review_response()` — entered when daemon detects new review threads. Uses fixup-planner decomposition (`kind='fixup-review'`) instead of monolithic doer call; bounded by `cfg.review_rounds_max` (default 15) before BLOCKing with "manual merge/close required". Resolve-thread is a deterministic GraphQL `resolveReviewThread` mutation, not an agent call.
- `run_ci_fix_response()` — entered when daemon detects CI failure post-AWAITING_MERGE. Same fixup-decomposition pattern (`kind='fixup-ci'`), pushes to existing branch, returns to AWAITING_MERGE.
- `run_rebase_to_main()` — entered for stacked children when parent merges; uses `git rebase --onto <parent_sha>`, recreates PR if base branch was deleted

Worker checkpoints for `tasks.needs_parent_rebase`: 5 sites read the
flag and run rebase + retarget inline before continuing. Sites:
per-subtask in `_subtask_loop`, entry to `_final_check_loop`,
`_commit_push`, `_open_pr`, each iteration of `_poll_pr_loop`.

### Agents (`quikode/agents/`)

Three CLI wrappers + ccusage:

| File | CLI | Headless flags that matter |
|---|---|---|
| `claude.py` | claude-code | `-p --permission-mode acceptEdits --add-dir /workspace` |
| `codex.py` | codex | `exec --dangerously-bypass-approvals-and-sandbox --color never --cd /workspace --skip-git-repo-check --output-last-message <tmp>` |
| `opencode.py` | opencode | `run --dangerously-skip-permissions --dir /workspace` |
| `ccusage.py` | (npm) | `ccusage` / `@ccusage/codex` / `@ccusage/opencode` — uniform token+cost |

Each agent invocation records into `agent_calls`: phase, cli, model,
rc, duration_s, tokens_input/output/cached, cost_usd. ccusage failures
fall through silently — costs are advisory.

### Docker env (`quikode/docker_env.py`)

Per-task lifecycle: dedicated network, postgres-16-alpine sidecar
(healthchecked), dev container with bind mounts. The sccache dir is
shared across all task containers (sccache uses file locks correctly).

Containers and networks carry a `qk_workspace=<8-hex>` label derived
from the workspace's state-dir path. `cleanup_all_quikode(cfg)` filters
by that label so two parallel quikode workspaces don't tear each other
down.

The dev image is built from `docker/Dockerfile` (rust+node+pg flavor)
or `docker/Dockerfile.python` (python flavor for fixture).

### Entrypoint (`docker/entrypoint.sh`)

Runs once per container start. Copies host auth dirs (RO mount at
`/host-auth/*`) into writable container locations, configures git
author + safe.directory, runs `gh auth setup-git`, writes a fallback
`/tmp/.git-credentials`, then touches `/tmp/qk-ready`. Orchestrator
polls for `/tmp/qk-ready` before invoking any agent.

### Store (`quikode/state.py`)

SQLite, WAL mode. Tables:

- `tasks` — one row per node; current state + branch + worktree_path + container_id + pr_number/url + retry counters + plan_text + last_error + parent_task_id + parent_pr_branch + needs_parent_rebase + needs_intent_review + auto_merged + pre_rebase_state + ...
- `state_log` — append-only transition log (from_state, to_state, ts, note)
- `artifacts` — agent outputs (planner_output / doer_output / checker_output / triage_output / ci_log / review_comments / progress_check_output / etc.)
- `agent_calls` — one row per agent invocation: phase, cli, model, rc, duration_s, tokens_*, cost_usd, subtask_id
- `subtasks` — planner-emitted structured subtasks; per-subtask state, retries, transient_retries, progress_check_count, flatline_count, commit_sha, pre_commit_failures
- `review_threads` — GraphQL node-id-keyed thread state; daemon's review-watcher diffs against this
- `progress_checks` — audit row per progress-check agent invocation
- `intent_reviews` — Phase B intent-drift reviews
- `container_stats` — periodic samples for resource tuning

`Store.recover_orphan_tasks()` runs on every `quikode run` startup;
resets active-state tasks to PENDING (with
`resume_from_existing_subtasks=1`) or AWAITING_MERGE per the recovery
table — see `runbook-incident-response.md`.

## State machine

```
PENDING → PROVISIONING → PLANNING ─[emits structured Plan JSON]─►
       │
       ▼
DOING_SUBTASK[i] ↔ CHECKING_SUBTASK[i] ↔ TRIAGING_SUBTASK[i]
       │ (per-subtask budget controlled by progress-check agent +
       │  cfg.subtask_hard_max_attempts)
       │ (on subtask PASS) → per-subtask COMMITTING/PUSHING (idempotent)
       │ (all subtasks done)
       ▼
FINAL_CHECKING ↔ TRIAGING ↔ FIXUP_PLANNING ↔ DOING_SUBTASK[fixup-final] (decomposed slices)
       │            (per-slice plan + commit; legacy whole-spec _do is fallback)
       │ (verdict PASS, ci pass)
       ▼
COMMITTING → PUSHING → PR_OPENING → POLLING_CI → AWAITING_MERGE
                                                       │
       ┌───────────────────────────────────────────────┼───────────────┐
   new review thread          mergeable=          intent-review     parent merged
   (via watcher)              CONFLICTING         flag set          (via _schedule_
       │                          │                   │              rebases_for_
       ▼                          ▼                   ▼              merged_parent)
RESPONDING_TO_REVIEW         REBASING            INTENT_REVIEWING        │
       │ (worker reuses          │                   │                   ▼
       │  worktree+branch+PR)    ▼              ┌────┴────┐         REBASING_TO_MAIN
       │                    CONFLICT_         NO_DRIFT   MINOR_DRIFT     │ (git rebase
       │                    RESOLVING            │       INTENT_         │  --onto
       │                         │            continue   CONFLICT        │  <parent_sha>;
       │                         ▼               │           │           │  recreate PR
       │                    force-push       (no-op)    REPLANNING /     │  if base
       │                    back to                     BLOCKED          │  branch deleted)
       │                    POLLING_CI                                   │
       └─────────► AWAITING_MERGE ◄──────────────────────────────────────┘
                          │
                          ▼ (auto-merge gating, opt-in: cfg.auto_merge_when_clean)
                       MERGED
```

Mid-flight parent-merge: when `tasks.needs_parent_rebase=1` is set on
an active child, the worker checkpoints (5 sites) handle rebase + PR
retarget **inline**, not via a state transition. The flag is cleared on
success.

Special transitions:
- `DOING → AWAITING_MERGE` (no-diff short-circuit) when the doer made no changes.
- `CHECKING → AWAITING_MERGE` directly when `checks_status == "none"` (fixture has no CI).
- `AWAITING_MERGE → RESPONDING_TO_REVIEW` for a CI failure post-merge-handoff (via `_schedule_ci_fix_response`); same fixup-decomposition pipeline as review-thread response.
- Any state → `FAILED` (uncaught exception)
- Any state → `ABORTED` (`quikode abort` per-task)
- Any state → `BLOCKED` (retry budget exhausted, or `cfg.review_rounds_max` exceeded with "manual merge/close required")

Terminal: MERGED, AWAITING_MERGE, BLOCKED, FAILED, ABORTED. The
`TERMINAL` set in `state.py` includes AWAITING_MERGE because no further
worker is scheduled — only the daemon's review-watcher / auto-merge can
move it forward.

## Stacked-diffs end-to-end

When `cfg.stacking_strategy != "off"` and a parent task is in
POLLING_CI/AWAITING_MERGE while a child becomes ready:

1. **Child branches off the parent's PR branch.** Orchestrator passes `parent_pr_branch=<parent_branch>` to the worker; worker creates the worktree off that branch, sets `tasks.parent_branch` and `tasks.parent_pr_branch`.
2. **Child opens a PR with `--base <parent_branch>`.** Stacks visually on GitHub.
3. **Parent merges.** GitHub squash-merges the parent into main and (per tanren policy) deletes the parent's remote branch with `--delete-branch`. The child's PR auto-closes (GitHub policy when base is deleted).
4. **Orchestrator detects the parent merge.** `_schedule_rebases_for_merged_parent` fires. For each child:
   - **Non-active children:** submits `_run_rebase_to_main_one` future.
   - **Active children (mid-doer/checker/etc.):** sets `tasks.needs_parent_rebase=1`. The worker reads this at its next checkpoint.
5. **Rebase worker (or worker checkpoint) runs:**
   - `parent_sha = git rev-parse <parent_branch>` — local ref persists post-deletion.
   - `git fetch origin main`
   - `git -c core.editor=true rebase --onto origin/main <parent_sha>` — drops parent's commits, replays only child-exclusive commits. Conflict resolver iterates if multi-conflict.
   - `git push --force-with-lease origin <branch>`
   - If the PR was auto-closed: **create a new PR** pointing at main (don't try to reopen the closed one). Update `tasks.pr_number` / `tasks.pr_url`.
   - Otherwise (parent merged via squash without delete-branch): `gh pr edit <pr> --base main`.
   - Clear `parent_branch`, `parent_pr_branch`, `needs_parent_rebase`.
6. **Child resumes its pre-rebase state** (captured in `tasks.pre_rebase_state` for non-active children; in-place for active children).

Capping knobs:
- `cfg.stacking_max_depth` (default 6) — max stack depth.
- `cfg.stacking_max_breadth_per_root` (default 12) — defensive cap on transitive children under a single root.

## Auto-merge interaction with review threads

When `cfg.auto_merge_when_clean=True`, the orchestrator's auto-merge
path **skips the merge** if any review threads on the PR are
unresolved. See `_attempt_auto_merge` in `orchestrator.py`. So a human
posting a thread effectively pauses auto-merge until that thread is
resolved.

The full auto-merge gate:
- Task state == AWAITING_MERGE
- PR state == OPEN
- PR mergeable == MERGEABLE
- All checks SUCCESS
- All review threads resolved (or no threads)
- Time since entering AWAITING_MERGE ≥ `cfg.auto_merge_min_age_s`

On success: `gh pr merge --squash --delete-branch`, set
`tasks.auto_merged=1`. The next poll tick picks up the MERGED state
from GitHub.

## Branching model

- Each fresh `quikode run` allocates a unique branch: `quikode/<task-id-slug>-<6hex>`. The 6-hex prevents collisions across runs whose remote branches we may not be able to delete.
- Worktree dir: `.quikode/worktrees/<task-id-slug>-<6hex>/`.
- On terminal-state teardown:
  - MERGED, ABORTED → worktree dir removed
  - AWAITING_MERGE, BLOCKED, FAILED → worktree dir kept on disk for inspection

Stacked children branch off the parent's branch (not main); see
"Stacked-diffs end-to-end" above.

## Configuration

`.quikode/config.toml` is per-workspace. Defaults are in
`quikode/config.py:Config`. The Pydantic field metadata (`ge=`/`le=`
bounds, descriptions) is the source of truth that the TUI settings
modal renders from — don't duplicate the field list elsewhere.

Key knob groups:
- Core paths: `repo_path`, `dag_path`, `image_tag`
- Orchestration: `max_parallel`, `base_branch`, `triage_budget_per_phase`
- Subtasks (v2 Phase 0): `subtask_doer_timeout_s`, `subtask_checker_timeout_s`
- Progress-check (v3 Phase A): `subtask_hard_max_attempts`, `subtask_progress_check_after`, `subtask_progress_check_every`, `subtask_flatline_block_count`, `subtask_transient_max_retries`, `pre_commit_runner`, `pre_commit_timeout_s`
- Review loop (v3 Phase B): `review_poll_interval_s`, `respond_to_bot_reviews`
- Auto-merge: `auto_merge_when_clean`, `auto_merge_min_age_s`
- Resources: `cpu_per_task`, `mem_per_task_gb`, `host_reserved_*`, `max_parallel_auto`, `container_stats_sample_seconds`
- Conflicts (Phase A): `conflict_auto_resolve`, `conflict_max_resolve_attempts`
- Intent (Phase B): `intent_check_on_dep_merge`, `intent_max_reviews_per_task`, `intent_max_replans`
- Stacking (Phase C): `stacking_strategy`, `stacking_max_depth`, `stacking_max_breadth_per_root`, `stacking_auto_rebase_on_parent_merge`
- Daemon (v3 Phase C): `daemon_heartbeat_staleness_s`, `daemon_min_run_for_backoff_reset_s`, `daemon_backoff_schedule_s`
- Fixup decomposition (2026-05-04): `fixup_max_rounds`, `review_rounds_max`, `review_response_extra_slots`
- Notifications (2026-05-04): `notify_settled_channel` ("none"/"ntfy"/"slack"/"both"), `notify_settled_after_s`, `notify_ntfy_url`, `notify_ntfy_topic`, `notify_slack_webhook_url`
- Slot scheduling (2026-05-04): `preempt_at_subtask_boundary`, `preempt_yield_threshold`
- Agent role assignments: `planner`, `doer`, `checker`, `triage`, `conflict_resolver`, `intent_reviewer`, `progress`

## Prompts

Templates in `prompts/` (Jinja2). Bundled with the package; per-workspace
overrides via `<workspace>/prompts/`. Files:

- `planner.md` — emits structured Plan JSON: subtasks with deps, files_to_touch, acceptance, interfaces (for BDD subtasks)
- `subtask-doer.md` — implements one subtask
- `subtask-checker.md` — verifies one subtask (acceptance bullets, real probes)
- `subtask-triage.md` — root-cause for a failing subtask
- `checker.md` — whole-spec checker (used both for `_check()` and final-check)
- `triage.md` — whole-spec triage; rendered with `phase="final_check"` for
  final-check failures, `phase="review"` for `run_review_response()` cycles
  (single template, conditional sections)
- `conflict-resolver.md` — resolves rebase conflicts
- `intent-reviewer.md` — checks intent drift after a dep merges
- `progress.md` — judges PROGRESSING / FLATLINED / TOO_EARLY for a struggling subtask

## Test harness

`tests/` — **581 pytest tests**, runs in <2s. Covers DAG loader,
state machine, schema parsing, agent invocation strings, token+cost
parsing, subtask schema, intent verdicts, stacking scheduler, resource
math, orphan recovery, per-subtask commit, pre-commit gate, progress
check, review-watcher, auto-merge gating, rebase --onto, daemon
supervisor, worker checkpoints.

## Image flavors

- `Dockerfile` — rust + node + pnpm + mold + sccache + gh + agent CLIs. ~1.2 GB. For tanren-style workspaces.
- `Dockerfile.python` — python 3 + uv + just + node + agent CLIs. ~700 MB. For the fixture and python-first workspaces.

Both share `entrypoint.sh`.

## Known invariants worth not breaking

- Worktree's `.git` file references `<host_repo>/.git/worktrees/<name>` by absolute path. The dev container mounts `<host_repo>/.git/` at the same path so git can resolve. **Don't move this mount.**
- `agent_calls` schema is additive-only; tests assert column presence. Don't drop columns.
- The orchestrator on startup runs `cleanup_all_quikode()` filtered by `qk_workspace` label — so parallel quikode workspaces are fine, but two orchestrator processes in the same workspace are blocked by `orchestrator.pid` freshness check.
- Branch deletion in `quikode reset` is a `git push --delete origin <ref>`. Without `check=True`, failures (denied permission, branch protection) are silent. Don't add `check=True` without a fallback path.
- `Store.recover_orphan_tasks` runs before the orchestrator constructor on every `quikode run`. Adding a new active state to the FSM **must** also add a recovery rule, or tasks in that state become orphans on crash.
- Adding a new column to a schema table requires both `SCHEMA` (for fresh DBs) and a `_migrate()` entry (for older DBs). Migrations are idempotent ALTER TABLEs.
