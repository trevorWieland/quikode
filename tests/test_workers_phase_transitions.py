"""Plan 58: phase + cycle lifecycle column wire-up.

The `tasks` table grew three new columns: `phase`, `cycle_in_phase`,
`pr_review_trigger`. These tests exercise the `Store.enter_phase` and
`Store.increment_cycle_in_phase` helpers + the column defaults.
"""

from __future__ import annotations

from pathlib import Path

from quikode.state import Store
from quikode.state_types import Phase, PrReviewTrigger, State


def _make_task(tmp_path: Path) -> Store:
    store = Store(tmp_path / "q.db")
    store.upsert_pending("R-001")
    return store


def test_fresh_task_defaults_to_initial_phase(tmp_path: Path) -> None:
    """A freshly-created task has phase=initial, cycle=1, trigger=none."""
    store = _make_task(tmp_path)
    row = store.get("R-001")
    assert row["phase"] == Phase.INITIAL.value
    assert row["cycle_in_phase"] == 1
    assert row["pr_review_trigger"] == PrReviewTrigger.NONE.value
    store.conn.close()


def test_enter_phase_writes_columns_atomically(tmp_path: Path) -> None:
    """`enter_phase` updates phase / cycle / trigger atomically + records a
    state_log row for historical context."""
    store = _make_task(tmp_path)
    store.enter_phase(
        "R-001",
        Phase.PRE_PR_REVIEW,
        cycle_in_phase=1,
        pr_review_trigger=PrReviewTrigger.NONE,
        note="initial subtasks done; entering PRE_PR_REVIEW phase",
    )
    row = store.get("R-001")
    assert row["phase"] == Phase.PRE_PR_REVIEW.value
    assert row["cycle_in_phase"] == 1
    assert row["pr_review_trigger"] == PrReviewTrigger.NONE.value
    # state_log captures the phase change.
    with store._tx_lock:
        entries = list(
            store.conn.execute(
                "SELECT note FROM state_log WHERE task_id = ? ORDER BY ts DESC LIMIT 1",
                ("R-001",),
            )
        )
    assert "PRE_PR_REVIEW" in entries[0]["note"]
    store.conn.close()


def test_increment_cycle_in_phase_bumps_counter(tmp_path: Path) -> None:
    """`increment_cycle_in_phase` returns the new cycle and bumps the row."""
    store = _make_task(tmp_path)
    store.enter_phase("R-001", Phase.PRE_PR_REVIEW)
    new_cycle = store.increment_cycle_in_phase("R-001", note="fixup round 1")
    assert new_cycle == 2
    row = store.get("R-001")
    assert row["cycle_in_phase"] == 2
    new_cycle = store.increment_cycle_in_phase("R-001")
    assert new_cycle == 3
    store.conn.close()


def test_pr_review_trigger_carries_through_cycle_increment(tmp_path: Path) -> None:
    """When entering PR_REVIEW with a trigger, the increment helper can
    optionally overwrite the trigger to a new value."""
    store = _make_task(tmp_path)
    store.enter_phase(
        "R-001",
        Phase.PR_REVIEW,
        cycle_in_phase=0,
        pr_review_trigger=PrReviewTrigger.NONE,
    )
    new_cycle = store.increment_cycle_in_phase(
        "R-001",
        pr_review_trigger=PrReviewTrigger.CI_FAILURE,
        note="CI failure cycle 1",
    )
    assert new_cycle == 1
    row = store.get("R-001")
    assert row["pr_review_trigger"] == PrReviewTrigger.CI_FAILURE.value
    new_cycle = store.increment_cycle_in_phase(
        "R-001",
        pr_review_trigger=PrReviewTrigger.REVIEW_FEEDBACK,
        note="review feedback cycle 2",
    )
    assert new_cycle == 2
    row = store.get("R-001")
    assert row["pr_review_trigger"] == PrReviewTrigger.REVIEW_FEEDBACK.value
    store.conn.close()


def test_phase_transitions_independent_of_fsm_state(tmp_path: Path) -> None:
    """The phase columns live alongside the FSM state — they are
    orthogonal axes. A task in PENDING_CI can have any phase value."""
    store = _make_task(tmp_path)
    store.transition("R-001", State.PENDING_CI, note="PR opened")
    store.enter_phase(
        "R-001",
        Phase.PR_REVIEW,
        cycle_in_phase=0,
        pr_review_trigger=PrReviewTrigger.NONE,
    )
    row = store.get("R-001")
    assert row["state"] == State.PENDING_CI.value
    assert row["phase"] == Phase.PR_REVIEW.value
    store.conn.close()
