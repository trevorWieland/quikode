# Plan 57 — typed `enter_*` guards in `fsm_runtime` (eliminate the InvalidTransition crash class)

## Why

Today's tanren run has fired four distinct `InvalidTransition: cannot
enter X from Y` crashes from worker / watcher paths:

- Plan 49 patched `_handle_post_pr_ci_failure` + `_handle_changes_requested`
  for `enter_addressing_feedback` from non-AWAITING_REVIEW states
- Plan 54 patched `_run_fixup_round` for `enter_fixup_planning` from
  PENDING_CI + the no-op-DONE path's `_handle_passed_subtask` firing
  SUBTASK_PASSED from ADDRESSING_FEEDBACK

Each patch was a per-call-site state-check guard: re-read current state
right before the FSM call, log + skip if invalid. The pattern works but
multiplies: every NEW call site (or future new state) needs its own
guard. We've still been seeing recurring `enter_doing_subtask` crashes
post plan-54 deploy — a sign the per-call-site approach isn't
sustainable.

Plan 57 generalizes: push the state-check INTO each `enter_*` helper
in `fsm_runtime`. The helpers become safe to call from any worker/
watcher path — invalid transitions log INFO and silently skip; valid
transitions proceed. The InvalidTransition class is eliminated as a
crash source.

This is foundational for plan 58. With plan 58 adding 5 new states +
reshaping transitions, the per-call-site approach would need to grow
proportionally. Plan 57 caps the growth at the FSM boundary.

## What ships

### Refactor every `enter_*` helper in `fsm_runtime.py`

Each helper today follows this shape:

```python
def enter_X(store, task_id, *, note=None, **fields):
    state = State(store.get(task_id)["state"])
    event_by_state = {Y: Event.Y_TO_X, Z: Event.Z_TO_X}
    event = event_by_state.get(state)
    if event is None:
        raise InvalidTransition(f"cannot enter X from {state.value!r}")
    return store.apply_event(task_id, event, note=note, **fields)
```

After plan 57:

```python
def enter_X(store, task_id, *, note=None, **fields) -> State | None:
    """Returns the new State if transitioned, None if silently skipped
    because the source state didn't allow this transition. Callers can
    branch on the return; the default no-action path is `enter_X(...)`
    fire-and-forget.
    """
    state = State(store.get(task_id)["state"])
    event_by_state = {Y: Event.Y_TO_X, Z: Event.Z_TO_X}
    event = event_by_state.get(state)
    if event is None:
        log.info(
            "fsm_runtime.enter_X: skipping (current state %r doesn't allow this transition)",
            state.value,
        )
        return None
    return store.apply_event(task_id, event, note=note, **fields)
```

Every `enter_*` helper in `fsm_runtime.py` gets this treatment:
- Return type becomes `State | None`
- Invalid source state → log INFO with helper name + current state + skip
- Valid source state → proceed as before
- `InvalidTransition` exception removed from the helpers' public surface

`mark_merged` and `block_current` follow the same pattern.

### Remove plan-49 / plan-54 per-call-site guards (cleanup)

The guards from plan 49 (`_handle_post_pr_ci_failure`,
`_handle_changes_requested`) and plan 54 (`_run_fixup_round`,
`_handle_passed_subtask`) become redundant under plan 57. Two options:

- **Keep them as defense-in-depth.** Cheap; explicit. Recommend KEEP.
- **Remove them.** Reduces line count; pushes all guarding to the FSM
  boundary. Riskier (a future regression in plan 57 would re-expose
  the crash).

**Recommend: KEEP plan 49/54 guards as upstream early-skips** (cheaper
than the FSM lookup + clearer at the call site about WHY we skip). The
FSM-level guard is the SAFETY NET, not the primary mechanism.

### Callers that depend on the raise

Search `quikode/` for any callers that catch `InvalidTransition`
specifically. After plan 57 these `except InvalidTransition` blocks
become unreachable for the `enter_*` paths (they'll never raise). Two
treatment options:

- Remove the `except InvalidTransition` blocks (dead code after
  plan 57)
- Keep them; document that they're defense-in-depth in case future
  code paths re-introduce the raise

The orchestrator's `_safe_crash_current` in `task_worker.py`
specifically catches `InvalidTransition` per plan-20 (1C); that one
stays exactly as-is.

Sweep the codebase:
```
grep -rn "except InvalidTransition" quikode/
```
Each hit: assess case-by-case. If it's an `enter_*` call site, remove
the except. If it's `apply_event` directly, leave the except.

### Tests

Add tests for the new no-op-on-invalid behavior:

```python
# tests/test_fsm_runtime_typed_guards.py (new)
def test_enter_X_from_invalid_state_returns_none_and_does_not_raise():
    store = ...
    store.create_task(state=State.MERGED)
    # MERGED is terminal — enter_addressing_feedback should be invalid
    result = fsm_runtime.enter_addressing_feedback(store, task_id)
    assert result is None
    assert log captured INFO with helper name + source state

def test_enter_X_from_valid_state_returns_new_state():
    store = ...
    store.create_task(state=State.AWAITING_REVIEW)
    result = fsm_runtime.enter_addressing_feedback(store, task_id)
    assert result is State.ADDRESSING_FEEDBACK
    assert store.get(task_id)["state"] == State.ADDRESSING_FEEDBACK.value
```

One test per `enter_*` helper at minimum: invalid + valid case. Likely
a parametrized fixture that walks every helper.

Update any existing tests that asserted `raises(InvalidTransition)` on
`enter_*` calls — those assertions now fail because the helper no
longer raises. Replace with `result is None` assertion.

### Plans index + orientation

- Add plan 57 row to `plans/00-INDEX.md`.
- `orientation.md` §7 invariants: new bullet noting that `fsm_runtime
  .enter_*` helpers are now safe to call from any worker/watcher path
  (silently skip on invalid source state with INFO log).

## Operational followup (manager handles)

After agent ships:
1. Validation ladder green.
2. Commit + push.
3. NO daemon restart yet — plan 57 deploys with plan 58 tonight as
   a paired cutover.

## Out of scope

- Plan 58 (state flatten + worker consolidation + phase/cycle layer)
  — ships immediately after plan 57 reviewed.
- Migrating `apply_event`'s raise behavior — it stays as-is. Only the
  `enter_*` helper layer gets the typed guards.
- Adding metrics/observability for skipped transitions — INFO log is
  enough; we can add metrics later if the skip rate becomes a
  concern.
