# Plan 22 — carry forward doer prior output across attempts

## Symptom

R-0005/S-10-bdd-B-0044 spent 47 attempts on a multi-file BDD harness
debugging task that genuinely needs more than one 22-min doer turn:
the first run identifies "DuplicateIdentifier — state leakage in the
in-process API harness", the second run starts from scratch, gets to
"transport: POST /accounts" errors, the third starts from scratch,
identifies a different state-leakage manifestation, and so on. The
doer's investigation thread doesn't accumulate.

Per-attempt durations on this subtask:
```
phase=subtask_doer  dur=1332.6s rc=124  (timeout)
phase=subtask_doer  dur=1332.6s rc=124  (timeout)
phase=subtask_doer  dur=1332.5s rc=124  (timeout)
phase=subtask_doer  dur=1332.5s rc=124  (timeout)
phase=subtask_doer  dur=714.4s  rc=0    (finished but produced wrong fix)
```

Each `dur=1332s` is the doer hitting `subtask_doer_timeout_s` = 22min
exactly (then SIGKILL via `subprocess.TimeoutExpired`). On timeout the
infra at `agents/base.py:214-237` correctly captures partial stdout
and persists it as the `subtask_doer:<subtask_id>` artifact (the
worktree state is also fully preserved — `git reset HEAD -- .` only
unstages, never reverts files). But the next attempt's prompt
**doesn't see it**, so the doer restarts from a blank investigation.

## Why we don't bump the timeout instead

User's standing direction (2026-05-07): "we'd rather carry forward
progress" than expose runaway agents to longer-than-warranted slots.
With 12 parallel slots subscription-capped (`project_max_parallel_subscription_cap`),
giving any one slot a 60-minute leash multiplies opportunity cost on
the other 11 slots. Carry-forward has the same effect — let one
22-minute turn build on the prior 22-minute turn — without inflating
the slot's lower-bound.

## Fix

1. `quikode/store_forensics.py`: new `latest_subtask_doer_output(task_id, subtask_id) -> str | None`
   reads the most recent `subtask_doer:<subtask_id>` artifact body.
2. `quikode/workers/subtask_execution.py`: new helper
   `_fetch_prior_doer_output(subtask, attempt) -> str | None`
   — returns None on attempt 1 or when no prior artifact, otherwise
   trims to the trailing 6000 chars (where the doer's "Files changed"
   / "Summary" sections live and where partial-on-timeout output's
   most recent investigation lives). `_do_subtask` calls it and
   passes the result into `subtask_doer_prompt`.
3. `quikode/prompts.py`: `subtask_doer_prompt` accepts
   `prior_doer_output: str | None`.
4. `prompts/subtask-doer.md`: new conditional block
   `{% if prior_doer_output %}## Your prior attempt's output —
   continue from where you left off ... {% endif %}`. Frames the
   carry-forward as "the worktree state is preserved + here's what
   you were doing" rather than as authoritative instructions, so the
   doer can update its mental model rather than blindly continue.

## Edge cases

- **Truncation marker.** When the prior output exceeds 6000 chars,
  the helper prepends `"[...earlier output truncated...]\n"` so the
  doer knows there's history not visible in the prompt.
- **Timeout-truncated output.** The trailing 6000 chars on a
  SIGKILL'd doer typically end mid-sentence. The prompt template
  explicitly tells the doer that partial output is expected and that
  the worktree state still reflects what the partial run did.
- **Attempt 1.** No prior artifact exists, `prior_doer_output=None`,
  the conditional block doesn't render. No regression for fresh
  subtasks.

## Validation

- `uv run pytest tests/ -q` — 854 passed (no test changes needed).
- Manual verification post-ship: on a subtask's 2nd attempt, the
  rendered prompt header (visible in `<task_log>.log`) includes the
  "Your prior attempt's output" section.
