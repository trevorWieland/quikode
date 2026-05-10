# Plan 54 — no-op DONE path must respect FSM context (post-PR CI-fix loop fix)

## Why

Plan 53's no-op-DONE path (empty-diff + green-gates + kind != fixup_ci
→ mark subtask DONE + synthesize PASS) fires `Event.SUBTASK_PASSED`
through `_handle_subtask_pass` → `fsm_runtime.enter_committing` etc.

That event chain is designed for the **per-subtask loop** where the
parent task is in `DOING_SUBTASK` / `CHECKING_SUBTASK` /
`TRIAGING_SUBTASK`. In that context, SUBTASK_PASSED is valid.

In the **post-PR CI-fix loop** (run_ci_fix_response), the parent task
is in `ADDRESSING_FEEDBACK`. Subtasks run *inside* that state via a
different control flow. The FSM does not register SUBTASK_PASSED as
valid from ADDRESSING_FEEDBACK → InvalidTransition raised, crash
caught, task back to PENDING_CI, next CI poll re-emits the same
fixup_ci subtask, doer hits no-op-DONE again, same crash. Repeat.

Symptom (2026-05-10 13:08): R-0015 crash-loop on
`run_ci_fix_response` for fixup_ci subtask `F-CI-2-...` (or similar)
after plan 53 deployed.

## What ships

### Worker dispatch — gate no-op-DONE on FSM context

`quikode/workers/subtask_completion.py:_handle_subtask_pass` (or
wherever the no-op-DONE prefix is detected and SUBTASK_PASSED is
fired):

- Before firing SUBTASK_PASSED, read the parent task's current FSM
  state.
- If the state is NOT in the per-subtask-loop set
  `{DOING_SUBTASK, CHECKING_SUBTASK, TRIAGING_SUBTASK}`, the no-op
  -DONE path does not apply via this codepath. Two options for the
  agent to evaluate:
  1. **Direct mark-done**: skip the FSM event, just update
     subtasks.state='done' for this row, persist
     `subtask_no_op_done` artifact, return a `kind="settled"`
     outcome to the caller (run_ci_fix_response). The caller
     advances normally.
  2. **State-specific event**: if there's an analogous
     ADDRESSING_FEEDBACK-context event that means "this fixup
     subtask was no-op", fire that. Probably doesn't exist; option
     1 is simpler.
  Pick option 1 unless a state-specific event exists naturally.

- Document explicitly in the code that no-op-DONE detection happens
  at empty-diff time but the **mark-done effect is FSM-context
  aware**.

### Per-subtask vs CI-fix flow audit

Beyond the no-op-DONE path, audit any other plan-53 logic that
fires per-subtask-loop FSM events. The relevant call sites are
likely in:
- `quikode/workers/subtask_completion.py` (per-subtask pass/fail
  handling)
- `quikode/workers/subtask_execution.py` (the discriminator
  dispatchers)
- `quikode/workers/feedback.py` (run_ci_fix_response — the call
  site that crashed)

Any FSM event firing in the post-PR CI-fix path that assumes
per-subtask-loop state is suspect. Fix the same way: state-check +
direct DB update fallback.

### `enter_fixup_planning` race guard (plan 49 follow-up)

Two earlier monitor events showed
`InvalidTransition: cannot enter fixup_planning from pending_ci`
crashes from `pre_pr.py:135` and similar. This is a separate but
related race — the watcher reads task state, decides to enter
fixup_planning, but the task transitions before the FSM call. Same
fix shape as plan 49: pre-firing-event guard. Patch:

- `quikode/workers/pre_pr.py:_run_fixup_round` (and any sibling)
  re-reads the task state immediately before the
  `enter_fixup_planning` call. If the state is no longer the
  expected ADDRESSING_FEEDBACK, log INFO and skip — the next FSM
  tick will re-evaluate.

### Tests

- `tests/test_workers_feedback.py` (or similar):
  - Doer returns no-op-DONE prefix while parent task is in
    ADDRESSING_FEEDBACK → no FSM event fired, subtask state set to
    done via direct DB update, run_ci_fix_response advances.
  - Doer returns no-op-DONE prefix while parent task is in
    DOING_SUBTASK → existing behavior preserved (FSM event fires,
    pre-commit gate skipped, subtask done).
- `tests/test_pre_pr.py` (or similar):
  - `_run_fixup_round` with task state racing from
    ADDRESSING_FEEDBACK → PENDING_CI between check and FSM call:
    no crash, log+skip behavior.

### Plans index

Add plan 54 row to `plans/00-INDEX.md`.

## Operational followup (manager handles)

After agent ships:
1. Validation ladder green.
2. Commit + push.
3. Reinstall + daemon restart.
4. R-0015's crash-loop ends; the run continues cleanly.

## Out of scope

- The broader generalization of plan 49's guard pattern across all
  watcher-driven `enter_*` calls (plan 55 candidate; this plan
  patches the two known recurring crash sites).
- Any deeper FSM redesign (e.g. unifying per-subtask-loop and
  post-PR CI-fix flow under shared events). Significant scope; out
  of bounds.
