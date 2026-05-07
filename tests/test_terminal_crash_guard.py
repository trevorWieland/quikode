"""Phase 1C: `crash_current` is a no-op when the task is already terminal.

The 2026-05-07 incident's FSM cascade fired CRASH from FAILED state, raising
`InvalidTransition: event 'crash' is not valid from state 'failed'` and
masking the original error that put the task in FAILED in the first place.
The fix puts the firing behind a state guard.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from quikode.fsm import State
from quikode.workers.task_worker import TaskWorker


def test_safe_crash_skips_when_already_failed():
    """The guard reads current_state; if it returns a TERMINAL state,
    `apply_event(CRASH)` must NOT be called."""
    # We don't construct a full TaskWorker here — the method is a self-contained
    # piece of logic that only touches self.store + self.node.id. Using a MagicMock
    # for `self` is sufficient.
    worker = MagicMock(spec=TaskWorker)
    worker.node = MagicMock()
    worker.node.id = "R-0019"
    worker.store = MagicMock()
    with (
        patch("quikode.workers.task_worker.fsm_runtime.current_state", return_value=State.FAILED),
        patch("quikode.workers.task_worker.fsm_runtime.crash_current") as crash,
    ):
        TaskWorker._safe_crash_current(worker, "earlier failure")
    crash.assert_not_called()


def test_safe_crash_skips_when_blocked():
    worker = MagicMock(spec=TaskWorker)
    worker.node = MagicMock()
    worker.node.id = "R-0008"
    worker.store = MagicMock()
    with (
        patch("quikode.workers.task_worker.fsm_runtime.current_state", return_value=State.BLOCKED),
        patch("quikode.workers.task_worker.fsm_runtime.crash_current") as crash,
    ):
        TaskWorker._safe_crash_current(worker, "later exception")
    crash.assert_not_called()


def test_safe_crash_fires_when_active():
    """The legitimate path — task is in DOING_SUBTASK when an exception fires
    — must still call `crash_current` so the row transitions to FAILED."""
    worker = MagicMock(spec=TaskWorker)
    worker.node = MagicMock()
    worker.node.id = "R-0010"
    worker.store = MagicMock()
    with (
        patch(
            "quikode.workers.task_worker.fsm_runtime.current_state",
            return_value=State.DOING_SUBTASK,
        ),
        patch("quikode.workers.task_worker.fsm_runtime.crash_current") as crash,
    ):
        TaskWorker._safe_crash_current(worker, "real bug")
    crash.assert_called_once()
    # The note + last_error params are passed through.
    _args, kwargs = crash.call_args
    assert kwargs.get("note") == "real bug"
    assert kwargs.get("last_error") == "real bug"


def test_safe_crash_swallows_inner_invalid_transition():
    """If `crash_current` itself raises (e.g. some other race), the guard
    logs and returns rather than re-raising — the caller is already inside
    an exception handler."""
    worker = MagicMock(spec=TaskWorker)
    worker.node = MagicMock()
    worker.node.id = "R-0007"
    worker.store = MagicMock()
    with (
        patch(
            "quikode.workers.task_worker.fsm_runtime.current_state",
            return_value=State.PROVISIONING,
        ),
        patch(
            "quikode.workers.task_worker.fsm_runtime.crash_current",
            side_effect=RuntimeError("boom"),
        ),
    ):
        # Should NOT raise.
        TaskWorker._safe_crash_current(worker, "outer failure")
