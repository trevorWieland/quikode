"""v3 stacked-diffs fix: startup orphan-recovery.

When the orchestrator process dies (SIGTERM, crash) it leaves rows in
active states with no worker behind them. On the next `quikode run`,
`Store.recover_orphan_tasks()` scans for these and rolls them back to
PENDING (with `resume_from_existing_subtasks=1` for in-implementation
states) or AWAITING_MERGE (for PR-already-opened states), resetting
retry counters along the way.

PENDING / MERGED / BLOCKED / FAILED / ABORTED are left alone — they're
already at appropriate FSM positions.
"""

from __future__ import annotations

import pytest

from quikode.config import Config
from quikode.state import State, Store


def _store(tmp_path) -> Store:
    # Scope state_dir to tmp_path so parallel/sequential tests don't share
    # a single .quikode/q.db on disk.
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path, state_dir=tmp_path / ".quikode")
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return Store(cfg.state_dir / "q.db")


def _seed(store: Store, task_id: str, state: State, **fields) -> None:
    store.upsert_pending(task_id)
    store.transition(task_id, state, **fields)


# ---- per-state recovery transitions ----


@pytest.mark.parametrize(
    "from_state",
    [
        State.PLANNING,
        State.DOING_SUBTASK,
        State.CHECKING_SUBTASK,
        State.TRIAGING_SUBTASK,
        State.COMMITTING,
        State.PUSHING,
        State.REPLANNING,
    ],
)
def test_in_implementation_state_resumes_to_pending(tmp_path, from_state):
    s = _store(tmp_path)
    _seed(s, "T-1", from_state)
    out = s.recover_orphan_tasks()
    assert ("T-1", from_state.value, State.PENDING.value) in out
    row = s.get("T-1")
    assert row["state"] == State.PENDING.value
    assert row["resume_from_existing_subtasks"] == 1
    s.conn.close()


def test_provisioning_clears_partial_artifacts(tmp_path):
    s = _store(tmp_path)
    _seed(
        s,
        "T-1",
        State.PROVISIONING,
        branch="quikode/t-1-abc",
        worktree_path="/tmp/wt",
        container_id="c1",
    )
    out = s.recover_orphan_tasks()
    assert ("T-1", State.PROVISIONING.value, State.PENDING.value) in out
    row = s.get("T-1")
    assert row["state"] == State.PENDING.value
    assert row["branch"] is None
    assert row["worktree_path"] is None
    assert row["container_id"] is None
    s.conn.close()


def test_pr_opening_with_pr_number_goes_to_awaiting_merge(tmp_path):
    s = _store(tmp_path)
    _seed(
        s,
        "T-1",
        State.PR_OPENING,
        branch="quikode/t-1-abc",
        pr_number=42,
        pr_url="https://github.com/o/r/pull/42",
    )
    out = s.recover_orphan_tasks()
    assert ("T-1", State.PR_OPENING.value, State.PENDING_CI.value) in out
    row = s.get("T-1")
    assert row["state"] == State.PENDING_CI.value
    s.conn.close()


def test_pr_opening_without_pr_number_resumes_to_pending(tmp_path):
    s = _store(tmp_path)
    _seed(s, "T-1", State.PR_OPENING, branch="quikode/t-1-abc")
    out = s.recover_orphan_tasks()
    assert ("T-1", State.PR_OPENING.value, State.PENDING.value) in out
    row = s.get("T-1")
    assert row["state"] == State.PENDING.value
    assert row["resume_from_existing_subtasks"] == 1
    s.conn.close()


def test_polling_ci_with_pr_number_goes_to_awaiting_merge(tmp_path):
    s = _store(tmp_path)
    _seed(s, "T-1", State.POLLING_CI, pr_number=42)
    s.recover_orphan_tasks()
    assert s.get("T-1")["state"] == State.PENDING_CI.value
    s.conn.close()


