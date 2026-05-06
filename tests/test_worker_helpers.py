"""TaskWorker._row / _h narrowing helpers — guard against the recursion bug
where _row() called itself instead of self.store.get()."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from quikode import fsm_runtime
from quikode import github as gh_mod
from quikode.config import Config
from quikode.dag import Node
from quikode.state import State, Store
from quikode.worker import TaskWorker


def _node() -> Node:
    return Node(
        id="T-1",
        kind="behavior",
        milestone="M-1",
        title="x",
        scope="x",
        depends_on=(),
        completes_behaviors=(),
        supports_behaviors=(),
        boundary_with_neighbors="",
        expected_evidence=(),
        playbook=(),
        rationale="",
        risks=(),
        raw={},
    )


def _worker(tmp_path) -> TaskWorker:
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path)
    store = Store(tmp_path / "q.db")

    # DAG just needs the node mapping; create a stub that has the right shape
    class _DAG:
        def __init__(self):
            self.nodes = {"T-1": _node()}

    return TaskWorker(cfg, _DAG(), store, _node())


def test_row_helper_returns_dict_not_none(tmp_path):
    w = _worker(tmp_path)
    w.store.upsert_pending("T-1")
    row = w._row()
    assert isinstance(row, dict)
    assert row["id"] == "T-1"


def test_row_helper_is_not_recursive(tmp_path):
    """Regression: _row() once called itself instead of self.store.get()."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-1")
    # Should not blow the stack — was previously RecursionError
    for _ in range(50):
        w._row()


def test_row_helper_asserts_when_missing(tmp_path):
    w = _worker(tmp_path)
    with pytest.raises(AssertionError, match="T-1"):
        w._row()


def test_h_helper_asserts_until_provisioned(tmp_path):
    w = _worker(tmp_path)
    with pytest.raises(AssertionError, match="_provision"):
        _ = w._h


def test_commit_push_clean_tree_with_branch_ahead_pushes(tmp_path, monkeypatch):
    """v3 regression: per-subtask commits make the working tree clean by the
    time _commit_push runs. The old 'nothing to commit → no diff' branch
    used to short-circuit to PENDING_CI without pushing or opening a PR.
    Fix: when commit_all reports clean tree, check ahead_count first; only
    treat as no-op when both are zero."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-1")
    # Stub container handle so worker._h works.
    w.handle = MagicMock(container_name="qk-stub")
    # Move past PROVISIONING so _h assertion passes.
    w.store.transition("T-1", State.CHECKING_SUBTASK)
    # Set a branch on the row so _commit_push can read it.
    w.store.set_field("T-1", branch="quikode/t-1-abc123")

    # commit_all returns rc=1 with the canonical "nothing to commit" message.
    monkeypatch.setattr(
        gh_mod,
        "commit_all",
        lambda h, msg, log_path=None: (1, "nothing to commit, working tree clean"),
    )
    push_called: list[tuple[str, str]] = []

    def fake_push(h, branch, remote="origin", log_path=None):
        push_called.append((branch, remote))
        return 0, ""

    monkeypatch.setattr(gh_mod, "push", fake_push)
    # Branch is 2 commits ahead of base — per-subtask commits exist.
    monkeypatch.setattr(gh_mod, "ahead_count", lambda h, branch, base="main", log_path=None: 2)
    # Avoid noise from sound module.
    monkeypatch.setattr("quikode.worker.sound.ding", lambda: None)

    outcome = w._commit_push()

    assert outcome is None, "should fall through to PR open, not return"
    assert push_called == [("quikode/t-1-abc123", w.cfg.pr_remote)]
    row = w._row()
    assert row["state"] == State.PUSHING.value


def test_commit_push_skips_redundant_commit_when_already_pushing(tmp_path, monkeypatch):
    """v3 regression: per-subtask commit+push leaves the task in PUSHING when
    the LAST subtask completes. _commit_push() previously fired SUBTASK_PASSED
    unconditionally → InvalidTransition (PUSHING ↛ COMMITTING) → task FAILED
    just before its first PR. Fix: detect PUSHING and advance directly to
    LOCAL_CI_CHECKING."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-1")
    w.handle = MagicMock(container_name="qk-stub")
    # Walk through the FSM to PUSHING state legally.
    w.store.transition("T-1", State.CHECKING_SUBTASK)
    w.store.transition("T-1", State.COMMITTING)
    w.store.transition("T-1", State.PUSHING)
    w.store.set_field("T-1", branch="quikode/t-1-abc123")

    # commit_all / push must NOT be called — the per-subtask flow already did
    # both. If the guard is missing, _commit_push will call enter_committing,
    # raise InvalidTransition, and these stubs would never run anyway.
    def _no_call(*args, **kwargs):
        raise AssertionError("commit/push should not be called when already in PUSHING")

    monkeypatch.setattr(gh_mod, "commit_all", _no_call)
    monkeypatch.setattr(gh_mod, "push", _no_call)

    outcome = w._commit_push()

    assert outcome is None, "fall through to pre-PR pipeline"
    row = w._row()
    assert row["state"] == State.LOCAL_CI_CHECKING.value


