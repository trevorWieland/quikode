"""BLOCKED-as-bug forensics dump.

When a task transitions to BLOCKED, the Store automatically captures a
comprehensive snapshot (retry-cause histogram, last checker outputs,
last triage notes, last progress-check verdicts, peak rss, recent state
log, subtask state distribution). The snapshot is the operator-facing
"what should the system have done differently?" answer.
"""

from __future__ import annotations

from quikode.state import State, Store


def test_block_transition_captures_forensics(tmp_path):
    """Transitioning a task to BLOCKED auto-populates block_forensics."""
    store = Store(tmp_path / "q.db")
    store.upsert_pending("R-001")
    store.upsert_subtasks("R-001", [{"subtask_id": "S-01"}])

    # Seed some retry reasons so the histogram is non-empty.
    store.append_retry_reason(
        "R-001",
        "S-01",
        attempt=1,
        category="checker_fail",
        signature="verdict=FAIL",
    )
    store.append_retry_reason(
        "R-001",
        "S-01",
        attempt=2,
        category="agent_cli_rate_limit",
        signature="429",
        transient=True,
    )
    # Drop a couple of subtask_checker artifacts so the snapshot has something.
    store.add_artifact("R-001", "subtask_checker:S-01", "VERDICT: FAIL\nROOT_CAUSE: missing handler")
    store.add_artifact("R-001", "subtask_triage:S-01", "Doer's output didn't address the root cause.")

    # Pre-condition: no forensics yet.
    assert store.get_block_forensics("R-001") is None

    # Transition through DOING_SUBTASK first so from_state isn't pending.
    store.transition("R-001", State.DOING_SUBTASK)
    store.transition(
        "R-001",
        State.BLOCKED,
        last_error="subtask flatlined",
    )

    snap = store.get_block_forensics("R-001")
    assert snap is not None
    assert snap["task_id"] == "R-001"
    # Retry histogram captured.
    assert snap["retry_categories_total"]["checker_fail"] == 1
    assert snap["retry_categories_total"]["agent_cli_rate_limit"] == 1
    # Per-subtask breakdown present.
    assert any(p["subtask_id"] == "S-01" for p in snap["per_subtask"])
    # Last checker output captured.
    assert any("VERDICT: FAIL" in c["excerpt"] for c in snap["last_checker_outputs"])
    # Last triage note captured.
    assert any("root cause" in t["excerpt"] for t in snap["last_triage_notes"])
    # Recent state-log captured.
    states = [s["to_state"] for s in snap["recent_state_log"]]
    assert "blocked" in states


def test_re_blocking_does_not_overwrite_first_snapshot(tmp_path):
    """Once BLOCKED, transitioning back to BLOCKED (e.g. an automated retry
    that re-blocks) shouldn't re-frame the original captured snapshot."""
    store = Store(tmp_path / "q.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.DOING_SUBTASK)
    store.transition("R-001", State.BLOCKED, last_error="first block")
    snap1 = store.get_block_forensics("R-001")
    assert snap1 is not None

    # Mock-transition through pending then BLOCKED again (would overwrite if
    # the transition path didn't gate on from_state).
    store.transition("R-001", State.PENDING)
    store.transition("R-001", State.DOING_SUBTASK)
    store.transition("R-001", State.BLOCKED, last_error="second block")

    # Snapshot was re-captured (pending → blocked is a fresh block, not a
    # blocked → blocked stutter).
    snap2 = store.get_block_forensics("R-001")
    assert snap2 is not None
    # Direct stutter (blocked → blocked) does NOT recapture.
    store.transition("R-001", State.BLOCKED, last_error="third block")
    snap3 = store.get_block_forensics("R-001")
    # snap3 should equal snap2 (no recapture from blocked→blocked).
    assert snap3 == snap2


def test_capture_block_forensics_handles_empty_task(tmp_path):
    """Defensive: a task with no subtasks / artifacts / progress checks
    should still capture cleanly (empty arrays, not crashes)."""
    store = Store(tmp_path / "q.db")
    store.upsert_pending("R-099")
    store.transition("R-099", State.BLOCKED)
    snap = store.get_block_forensics("R-099")
    assert snap is not None
    assert snap["retry_categories_total"] == {}
    assert snap["per_subtask"] == []
    assert snap["last_checker_outputs"] == []
