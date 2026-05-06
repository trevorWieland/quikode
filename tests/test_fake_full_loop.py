from __future__ import annotations

from quikode.fsm import Event
from quikode.state import State, Store


def test_fake_project_full_lifecycle_without_docker_or_github(tmp_path):
    store = Store(tmp_path / "quikode.db")
    task_id = "F-LOOP"
    store.upsert_pending(task_id)

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
        Event.CI_GREEN_THREADS_CLEAN,
        Event.SETTLE_WINDOW_ELAPSED,
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
        Event.CI_FAILED_OR_THREADS_FOUND,
        Event.ACTIONABLE_FEEDBACK,
        Event.FEEDBACK_PUSHED,
        Event.PARENT_MERGED_OR_CONFLICT,
        Event.REBASE_PUSHED,
    ]:
        store.apply_event(task_id, event, note=f"fake-loop:{event.value}")

    row = store.get(task_id)
    assert row is not None
    assert row["state"] == State.PENDING_CI.value
