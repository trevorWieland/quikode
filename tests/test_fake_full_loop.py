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
        # Plan 58: AUDIT_PASSED retired in favor of per-stage advance events.
        Event.AUDIT_LOCAL_CI_PASSED,
        Event.AUDIT_RUBRIC_PASSED,
        Event.AUDIT_STANDARDS_PASSED,
        Event.AUDIT_ARCHITECTURE_PASSED,
        Event.AUDIT_BEHAVIOR_PASSED,
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
        # Plan 58: per-stage events advance through the gauntlet, then
        # PR_OPENED → PENDING_CI → CI_FIXUP_START enters AUDIT_LOCAL_CI
        # → ... → AUDIT_BEHAVIOR_PASSED → PR_OPENING → PR_OPENED → PENDING_CI.
        Event.AUDIT_LOCAL_CI_PASSED,
        Event.AUDIT_RUBRIC_PASSED,
        Event.AUDIT_STANDARDS_PASSED,
        Event.AUDIT_ARCHITECTURE_PASSED,
        Event.AUDIT_BEHAVIOR_PASSED,
        Event.PR_OPENED,
        Event.CI_FIXUP_START,
        Event.AUDIT_LOCAL_CI_PASSED,
        Event.AUDIT_RUBRIC_PASSED,
        Event.AUDIT_STANDARDS_PASSED,
        Event.AUDIT_ARCHITECTURE_PASSED,
        Event.AUDIT_BEHAVIOR_PASSED,
        Event.PR_OPENED,
        Event.PARENT_MERGED_OR_CONFLICT,
        Event.REBASE_PUSHED,
    ]:
        store.apply_event(task_id, event, note=f"fake-loop:{event.value}")

    row = store.get(task_id)
    assert row is not None
    assert row["state"] == State.PENDING_CI.value
