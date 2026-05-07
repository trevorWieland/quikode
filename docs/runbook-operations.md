# Operations Runbook

## Start

```bash
quikode doctor
quikode seed-from-base
quikode daemon start --detach --max-parallel <N>
quikode daemon status
```

Use `quikode run` for foreground debugging.

## Monitor

```bash
quikode status
quikode briefing
quikode show <task-id>
quikode subtasks <task-id>
quikode tail <task-id>
```

For long-running watch loops, poll the SQLite `state_log` directly (a small Python poller); avoid `tail -F daemon.log | grep` (flaky on log rotation, ANSI escapes, pipe buffering — the WSL filesystem in particular drops the file handle on daemon restart). See `orientation.md` §6.

## Post-PR state meanings

Plan 28 streamlined the post-PR slice to three states:

- **`pending_ci`** — PR open, CI running. Daemon polls only `gh pr view` for CI rollup.
- **`awaiting_review`** — CI is green; daemon polls formal GitHub Reviews. The "needs human" state.
- **`addressing_feedback`** — daemon detected CI failure or a non-bot `CHANGES_REQUESTED` review; worker is running the fixup-decomposition path with bundled context.

Plan 30 adds a derived signal on top of `awaiting_review`:

- **review-ready-settled** — task has been in `awaiting_review` for ≥ `cfg.review_ready_settle_s` (default 900s = 15 min). Triggers two things: (1) ntfy push to `cfg.notify_ntfy_topic`; (2) stacked-diff dependents whose only un-met dep is this task become eligible to start.

(Retired by plan 28: `merge_ready`, `triaging_feedback`. Bot/AI-reviewer line comments are no longer polling triggers — they bundle as context for the fixup planner when a real review fires.)

## Stacking-gate behavior at startup

With `cfg.stacking_strategy = "aggressive"` and `cfg.stacking_readiness = "settled"`, expect a **ramp** rather than instant slot saturation:

- The first wave of in-flight tasks is whatever subset of the DAG has all-deps-merged (the "primary tier"). On a fresh seed this is the depth-1 children of seed-merged nodes.
- All other tasks are stacked candidates, gated on their parent reaching `awaiting_review` for ≥ 15 min. Until the first parent settles, the scheduler cannot dispatch them — slots will sit idle.
- Once the first wave settles, ~3–5 dependents per parent become eligible and the funnel widens. Steady state quickly approaches `max_parallel`.

This is by design. Plan 30's safety property: every stacked child starts from a CI-green base the operator could have reviewed. The cost is one cold-start cycle of partial slot fill.

If you've **just tightened the stacking gate** (e.g. flipped `stacking_readiness` from `"speculative"` to `"settled"`, or bumped `stacking_strategy`), see `runbook-incident-response.md` § "Fruit-of-rotten-tree wipe" — pre-tightening worktrees were forked under the looser gate and likely need cleaning.

## Interventions

`retry <id>` — wipes worktree + branch + subtask rows; planner re-plans from scratch. Requires task in BLOCKED/FAILED/ABORTED. For PENDING tasks, `abort` first.

`rewind <id> <subtask>` — surgical: rewinds branch + worktree to predecessor's commit; resets target + every topo-after subtask to PENDING; preserves prior subtasks' commits. Requires BLOCKED/FAILED. `--dry-run` first.

`resume <id>` — drops a BLOCKED/FAILED task back to PENDING with a resume marker; preserves worktree, branch, subtask rows.

`reset-retries <id> [<subtask>]` — zeroes retry counters on BLOCKED subtasks without discarding committed work. Pair with `resume`.

`abort <id>` — marks a task ABORTED and tears down its container.

`unblock <id>` — prints forensics + local context for a blocked task.

`mark-merged <id>` — marks already-landed upstream work as merged.

For decision rules on which intervention to use, see `orientation.md` §3 (Resolving blockers — the intervention decision framework).

## ntfy review-ready notifications

Set `notify_ntfy_topic` in `.quikode/config.toml` to a secret topic string and install the ntfy.sh app on your phone (subscribe to the same topic). When any task reaches review-ready-settled, you'll get a push: title `"R-NNNN: ready for review"`, body with the task title + settled minutes + review round count + PR URL, click → PR.

Test the wiring by waiting for the first task to settle, or — for synthetic verification — flip `cfg.review_ready_settle_s` to 0 in config (the next poll tick will fire ntfy on every AWAITING_REVIEW task it sees), then revert.

## Overnight Checklist

Run the validation ladder, initialize a fresh workspace, run `seed-from-base`, confirm already-landed nodes are `merged`, then start the daemon with the intended `--max-parallel`. Verify ntfy is wired (config has `notify_ntfy_topic`, app subscribed). Expect a slot-fill ramp over the first ~30 min as the first wave of primaries reaches review-ready-settled.
