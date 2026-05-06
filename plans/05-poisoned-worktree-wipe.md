# Plan 05 — auto-detect poisoned worktree, wipe and replan

## The problem the user named

> "It is truly better to wipe a worktree and start over, rather than carry forward
> poisoned work sometimes."

Today, the only reset granularities are:

- `qk retry <id>` — manual, drops the whole task back to PENDING, fresh worktree, new
  branch. Operator-only, lossy in the sense that we throw out planner output too.
- Subtask retry — keeps the worktree, the failing files, the partially-good commits.

Between those two there's nothing. When a doer poisons the worktree (e.g. introduces a
syntax error in an unrelated file, edits the wrong target, splatters generated junk),
the subtask loop carries the poison forward. Each retry has the same broken starting
point. The flatline detector eventually fires after burning ~10 attempts, and the task
goes BLOCKED.

## What "poisoned worktree" looks like programmatically

Heuristics, in priority order:

1. **Same checker root cause across N consecutive attempts**, AND root cause references
   files outside the subtask's `files_to_touch`. The doer is breaking unrelated code
   and the checker keeps rediscovering it.

2. **`subtask_check_command` (`just check`) keeps failing with errors that didn't exist
   before subtask started.** Per-subtask, capture the `just check` baseline at subtask
   entry; if all attempt checker logs share a failure mode that wasn't in the baseline,
   that's poison.

3. **Doer's diff grows monotonically without converging.** If attempt N's diff is a
   strict superset of attempt N-1's diff plus a few lines, and the failure mode is the
   same, the doer is layering on broken work.

## The recovery: "wipe to subtask boundary"

A new FSM transition:

```
DOING_SUBTASK -> PROVISIONING on Event.WORKTREE_POISONED
```

The transition handler:

1. Tear down container.
2. `git worktree remove --force` the worktree.
3. Re-create worktree from the *parent commit of the current subtask* (not the parent
   of the task — we keep already-merged subtasks of this task).
4. Re-provision container.
5. Re-enter DOING_SUBTASK with attempt counter reset for this subtask, `replan_count`
   incremented.

If `replan_count` already at `intent.max_replans`, fall through to BLOCKED instead.

## Where to detect it

`workers/subtask_progress.py` already runs the progress agent. Add a lightweight
deterministic check before the agent call:

```python
def looks_poisoned(attempts: list[ProgressAttempt], *, files_in_subtask: set[str]) -> bool:
    if len(attempts) < 3:
        return False
    last_three = attempts[-3:]
    causes = [a.checker_root_cause for a in last_three]
    if len(set(causes)) > 1:
        return False
    out_of_scope = any(
        f for cause in causes
        for f in _files_mentioned(cause)
        if f and f not in files_in_subtask
    )
    return out_of_scope
```

If `looks_poisoned()` → emit `WORKTREE_POISONED` event before the next doer call.

## Why this is safer than the alternatives

- `qk retry` throws away planner output. We keep it.
- Increasing `subtask_hard_max_attempts` just delays the BLOCKED. We make progress.
- Asking the doer to "fix the unrelated breakage" pollutes the prompt and steers the
  model away from the actual subtask.

## Risks

- False positives. A subtask that genuinely touches an out-of-scope file (because the
  planner under-specified `files_to_touch`) will look poisoned. Mitigate: cap wipes per
  task at 1, then fall back to `BLOCKED`.
- We rebuild the container — adds ~30s per wipe. Acceptable; we save many minutes
  of doer-checker churn.

## Tests

- Three attempts with same root cause referencing a file outside `files_to_touch` →
  assert `WORKTREE_POISONED` event.
- Three attempts where root cause varies → assert no event.
- Wipe limit: second poison detection in same task → BLOCKED, not another wipe.

## Open question

Should we also detect "worktree has uncommitted changes from a previous attempt"? In
theory the subtask loop commits cleanly, but on a crash mid-run we could end up with a
dirty worktree on resume. Worth a follow-up plan.
