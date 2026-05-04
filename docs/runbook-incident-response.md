# runbook — incident response

What to do when things break. For day-to-day ops, see
`runbook-operations.md`.

Format: **symptom → first-look → recovery options**, ordered roughly
from most-common to least-common.

## Symptom: task BLOCKED

The task hit its retry budget. Always recoverable; the question is *how*.

**First look:**

```bash
quikode show <id>             # state timeline + latest artifacts + costs
quikode unblock <id>          # prints worktree path, branch, PR, last error, next-step hints (no state change)
quikode subtasks <id>         # which subtask blocked, on what
```

Read the last 3 transitions in the state timeline, the resolver / triage
agent's last message, and the `last_error` field. Decide:

| Want | Recovery |
|---|---|
| Push a fix from your laptop | `cd <worktree>; <edit>; git commit; git push`. Daemon detects new commits and resumes from the failing subtask via the standard flow. |
| Guide the agent | Post a review-thread comment via `gh pr review --comment --body "..."`. Watcher picks it up next tick. |
| Fix locally and re-pend | `quikode unblock <id>` (read-only context), do the work, then `quikode resume <id>` from the workspace dir. Reuses Plan + finished subtasks. |
| Restart the task from scratch | `quikode retry <id>` — wipes worktree, re-plans, fresh budget. |

`unblock --edit` launches `$EDITOR` on the worktree path if you want a
keystroke shortcut.

## Symptom: daemon crashed / heartbeat stale

```bash
quikode daemon status         # exit 0 alive+fresh, 1 down, 2 stale
```

The heartbeat is written by the inner `quikode run`, not the supervisor.
Stale heartbeat with the supervisor still alive means the inner
orchestrator is hung but the supervisor hasn't noticed yet (it only
restarts on actual exit).

| Output | Meaning | Action |
|---|---|---|
| `daemon alive` + `heartbeat fresh` | Healthy. | None. |
| `daemon alive` + `heartbeat STALE` | Inner orchestrator hung. | `tail -f .quikode/logs/daemon.log` to see what's happening; if truly stuck, `quikode daemon stop` then `daemon start` for a forced restart. |
| `daemon not running` | Supervisor died (rare). | Check `.quikode/logs/daemon.log` for cause. Restart with `quikode daemon start`. |

Supervisor crashes themselves are rare. The supervisor restarts the
*child* on crash with backoff (`60s → 5m → 30m`, capped). If the inner
`quikode run` ran for `cfg.daemon_min_run_for_backoff_reset_s` (default
300s) before crashing, backoff resets to the first entry — so it's
self-healing for transient crashes but doesn't tight-loop.

## Symptom: orphan tasks after restart

Orphan recovery runs automatically on every `quikode run` startup
(`state.py:Store.recover_orphan_tasks`). Tasks left in active states get
reset:

| State at crash | Recovery state |
|---|---|
| `provisioning` / `planning` (no plan_text) | `pending` (clean reset) |
| `doing_subtask` / `checking_subtask` / `triaging_subtask` / `final_checking` / `committing` / `pushing` | `pending` + `resume_from_existing_subtasks=1` |
| `pr_opening` / `polling_ci` (with pr_number) | `awaiting_merge` |
| `responding_to_review` | `awaiting_merge` (watcher re-detects threads) |
| `rebasing_to_main` / `conflict_resolving` | `awaiting_merge` if PR exists, else `pending` + resume |
| `intent_reviewing` | `awaiting_merge` if PR exists, else `pending` + resume |
| Terminal (`merged` / `blocked` / `failed` / `aborted` / `awaiting_merge`) | left alone |

The recovered transition is logged with note `orphan recovery`. If a
task remains stuck, `quikode show <id>` reveals the recovery transition
and current state — pick from the BLOCKED options above.

## Symptom: review comment ignored

Only **review-thread** comments are picked up. The watcher polls via
`github_graphql.get_review_threads`, which queries the GraphQL
`reviewThreads` connection — plain `issueComments` are explicitly
filtered out (intentional; otherwise every CI bot ping triggers a
worker).