def test_commit_push_resume_from_planning_with_all_subtasks_done(tmp_path, monkeypatch):
    """Resume bug: when `qk resume <id>` runs on a task that had all subtasks
    DONE in the store, _plan() lands the task in PLANNING and _subtask_loop
    returns None without entering any subtask body. _commit_push then tried
    to fire SUBTASK_PASSED from PLANNING → InvalidTransition. Fix: detect any
    of {PUSHING, PLANNING, DOING_SUBTASK} and walk synthetic transitions to
    LOCAL_CI_CHECKING."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-1")
    w.handle = MagicMock(container_name="qk-stub")
    # Land in PLANNING — the state where resume's _plan() leaves us.
    w.store.transition("T-1", State.PROVISIONING)
    w.store.transition("T-1", State.PLANNING)
    w.store.set_field("T-1", branch="quikode/t-1-resumed")

    def _no_call(*args, **kwargs):
        raise AssertionError("commit/push should not run on a fully-done resume")

    monkeypatch.setattr(gh_mod, "commit_all", _no_call)
    monkeypatch.setattr(gh_mod, "push", _no_call)

    outcome = w._commit_push()

    assert outcome is None
    row = w._row()
    assert row["state"] == State.LOCAL_CI_CHECKING.value


def test_enter_local_ci_checking_idempotent(tmp_path):
    """Pipeline cycle 1 calls enter_local_ci_checking after _commit_push's
    fast-forward already landed us in LOCAL_CI_CHECKING. Without the
    idempotency guard, the second call fires ALL_SUBTASKS_DONE from
    LOCAL_CI_CHECKING — InvalidTransition, task FAILS at the start of the
    pre-PR pipeline."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-1")
    w.store.transition("T-1", State.LOCAL_CI_CHECKING)
    # Should not raise — this is the call from pre_pr.py:_run_pre_pr_pipeline.
    fsm_runtime.enter_local_ci_checking(w.store, "T-1", note="cycle 1")
    assert w._row()["state"] == State.LOCAL_CI_CHECKING.value


def test_commit_push_clean_tree_no_commits_marks_no_diff(tmp_path, monkeypatch):
    """When the tree is clean AND the branch has no commits ahead, the old
    'no diff — task already complete' shortcut still applies."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-1")
    w.handle = MagicMock(container_name="qk-stub")
    w.store.transition("T-1", State.CHECKING_SUBTASK)
    w.store.set_field("T-1", branch="quikode/t-1-empty")

    monkeypatch.setattr(
        gh_mod,
        "commit_all",
        lambda h, msg, log_path=None: (1, "nothing to commit, working tree clean"),
    )
    monkeypatch.setattr(gh_mod, "ahead_count", lambda h, branch, base="main", log_path=None: 0)
    monkeypatch.setattr("quikode.worker.sound.ding", lambda: None)

    outcome = w._commit_push()

    assert outcome is not None
    assert outcome.final_state == State.PENDING_CI
    assert "no diff" in outcome.note
