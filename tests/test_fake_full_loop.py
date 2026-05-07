from __future__ import annotations

from quikode.fsm import Event
from quikode.state import State, Store


def test_fake_project_full_lifecycle_without_docker_or_github(tmp_path):
    store = Store(tmp_path / "quikode.db")
    task_id = "F-LOOP"
    store.upsert_pending(task_id)

    # Plan 28: PR_OPENED → PENDING_CI → AWAITING_REVIEW (CI_PASSED) → MERGED.
    # MERGE_READY + the settle-window hop retired with the per-thread classifier.
    sequence = [
        Event.START_TASK,
        Event.ENVIRONMENT_READY,
        Event.PLAN_VALID,
        Event.DOER_DONE,
        Event.SUBTASK_PASSED,
        Event.COMMIT_CREATED,
        Event.ALL_SUBTASKS_DONE,
        Event.LOCAL_CI_PASSED,
        Event.AUDIT_PASSED,
        Event.PR_OPENED,
        Event.CI_PASSED,
        Event.MERGED,
    ]

    for event in sequence:
        store.apply_event(task_id, event, note=f"fake-loop:{event.value}")

    row = store.get(task_id)
    assert row is not None
    assert row["state"] == State.MERGED.value


def test_fake_project_feedback_and_rebase_branches_without_providers(tmp_path):
    store = Store(tmp_path / "quikode.db")
    task_id = "F-FEEDBACK"
    store.upsert_pending(task_id)
    for event in [
        Event.START_TASK,
        Event.ENVIRONMENT_READY,
        Event.PLAN_VALID,
        Event.DOER_DONE,
        Event.SUBTASK_PASSED,
        Event.COMMIT_CREATED,
        Event.ALL_SUBTASKS_DONE,
        Event.LOCAL_CI_PASSED,
        Event.AUDIT_PASSED,
        Event.PR_OPENED,
        # Plan 28: CI_FAILED routes PENDING_CI → ADDRESSING_FEEDBACK directly,
        # bypassing the retired TRIAGING_FEEDBACK / ACTIONABLE_FEEDBACK pair.
        Event.CI_FAILED,
        Event.FEEDBACK_PUSHED,
        Event.PARENT_MERGED_OR_CONFLICT,
        Event.REBASE_PUSHED,
    ]:
        store.apply_event(task_id, event, note=f"fake-loop:{event.value}")

    row = store.get(task_id)
    assert row is not None
    assert row["state"] == State.PENDING_CI.value