def test_addressing_feedback_goes_to_awaiting_merge(tmp_path):
    """The watcher will re-detect the open thread on the next poll tick
    and submit a fresh review-response future."""
    s = _store(tmp_path)
    _seed(s, "T-1", State.ADDRESSING_FEEDBACK, pr_number=42)
    s.recover_orphan_tasks()
    assert s.get("T-1")["state"] == State.PENDING_CI.value
    s.conn.close()


@pytest.mark.parametrize(
    "from_state",
    [State.REBASING, State.CONFLICT_RESOLVING, State.INTENT_REVIEWING, State.REBASING_TO_MAIN],
)
def test_pr_aware_states_with_pr_go_to_awaiting_merge(tmp_path, from_state):
    s = _store(tmp_path)
    _seed(s, "T-1", from_state, pr_number=42)
    s.recover_orphan_tasks()
    assert s.get("T-1")["state"] == State.PENDING_CI.value
    s.conn.close()


@pytest.mark.parametrize(
    "from_state",
    [State.REBASING, State.CONFLICT_RESOLVING, State.INTENT_REVIEWING, State.REBASING_TO_MAIN],
)
def test_pr_aware_states_without_pr_resume_to_pending(tmp_path, from_state):
    s = _store(tmp_path)
    _seed(s, "T-1", from_state)
    s.recover_orphan_tasks()
    row = s.get("T-1")
    assert row["state"] == State.PENDING.value
    assert row["resume_from_existing_subtasks"] == 1
    s.conn.close()


# ---- terminal/already-PENDING states are left alone ----


@pytest.mark.parametrize(
    "from_state",
    [State.PENDING, State.MERGED, State.BLOCKED, State.FAILED, State.ABORTED, State.PENDING_CI],
)
def test_terminalish_states_unchanged(tmp_path, from_state):
    s = _store(tmp_path)
    _seed(s, "T-1", from_state)
    out = s.recover_orphan_tasks()
    # These states should not appear in the recovery output.
    assert all(t[0] != "T-1" for t in out)
    assert s.get("T-1")["state"] == from_state.value
    s.conn.close()


# ---- retry counters cleared on recovery ----


def test_recovery_clears_retry_counters(tmp_path):
    s = _store(tmp_path)
    _seed(s, "T-1", State.DOING_SUBTASK)
    s.set_field(
        "T-1",
        do_check_retries=5,
        ci_triage_retries=2,
        review_triage_retries=1,
        conflict_resolve_retries=3,
        needs_intent_review=1,
        needs_parent_rebase=1,
        last_error="something",
    )
    s.recover_orphan_tasks()
    row = s.get("T-1")
    assert row["do_check_retries"] == 0
    assert row["ci_triage_retries"] == 0
    assert row["review_triage_retries"] == 0
    assert row["conflict_resolve_retries"] == 0
    assert row["needs_intent_review"] == 0
    assert row["needs_parent_rebase"] == 0
    assert row["last_error"] is None
    s.conn.close()


def test_recovery_returns_log_entries(tmp_path):
    s = _store(tmp_path)
    _seed(s, "T-1", State.DOING_SUBTASK)
    _seed(s, "T-2", State.PR_OPENING, pr_number=42)
    _seed(s, "T-3", State.PENDING)  # untouched
    _seed(s, "T-4", State.MERGED)  # untouched
    out = s.recover_orphan_tasks()
    by_id = {t[0]: t for t in out}
    assert by_id["T-1"] == ("T-1", State.DOING_SUBTASK.value, State.PENDING.value)
    assert by_id["T-2"] == ("T-2", State.PR_OPENING.value, State.PENDING_CI.value)
    assert "T-3" not in by_id
    assert "T-4" not in by_id
    s.conn.close()


def test_recovery_idempotent_second_run_is_noop(tmp_path):
    s = _store(tmp_path)
    _seed(s, "T-1", State.DOING_SUBTASK)
    out1 = s.recover_orphan_tasks()
    assert len(out1) == 1
    out2 = s.recover_orphan_tasks()
    assert out2 == []
    s.conn.close()
