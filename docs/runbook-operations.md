# runbook — operations

Daily ops manual. For breakage, see `runbook-incident-response.md`. For
tanren-specific gotchas, see `runbook-tanren-watch-points.md`.

## First-time setup

```bash
# Install (editable so prompt + code edits land immediately)
cd /home/trevor/github/quikode
uv tool install --editable .

# Initialize a workspace pointing at a target repo + DAG
mkdir -p ~/github/quikode-runs/<workspace-name> && cd $_
quikode init --repo ../tanren --dag ../tanren/docs/roadmap/dag.json

# Verify the environment
quikode doctor

# Build the dev container (tanren flavor: rust+node+pg+agent CLIs)
quikode build-image --flavor tanren
# or for python projects (fixture):
quikode build-image --flavor python
```

`quikode doctor` checks docker, gh auth, agent CLIs (claude / codex /
opencode) on the host, presence of the dev image, and config paths. Fix
anything red before kicking off a real run.

Edit `.quikode/config.toml` to tune knobs. Defaults are in
`quikode/config.py:Config`. Common overrides:

| Knob | Default | When to change |
|---|---|---|
| `max_parallel` | 3 | Lower if host can't handle parallel `cargo build` |
| `max_parallel_auto` | false | `true` to compute from host headroom on `run` |
| `cpu_per_task` / `mem_per_task_gb` | 4 / 12 | Tune to your hardware |
| `auto_merge_when_clean` | false | `true` for trusted workspaces (fixture; never tanren without review) |
| `stacking_strategy` | "off" | "within-milestone" or "aggressive" once parallel runs are stable |
| `subtask_hard_max_attempts` | 30 | Lower in cost-sensitive runs |

## Starting a tanren run

Recommended path (resilient to crashes):

```bash
cd ~/github/quikode-runs/tanren

# Optional: see what would run
quikode plan
quikode briefing       # cost so far, blocked tasks, recent merges

# Kick off the daemon supervisor in foreground
quikode daemon start --max-parallel 3 --retry-failed
```

`--retry-failed` resets BLOCKED / FAILED / ABORTED tasks in scope to
PENDING on startup, useful for "auto-retry overnight" loops.

For a single-task drive-by (no supervisor):

```bash
quikode run --only R-0001 --max-parallel 1 --retry-failed
```

For a fixture smoke test:

```bash
cd ~/github/quikode-runs/fixture
quikode reset --yes --close-prs
quikode run --max-parallel 1
# or assert with timeout
quikode dev-test
```

**Validation runs require `--max-parallel >= 2`.** Stacked diffs,
sibling-CONFLICTING auto-rebase, mid-flight parent-merge handling, and
auto-merge are only exercised under parallelism. If you're validating
quikode changes against the fixture, use `--max-parallel 3`. The
default `quikode dev-test` smoke-tests T-001 only — useful for quickly
confirming the loop runs end-to-end, but it doesn't cover the full v3
surface.

### Fixture full-E2E validation

Use this when validating a quikode code change against the full v3
surface (stacked diffs, sibling conflicts, review-response cycle,
auto-rebase on parent merge, MERGED detection, etc.). The fixture's
4-task DAG exercises the same loop tanren does, just smaller and
faster.

**When to use:** before merging a non-trivial change to `worker.py`,
`orchestrator.py`, `worktree.py`, or any state/transition logic. Also
the right vehicle for "have we regressed since X" checks.

**Workflow:** identical to a tanren run.

```bash
cd ~/github/quikode-runs/fixture
quikode reset --yes --close-prs
# Revert fixture's main back to baseline first — see "Fixture
# between-run reset" in Routine maintenance.
quikode daemon start --max-parallel 3
```

**Required interaction points:**

1. After T-001's PR opens, post an inline review comment on a
   specific line (see "Reviewing PRs" for how — bare review-body
   comments are ignored). This exercises the response cycle.
