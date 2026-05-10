# Plan 49 — review watcher must not crash on blocked tasks

## Why

The daemon entered a crash loop on 2026-05-10 08:29 with:

```
InvalidTransition: cannot enter addressing_feedback from blocked
quikode/orchestration/review_watch.py:474 in _schedule_ci_fix_response
```

`_handle_post_pr_ci_failure` saw a failing CI on PR #147 (R-0040, currently
BLOCKED with same-signature stop-loss). It called `_schedule_ci_fix_response`,
which unconditionally fires `fsm_runtime.enter_addressing_feedback`. The FSM
correctly rejects `blocked → addressing_feedback` (operator must unblock
first), but the watcher doesn't respect that — it raises, the orchestrator
child crashes rc=1, supervisor backs off 300s, restart, same crash, repeat.

A second nearby call site (`_apply_changes_requested_review` at
`review_watch.py:285`) has the same shape: a daemon-detected CHANGES_REQUESTED
review on a blocked task would re-trip the same crash.

The system invariant is: **a BLOCKED task is awaiting operator review and the
review watcher must not auto-schedule fixup work on it.** When the operator
unblocks the task, normal review handling resumes.

## What ships

### `quikode/orchestration/review_watch.py`

Two changes, both pre-firing-event guards:

1. `_handle_post_pr_ci_failure` (entry point at ~line 427): after the
   `task_row["id"] in futures or len(futures) >= review_cap` check and BEFORE
   `_schedule_ci_fix_response`, also skip if the task's current state is
   `BLOCKED` or `FAILED`. Log at INFO: "task R-NNNN PR CI failing but task is
   blocked/failed — skipping daemon CI-fix; awaiting operator unblock."

2. `_apply_changes_requested_review` (around line 285): before firing
   `enter_addressing_feedback`, check the current state. If it's `BLOCKED`
   or `FAILED`, log and skip the FSM event + don't schedule a worker. The
   review row should NOT be marked processed in this skip path — when the
   operator unblocks, the watcher will see the review again on the next
   poll and address it normally.
   - Re-read the row's state via `self.store.get(task_id)` immediately
     before the FSM call to avoid TOCTOU drift; the dispatcher upstream
     read state earlier.

### Testing

- New unit-ish tests in `tests/test_review_watch.py` (or wherever
  review_watch tests live). Search first; create file if needed.
- `_handle_post_pr_ci_failure` with a BLOCKED task row: assert no FSM
  event fired, no future submitted, log line emitted at INFO.
- `_handle_post_pr_ci_failure` with a normal (PENDING_CI / AWAITING_REVIEW)
  task row: existing behavior preserved (FSM event fires, future
  submitted).
- `_apply_changes_requested_review` with a BLOCKED task: no FSM event,
  no future, review row NOT marked processed.
- `_apply_changes_requested_review` with a normal task: existing
  behavior preserved.
- Use the existing test fixtures and mocks; if `Store.get` returns a
  TaskRow shape, mock that. Don't reinvent infrastructure.

### Plans index

Add plan 49 row to `plans/00-INDEX.md`.

## Operational followup (manager handles)

After this and plan 48 both land:

1. Validation ladder green.
2. Commit + push.
3. Reinstall + daemon start.
4. Recover the 6 blocked tasks via rewind/reset+resume.

## Out of scope

- Whether BLOCKED should auto-recover on review activity (a stronger
  semantics where review activity unblocks). That's a design change;
  this plan is a crash-loop hotfix only. The skip-and-wait behavior is
  the safe option.
- Other FSM transitions in review_watch beyond the two named here.
- Plan 48's stop-loss bug (separate plan, in flight).
