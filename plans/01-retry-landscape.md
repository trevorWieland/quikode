# Retry & failure-mode landscape (snapshot before tanren overnight run)

Map of every place tanren overnight could enter an unproductive retry loop or hit a permanent
failure that's actually recoverable. Used to prioritize the other plans/*.md.

## Subtask loop
- **Hard-max attempts cap** — `subtasks.py:206–239`. Knob `subtask_hard_max_attempts`
  (config currently 50). Exhaustion → `BLOCKED`. Recovery is `qk resume` after manual
  fix. Transient rc=124 doesn't bump the counter — but mis-classification (slow real
  failure looking transient) silently burns time.
- **Progress flatline blocking** — `subtask_progress.py:20–28`, `subtasks.py:297–307`.
  Knobs `subtask_progress_check_after`, `_every`, `_flatline_block_count` (3). Three
  consecutive FLATLINE verdicts → BLOCKED. Race window on `flatline_count` if multiple
  triage cycles overlap. See plan 06.
- **Pre-commit gate** — `subtask_completion.py:100–112`. Single attempt, hangs to
  `pre_commit_timeout_s`; on timeout each subtask attempt is consumed.

## Fixup / pre-PR audit
- **Fixup planner transient retry** — `pre_pr.py:234–275`. `fixup_planner_retries_on_transient`
  (default 2) only retries rc=124. Real timeout retries the same prompt with no backoff.
- **Pre-PR audit cycles** — `pre_pr.py:386–562`. `pre_pr_audit_max_cycles` (3). Per-stage
  failures (rubric/standards/behavior agent crash) silently lose their signal — no per-stage
  retry budget.

## Post-PR
- **CI fix budget** — `pr_lifecycle.py:200–223`. `triage_budget_per_phase` (10 in tanren
  config) caps `_run_fixup_round(kind="fixup-ci")`.
- **Intent review budget** — `pr_lifecycle.py:244–250`. `intent_max_reviews_per_task` (10).
- **Replan budget on INTENT_CONFLICT** — `pr_lifecycle.py:335–432`. `intent_max_replans` (5).
- **Review rounds** — `review_watch.py`, knob `review_rounds_max`. Fix budget exhaustion
  silently flips back to `PENDING_CI`, no error state visible. See plan 04.

## Rebase / conflict
- **Conflict iteration cap** — `rebase_conflicts.py:71–88`. **Hardcoded to 6**, ignores
  `conflict_max_resolve_attempts`. See plan 03.
- **Force-push** — `rebase_conflicts.py:118–128`, `rebase_branch.py:179–190`. Single
  attempt; transient network blip → `BLOCKED`. See plan 03.

## Provisioning / docker
- **Container provision** — `task_worker.py:317–336`. Single try, no retry. Crash → task
  goes `FAILED`. Common transient is "postgres not healthy in 60s".
- **Worktree reconstruction** — `task_worker.py:290–307`. Resume after worktree cleanup
  can orphan tasks (worktree path cleared, branch row stuck).

## Supervision
- **Stall detector silently resets review-response** — `supervision.py:106–150`. After
  `stall_warn_seconds` (1800) of quiet ADDRESSING_FEEDBACK with zero agent_call rows,
  force-cancels future and resets to PENDING_CI. **No state_log entry**, evidence of
  the underlying crash gets erased. See plan 04.
- **Stalled DOING_SUBTASK warning** — `supervision.py:84–104`. Logs only, no recovery.
  A wedged subprocess waits forever.

## Network
- No backoff anywhere. Every git/gh/github_graphql call uses `timeout=60, check=False`,
  emits a warning on rc!=0, then the next worker tick retries the same call immediately.
  Rate-limit (429) loop: poll every 10s, hit 429, retry 10s later, indefinitely. See
  plan 02.

## Tanren-config-relevant retry budgets (current values)
- `subtask_hard_max_attempts = 50` — generous
- `subtask_progress_check_after = 6`, `_every = 3`, `_flatline_block_count = 3`
- `subtask_transient_max_retries = 10`
- `triage_budget_per_phase = 10`
- `fixup_max_rounds = 3`
- `conflict.auto_resolve = true`, `max_resolve_attempts = 5` (but the loop is hardcoded
  to 6, so the config knob does nothing — see plan 03).
- `intent.max_reviews_per_task = 10`, `max_replans = 5`
- `stall_warn_seconds = 1800`, `subtask_doer_timeout_s = 1200`

## Priorities (for follow-up plans)
1. Plan 02 — exponential backoff on rate-limit / network errors. Highest leverage,
   touches review-poll + push hot paths.
2. Plan 04 — supervisor reset must emit a structured state_log row. Loss-of-evidence
   bug; cheap to fix.
3. Plan 03 — wire `conflict_max_resolve_attempts` to the actual loop, add force-push
   retry-with-backoff.
4. Plan 05 — poisoned-worktree detection: auto-wipe and re-plan when N consecutive
   subtasks fail in the same file with the same checker root cause.
5. Plan 06 — progress-check signal hardening: distinguish "FLATLINE because the agent
   keeps trying the same wrong thing" from "FLATLINE because the agent never reached
   the affected file."