2. Once T-002 reaches AWAITING_MERGE, merge it manually via the
   GitHub UI. This forces a sibling-rebase on T-001 (if T-001 is
   stacked under T-002 or has a mergeability conflict) and on T-003
   (which depends on T-002).
3. Watch the daemon's stacked-rebase handling for T-003.

**Expected end state:** 4/4 tasks merged with no manual intervention
beyond the review comment + manual merges above. Approximate cost: a
few dollars in agent calls. Approximate runtime: 15-30 minutes.

**Key warning:** don't use `--max-parallel 1` for validation — it
serializes execution and skips most v3 paths. Stacked diffs alone
require parallelism, since by definition they involve a child task
opened against an unmerged parent's branch.

### When to enable `auto_merge_when_clean`

Only for trusted task types where merges don't need a human review pass.
The fixture qualifies; tanren generally does not. Daemon merges only
when **all** of:
- PR state is OPEN, MERGEABLE, all checks SUCCESS
- All review threads marked resolved
- Task has been in AWAITING_MERGE for at least `auto_merge_min_age_s` (default 60s)

See `orchestrator.py:_attempt_auto_merge` for the exact gating.

## Monitoring an in-flight run

| What you want to know | Command | Notes |
|---|---|---|
| Live dashboard | `quikode tui` | Press `g` for DAG viewer; `/help` for slash commands |
| One-shot snapshot | `quikode briefing` | In-flight, awaiting, blocked, recent transitions, cost-so-far, recent merges |
| Per-task detail | `quikode show <id>` | State timeline, latest planner/checker/triage artifacts, agent costs |
| Subtask breakdown | `quikode subtasks <id>` | Per-subtask state + retries + last error |
| Live status table | `quikode watch` | Refreshing; `--active` to filter to non-terminal |
| DAG progress | `quikode dag-stats --by milestone` | Per-milestone merged/awaiting/active/blocked/pending |
| Daemon liveness | `quikode daemon status` | Exit 0 alive+fresh, 1 down, 2 stale heartbeat. `--json` for scripting. |
| Resource caps + headroom | `quikode resources` | What was computed, what the host can support. For Phase 5 scaling, set `cfg.max_parallel_auto = true` to let quikode compute the safe ceiling from this same calculation on each `run`. |
| Disk usage | `quikode disk-usage` | sccache, worktrees, logs, SQLite |

### Where logs live

| File | What it carries |
|---|---|
| `.quikode/logs/<task-id>.log` | Per-task: every docker exec, agent stdout/stderr, transitions |
| `.quikode/logs/daemon.log` | Daemon supervisor: spawn / restart / backoff / signal events |
| `.quikode/quikode.db` | SQLite source of truth — `state_log`, `agent_calls`, `artifacts`, `subtasks`, `review_threads`, `progress_checks`, `intent_reviews`, `container_stats` |
| `.quikode/orchestrator.heartbeat` | JSON blob written every poll tick by the orchestrator |
| `.quikode/orchestrator.pid` / `.quikode/daemon.pid` | PIDs (with start ts) for liveness checks |

`quikode tail <id>` tails a task log; `quikode logs <id>` prints its path.

## Reviewing PRs

The human's job in the loop:

1. **Inspect per-subtask commits.** Each subtask commits independently (`worker.py:_subtask_loop`). The PR diff is reviewable as a stack of small slices, not one monolithic change.
2. **Post review-thread comments.** Only **review-thread** comments via the GitHub review system trigger the response cycle. Plain issue comments are intentionally ignored (see `cfg.respond_to_bot_reviews` and `_classify_threads` in `orchestrator.py`). Post comments **inline on a specific line** for the response cycle to fire. The watcher polls graphql `reviewThreads` which only includes line-anchored threads, not bare review-body comments. Use one of:
   ```bash
   gh api -X POST /repos/<owner>/<repo>/pulls/<n>/comments \
     -f body='your comment' \
     -f commit_id=$(gh pr view <n> --json headRefOid --jq .headRefOid) \
     -f path='path/to/file.ext' -F line=<line> -f side=RIGHT
   ```
   Or in the GitHub UI: 'Start a review' → click a `+` on a specific line → 'Add a single comment' or 'Start a review' → 'Submit review'. A bare `gh pr review --comment --body '...'` (no inline anchor) creates only a review-body comment, NOT a thread, and will be ignored by the watcher.
