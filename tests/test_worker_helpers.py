"""TaskWorker._row / _h narrowing helpers — guard against the recursion bug
where _row() called itself instead of self.store.get()."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

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