Recovery: re-post the same body inside a review:

```bash
gh pr review <pr-number> --comment --body "..."
```

Or in the UI: "Files changed" → "Start a review" → comment on a line →
"Submit review". Re-posting from "Conversation" tab as a regular comment
won't trigger the watcher.

`cfg.respond_to_bot_reviews` (default `true`) controls whether
bot-authored review threads (e.g., chatgpt-codex-connector) are
addressed. Set to `false` to ignore bot reviews and address only
human-authored threads.

## Symptom: PR auto-closed by GitHub (base branch deleted)

Happens when the parent task merges with `--delete-branch` (squash-merge
with branch deletion is GitHub's default for tanren). Stacked children
pointing at the deleted branch get auto-closed.

Recovery is automatic. The rebase worker detects the deleted base
branch, runs `git rebase --onto origin/main <parent_sha>`, **creates a
fresh PR** pointing at main (not just `gh pr edit --base main`, since
the old PR is closed), and updates `tasks.pr_number` / `tasks.pr_url`.
See `worker.py:run_rebase_to_main`.

If you see an old closed PR + a new open PR for the same task, that's
the auto-recreation path working as designed. The `state_log` will show
a `REBASING_TO_MAIN → AWAITING_MERGE` transition with a note.

## Symptom: rebase loop / repeated rebase storms

`_schedule_rebases_for_merged_parent` only triggers a child rebase when
the child's PR is `CONFLICTING` or its base branch is deleted. Other
parent state changes do **not** schedule rebases. If you see repeats,
something is regenerating the trigger condition — most likely a flaky
mergeable check or repeated parent re-merging (shouldn't happen).

Look at `state_log` notes for the trigger reason on each
`REBASING_TO_MAIN` entry. If you see two within seconds of each other,
that's the known coalescing gap (see `future-work.md`).

If a single rebase is itself failing repeatedly, the conflict resolver
may be stuck on a semantic conflict. `quikode unblock <id>` and resolve
manually.

## Symptom: stuck in RESPONDING_TO_REVIEW

The agent is mid-fixup. The loop is unbounded by design — humans drive
cadence. `quikode tail <id>` to peek at what the agent is doing. If
truly stuck (agent CLI hung):

```bash
quikode abort <id>            # tear down container; mark ABORTED
quikode retry <id>            # back to PENDING for a fresh attempt
```

A stuck agent CLI is usually visible in `quikode show <id>` as no new
agent_calls rows for many minutes against a still-active state.

## Symptom: subtask retrying without convergence

The task is in `DOING_SUBTASK` / `CHECKING_SUBTASK` / `TRIAGING_SUBTASK`,
attempt count is climbing past 4-5, and costs are mounting. The progress
check should catch flatline in theory; in practice it can return
`PROGRESSING` / `UNCERTAIN` even on a runaway loop where the doer is
making cosmetically-different edits each iteration (different SHAs,
identical content).

**First look:**

```bash
quikode show <id>             # state timeline + per-attempt root_cause
quikode subtasks <id>         # which subtask is looping, on what
```

Look at the recent transitions for a pattern of `S-NN attempt N
failed` lines. If the failure root causes are repeating verbatim — same
checker complaint, same triage notes — the agent's heuristic is failing
in a way that the progress checker doesn't catch.

**Check the progress agent's verdicts:**

```bash
sqlite3 .quikode/quikode.db \
  "SELECT * FROM progress_checks WHERE task_id = '<id>' ORDER BY ts DESC LIMIT 5;"
```

If verdicts are PROGRESSING/UNCERTAIN despite repeated identical root
causes, this is the "rotating-SHA loop" failure mode (Bug 2 from the
2026-05-03 validation findings; the immediate trigger was fixed in
`worktree.py`, but the underlying class of failure can recur if the doer
finds another way to make superficial progress without actually
addressing the checker's complaint).

**Recovery:**

```bash
quikode daemon stop
quikode retry <id>            # full reset; fresh subtask attempt
quikode daemon start --max-parallel 3
```

`retry` is preferred over `resume` here — `resume` carries forward the
existing subtask state, which is exactly what's stuck. `retry` wipes
the worktree, re-plans, and gives the agent a fresh budget. If the
loop reproduces immediately on retry, that's a deterministic
quikode-side bug; capture the task log + state and file an issue.

## Symptom: worktree missing or corrupted mid-run

The task's `worktree_path` row points to a directory that doesn't exist
on disk; the worker crashes on its first git command. Rare, but
observed in Run 1 of the 2026-05-03 validation when an external process
removed the worktree mid-flight.

**First look:**

```bash
ls .quikode/worktrees/                        # see what's actually there
quikode show <id>                             # see the row's stored worktree_path
```

If the worktree directory listed for the task is missing, that's the
condition.

**Recovery:** `quikode retry <id>` is the safest path — it wipes any
stale state and re-plans from scratch. The orphan-recovery on the next
daemon restart will also handle this case if the task is still in an
active state, but `retry` gives you control over timing.

```bash
quikode retry <id>
```

If the task was AWAITING_MERGE with a real PR, you can sometimes
preserve that PR by re-creating the worktree manually from the remote
branch (`git worktree add <path> <branch>`) and then `quikode resume
<id>` — but this is a power-user move; `retry` is fine 95% of the time.

## Symptom: ccusage shows wrong cost

`quikode/agents/ccusage.py` uses snapshot-delta (read JSONL state before
and after each agent invocation). Costs are advisory and never block.

If a known cost looks wrong:
1. Check `agent_calls.cost_usd` directly: `sqlite3 .quikode/quikode.db 'select task_id, phase, cost_usd from agent_calls order by ts desc limit 20'`.
2. ccusage failures fall through to `None`; the column is missing for that call. The total in `quikode briefing` is the sum of non-NULL `cost_usd` rows.
3. claude-code's stream-json envelope is preferred over ccusage when present (see the docstring in `ccusage.py`).

## Symptom: task FAILED (vs BLOCKED)

`FAILED` is unrecoverable crash — usually a code bug in quikode, an
uncaught exception in the worker, or a docker daemon failure.

`BLOCKED` is the orderly exit when retry budgets are exhausted.

Recovery for FAILED:

```bash
quikode show <id>             # check last_error + state timeline
quikode tail <id>             # task log will have the traceback
# Fix the cause (file an issue if it's a quikode bug), then:
quikode retry <id>            # back to PENDING
```

If the same task keeps FAILING, it's a deterministic bug. Capture the
log and the SQLite state before retrying.

## Symptom: container stranded after a crash

`quikode run` startup runs `cleanup_all_quikode(cfg)` which kills every
`qk-*` container labeled `qk_workspace=<this-workspace's-8hex>`. So a
fresh `quikode run` cleans up. For mid-flight cleanup without
restarting:

```bash
quikode clean-containers      # workspace-scoped; safe with parallel workspaces
```

The `qk_workspace` label is derived from the workspace's state-dir path,
so this never touches another workspace's containers.

## Symptom: `quikode reset` failed to delete remote branch

`reset --close-prs` runs `git push --delete origin <ref>` per branch.
If the user-token doesn't have repo:delete (or branch protection
denies), the call fails silently — `subprocess.run` without
`check=True`. Don't add `check=True` without a fallback; just delete
the branch by hand on GitHub if needed.

## Symptom: two orchestrators trying to run in the same workspace

`quikode run` writes `.quikode/orchestrator.pid` and refuses to start
if another fresh PID is on disk (within 60s). The daemon supervisor has
its own `daemon.pid`. So:

- Running `quikode daemon start` while another `daemon start` is alive → the second one prints "daemon already running" and exits 1.
- Running `quikode run` while a daemon is alive → the second one detects the fresh `orchestrator.pid` and refuses.

If the PID file is stale (older than 60s, no live process), the new
invocation proceeds and overwrites it. To force-clear: `rm
.quikode/orchestrator.pid .quikode/daemon.pid` (only when truly stale).
