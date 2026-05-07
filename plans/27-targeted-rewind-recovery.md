# Plan 27 — surgical rewind recovery (`qk rewind`)

## Why this exists

Operator's standing direction (2026-05-07 session, after R-0005/S-10
hit the same-signature stop-loss at attempt 49 and BLOCKED with no
acceptable path forward):

> "We cannot just leave R-0005 blocked, it's required, so what do we
> need to do to get it unblocked? Do we have existing mechanisms to
> auto-revert all commits and state back from a previous subtask? For
> example, since the issue was a toxic loop in S-10, could we not
> wipe the tree, but instead resume from the final commit state as it
> was entering S-10? This would enable us to maintain progress but
> replay state back to the last known healthy state."

The pre-plan-27 toolbox forced a binary choice for any task in a
toxic loop:

- **`qk retry`** — wipes the entire worktree + branch + subtask rows.
  Loses every prior subtask's commits. Heavy hammer.
- **`qk resume` / `qk reset-retries`** — preserves everything, but
  the toxic subtask's accumulated worktree garbage and stale triage
  remain. Often just retries into the same loop.
- **Hope it self-heals** via prompts/stop-loss — fine for prevention,
  but no escape hatch when prevention fails.

The user's framing flips this: as we ship better prompts and
stop-losses (plans 21–26), the system blocks more often on real
deadlocks. Without a surgical recovery primitive, the only choice is
"wipe all progress" — disproportionate punishment for one toxic
subtask. Plan 27 adds the missing middle ground.

## What `qk rewind` does

`qk rewind <task_id> <subtask_id>` resets the task to the state it
was in *just before the named subtask started*:

1. **Validate** the task is in BLOCKED or FAILED. (Refuses on
   running tasks; if needed, `qk abort` first.)
2. **Resolve** the rewind target commit-sha:
   - If the named subtask was DONE/committed: target =
     `target.commit_sha~1` (parent commit).
   - If the named subtask was never committed (the typical block
     case): target = current HEAD on the branch, which is already
     the predecessor's commit. Reset still runs to wipe any
     uncommitted toxic edits that accrued across failed attempts.
3. **`git reset --hard <target>`** in the worktree. Wipes index +
   working tree to the chosen point. The user's standing rule
   "never silently revert agent work" is preserved: this is an
   explicit operator-invoked revert that has been authorized by the
   command itself.
4. **`git push --force-with-lease origin <branch>`** by default,
   skipped under `--keep-remote`. Aligns the remote branch tip with
   the local rewound state so the next subtask's commit can push
   cleanly. Falls back to a warning + continuation if push fails;
   the next commit's `git_push_recovery` auto-rebase will resolve
   non-fast-forward.
5. **Reset every subtask** whose `created_at` is at or after the
   target's: `state="pending"`, `retries=0`, `transient_retries=0`,
   `flatline_count=0`, `triage_notes=NULL`, `last_error=NULL`,
   `commit_sha=NULL`, `retry_reasons=NULL`, `accepted_files=NULL`,
   `pre_commit_failures=0`, `progress_check_count=0`. Covers spec
   successors AND any fixup subtasks the planner emitted on top of
   the now-discarded branch state. Immutable subtask fields (id,
   title, depends_on, files_to_touch, boundary, acceptance, notes,
   kind, addresses_findings) are preserved so the worker re-runs
   the same shape.
6. **Clear** `pre_pr_audit_summary` — prior cycle's findings were
   against a branch state that no longer exists.
7. **Transition task** to PENDING with
   `resume_from_existing_subtasks=1` so the worker reuses the saved
   `plan_text` and re-enters at the rewound subtask without
   re-running the planner.

The worker picks up on the next scheduling tick.

## Flags

- `--dry-run` prints the plan (target sha, list of subtasks to
  reset, branch + worktree paths) without changing state. Always
  the first thing operators should run on an unfamiliar task.
- `--keep-remote` skips the force-push step. Use when the operator
  wants to preserve the remote branch as it is (rare; typically
  for forensics).

## Example: unblock R-0005 from the soft-cap stop-loss

R-0005/S-10-bdd-B-0044 hit the plan-23 same-signature stop-loss at
attempt 49 (47 of those attempts were burned under pre-plan-23
code). The toxic loop was real — the doer kept editing the wrong
files in a fundamentally broken way. S-09-bdd-harness-steps and
earlier landed cleanly.

```
$ qk rewind R-0005 S-10-bdd-B-0044 --dry-run
$ qk rewind R-0005 S-10-bdd-B-0044
```

The worktree resets to S-09's commit, S-10's accumulated 47 attempts
of cross-file uncommitted garbage gets wiped, S-10 goes back to
PENDING with retries=0. Worker picks up; doer runs S-10 fresh with
all the post-plan-21–26 stop-losses, observability, and prior-output
carry-forward armed.

If S-10 still loops with new code: same-signature stop-loss fires
again, operator runs `qk rewind` again with whatever new diagnosis
the prompts/stop-loss surface (could be different subtask this
time, or could conclude the planner over-decomposed).

## What this does NOT do

- **Doesn't bypass safety rails.** Block reasons are preserved in
  `state_log` (the rewind transition's note links back to the
  reason). If the user runs rewind without understanding why the
  task BLOCKED, plan-23/24/26 will surface again.
- **Doesn't preserve the failed subtask's investigation.** Plan 22's
  `subtask_doer:<id>` artifacts persist (the worker doesn't delete
  them on rewind), so on the next attempt the doer still gets the
  prior-output carry-forward — but the worktree edits are gone.
  This is the right tradeoff: keep the *thinking*, discard the
  *poisoned commits*.
- **Doesn't handle running tasks.** Refused at the validation step
  with a clear error pointing to `qk abort` first.

## Validation

- New tests:
  - Refuses on tasks not in BLOCKED/FAILED.
  - Refuses on tasks with no worktree_path / branch.
  - Refuses on unknown subtask_id.
  - Resets correct set of subtasks (target + topologically-after).
  - Clears pre_pr_audit_summary.
  - Transitions to PENDING with resume marker.
  - --dry-run makes no DB / git changes.
- Validation ladder (`uv run pytest tests/ -q --deselect <pre-existing-budget>`)
  — green.

## Documentation

`orientation.md` updated to remove the "wipe and start over" pattern
from the recovery toolbox in favor of the rewind-first flow:

- Old: "It is truly better to wipe a worktree and start over,
  rather than carry forward poisoned work sometimes."
- New: "Prefer surgical rewind (`qk rewind <task> <subtask>`) over
  full wipes (`qk retry`). Rewind keeps every prior subtask's
  commits and discards only the toxic subtask's accumulated state."
