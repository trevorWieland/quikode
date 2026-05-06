# Plan 08 — tighten retry budgets to fail-fast on real stuckness

## Numbers from tanren config (May 2026)

| Knob | Value | Worst-case time before BLOCKED |
|---|---|---|
| `subtask_hard_max_attempts` | 50 | 50 × ~10 min = ~8 hours |
| `subtask_progress_check_after` | 6 | first check at attempt 6 |
| `subtask_progress_check_every` | 3 | next at 9, 12, 15... |
| `subtask_flatline_block_count` | 3 | block at attempt 12 if all flatlined |
| `subtask_transient_max_retries` | 10 | another 10 free retries on top |
| `triage_budget_per_phase` | 10 | 10 fixup-ci rounds |
| `intent.max_replans` | 5 | 5 full replans |
| `fixup_max_rounds` | 3 | reasonable |

The 50 attempts limit is a safety net only useful when the progress detector breaks.
But if the progress detector is working, it will fire well before. The other dial that
matters is `subtask_doer_timeout_s = 1200` — a single attempt can run 20 minutes.

So: hard_max=50 × 1200s = **16+ hours** for one stuck subtask. We have 7 parallel slots,
so a single stuck task can hold 1/7th of capacity for 16 hours.

## Proposal

Cut `subtask_hard_max_attempts` to **20**. If the progress detector hasn't blocked by
attempt 12, three more cycles is plenty to confirm. After 20, we're definitively
burning cycles.

Cut `subtask_doer_timeout_s` to **900** (15 min). The current 1200s is generous; we
observed planning phases averaging ~4 min, and doer phases similar. A doer that runs
for 15 minutes without finishing is almost certainly looping; the next attempt can
re-anchor.

Keep `subtask_transient_max_retries` at 10 — transients are real and uncorrelated with
"stuck"-ness.

Tighten `intent.max_replans` from 5 to **3**. A task that needs three full replans is
not converging; the planner is working with bad inputs (probably a stale parent diff).
Better to BLOCK and operator-inspect than spend 3 more replans of agent budget.

## Cross-cutting: per-task wall-clock budget

New knob: `task_max_wall_clock_s` (default 4 hours). The supervisor checks every minute;
if a non-merged task has been alive longer than this, it's eligible for a wipe-and-retry
flag in `qk briefing` (operator decides). This is a soft signal, not auto-block; the
operator sees "R-0019 is 5h old, likely stuck" and can intervene.

## Why "tighten" instead of "loosen"

The argument for high budgets is "let the model figure it out". But empirically:
- Subtasks that succeed do so in attempts 1-3, almost always.
- Subtasks that need 10+ attempts almost never converge — they need replan or wipe.

So the budget is mostly insurance against false-positive flatline detection. We can
lower it dramatically once plans 05 (poison-wipe) and 06 (better progress signal) ship,
because the "noise" cases get handled by those mechanisms instead of by attempt count.

## Sequencing

This plan should land **after** plans 05 and 06. Tightening budgets without better
detectors increases false-positive BLOCKED — operators get woken for things that
would have resolved.

## Tests

Smoke test with the fastapi fixture: pretend the subtask never converges. Assert
BLOCKED at attempt 20, not 50.