3. **Wait for the daemon's response.** Within `cfg.review_poll_interval_s` (default 60s), the watcher detects the new thread, sets the task to RESPONDING_TO_REVIEW, and dispatches a worker on the existing worktree/branch/PR. The worker addresses the threads and pushes new commits to the same branch.
4. **Merge** (or rely on auto-merge). For tanren, merge manually after review. The daemon detects the merge, schedules rebases for any stacked children, and removes the worktree.

To pull a PR branch locally for hands-on testing without disturbing the daemon's worktree:

```bash
quikode demo <task-id>             # clones (or fetches) into <repo>-demo/
quikode demo <task-id> --clean     # nuke + re-clone
```

## Stopping cleanly

```bash
quikode daemon stop --timeout-s 30
```

Sends SIGTERM to the supervisor, which forwards SIGTERM to the inner
`quikode run`. The orchestrator's stop event triggers; in-flight workers
finish their current step and exit. After `timeout_s`, SIGKILL.

For a non-daemon `quikode run`, just Ctrl-C — the same handler fires.

After a stop, the next `quikode run` (or `daemon start`) auto-recovers
orphan tasks via `Store.recover_orphan_tasks()`.

## Routine maintenance

| Command | What it does |
|---|---|
| `quikode reset [--close-prs]` | Tear down everything: containers, worktrees, branches, optionally close+delete remote PR branches. Drops state. Use between fixture smoke runs. |
| `quikode prune [--sccache-max-gb N]` | Trim sccache + remove worktrees of terminal-state tasks (MERGED, ABORTED). Worktrees of AWAITING_MERGE / BLOCKED / FAILED tasks are kept on disk for inspection. |
| `quikode clean-containers` | Remove stranded `qk-*` containers (no state change). Workspace-scoped via `qk_workspace=<8-hex>` label; doesn't touch other workspaces. |
| `quikode disk-usage` | What quikode is using on disk |
| `quikode mark-merged <id ...>` | Manually mark already-complete tasks as MERGED so dependents unblock |
| `quikode retry <id>` | Reset BLOCKED/FAILED/ABORTED → PENDING (cleans worktree). Worker re-plans from scratch. |
| `quikode resume <id>` | Re-pend with `resume_from_existing_subtasks=1` — worker reuses the stored Plan + per-subtask state, picks up at the first non-DONE subtask. |

`reset` vs `retry` vs `resume`:
- `reset` — workspace-wide nuke
- `retry` — single task, fresh plan
- `resume` — single task, keep plan + finished subtasks

### Fixture between-run reset

The fixture's GitHub `main` accumulates merges across runs. For fresh
validation runs, `main` needs to be reverted to the baseline (just the
`/health` endpoint) so all four tasks have something to do — otherwise
T-001..T-004 short-circuit as no-ops because their work is already
present.

```bash
cd ~/github/quikode-fixture
git pull --ff-only
# Inspect recent commits to find which T-* commits to revert.
git log --oneline -10
# Revert all T-001..T-004 commits since the baseline (most recent first).
git revert --no-edit <sha1> <sha2> ...
git push
```

One-liner that detects and reverts all `T-00[1-4]:` titled commits in
one shot. **Anchor the regex on `^<sha> T-00X: `** to avoid matching
`Revert "T-00X:..."` or `Reapply "T-00X:..."` commits — those start
with the prefix words, and a greedy match would chain into endless
revert-of-revert loops:

```bash
SHAS=$(git log --oneline | grep -E '^[a-f0-9]+ T-00[1-4]: ' | head -4 | awk '{print $1}')
git revert --no-edit $SHAS && git push
```

After this, `quikode reset --yes --close-prs` in the workspace dir
gives you a clean slate for the next run.
