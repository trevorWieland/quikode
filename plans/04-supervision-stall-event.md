# Plan 04 — supervision stall reset emits an honest FSM event

## Background

When `supervision._check_stalled_review_response()` (orchestration/supervision.py:106–164)
notices that an `ADDRESSING_FEEDBACK` task has been silent past `stall_warn_seconds`,
it cancels the future and calls `fsm_runtime.enter_pending_ci(note="orchestrator
force-recovery: ...")`.

Two observable problems:

1. **Wrong FSM event.** `enter_pending_ci` for `ADDRESSING_FEEDBACK` source applies
   `Event.FEEDBACK_PUSHED`. A reader scanning state_log sees "feedback_pushed" with a
   note "force-recovery"; the event doesn't tell the truth. Anything that branches on
   the event (analytics, dashboards, replay) sees a normal feedback push.

2. **Mute on the worker side.** The cancelled Future is silently popped from `futures`.
   If the worker thread is wedged on a subprocess call, it's still alive — but its
   completion will never be observed. We have no log row that says "task X's worker
   thread was abandoned at <stack>".

## Fix

### A. New FSM event

Add to `quikode/fsm.py`:

```python
class Event(StrEnum):
    ...
    FORCE_RECOVERY_FROM_STALL = "force_recovery_from_stall"
```

Wire transitions:

- `ADDRESSING_FEEDBACK -> PENDING_CI on FORCE_RECOVERY_FROM_STALL`
- `DOING_SUBTASK -> PENDING (?)` — see plan 05 for this; out of scope here.

Add to `fsm_runtime.py`:

```python
def force_recovery_from_stall(store, task_id, *, note=None, **fields):
    return store.apply_event(task_id, Event.FORCE_RECOVERY_FROM_STALL, note=note, **fields)
```

Update `supervision.py:157` to call `force_recovery_from_stall` instead of
`enter_pending_ci`.

### B. Capture worker thread state on cancel

Before `fut.cancel()` in supervision.py:147, peek at the future's stack if available
(via `concurrent.futures.thread._WorkItem` is private — better path is to ask the
TaskWorker for its current state via a lightweight `worker.snapshot()` method we add).

If the snapshot returns "blocked on `subprocess.run('codex')`", that's a real signal —
include it in the note and as an artifact `stall_snapshot`.

### C. Extend `qk briefing` "Recent transitions" to highlight stall recoveries

Already shows transitions. After the new event lands, the briefing prints
"force_recovery_from_stall" instead of "feedback_pushed", which is what we want.

## Tests

- Stub agent_calls table empty + entered_ts old enough → run supervision pass → assert
  state_log row has event = `force_recovery_from_stall`.
- Stub a fake future stuck in running → assert it's popped from `futures` and slot
  becomes available on next reap.
- Existing happy-path tests should be unaffected (force_recovery is only emitted on the
  stall code path).

## Out of scope

- Auto-fixing the underlying wedge (e.g., killing the codex subprocess from the host).
  That's plan 05's territory: the more general "the worktree is poisoned, wipe it"
  flow. Here we only ensure the recovery is *observable*.
