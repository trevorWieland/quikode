"""Bug 4 from validation-2026-05-03 findings: defensive post-rebase
ahead-count check.

After a rebase succeeds, the worker now verifies the resulting branch
is at least 1 commit ahead of the base branch before pushing. If the
rebase / conflict-resolver dropped all of the task's exclusive work
(0 commits ahead), pushing would land an empty PR that github
auto-closes — instead, BLOCK with a clear note so a human can inspect.

This test exercises the three rebase entry points:
  - `_rebase_to_base_branch` (used by `_handle_parent_rebase_if_needed`)
  - `run_rebase_to_main` (the v3 alternate worker entry mode)
  - `_rebase_or_resolve` (the worker-side `_poll_pr_loop` path)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import quikode.worker as worker_mod
from quikode.config import Config
from quikode.dag import Node
from quikode.state import State, Store
from quikode.worker import TaskWorker


def _node(task_id: str = "T-X") -> Node:
    return Node(
        id=task_id,
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


def _worker(tmp_path) -> Any:
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path)
    store = Store(tmp_path / "q.db")

    class _DAG:
        def __init__(self):
            self.nodes = {"T-X": _node()}

    return TaskWorker(cfg, _DAG(), store, _node())


# -------- _rebase_to_base_branch --------


def test_rebase_to_base_branch_blocks_when_ahead_zero(tmp_path):
    """_rebase_to_base_branch: rebase succeeds, ahead-count is 0 →
    BLOCK with the empty-branch note; push is NOT attempted."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-X")
    w.store.transition("T-X", State.PR_OPENING, branch="quikode/t-x-abc")
    w.store.set_field("T-X", worktree_path="/tmp/wt-x")
    w.handle = MagicMock(container_name="qk-stub")

    git_calls: list[list[str]] = []

    def fake_git(args):
        git_calls.append(args)
        if args[0] == "fetch":
            return 0, ""
        if args[:2] == ["rev-list", "--count"]:
            return 0, "0\n"  # branch is 0 ahead — empty
        if "rebase" in args:
            return 0, ""
        if args[0] == "push":
            # Should NOT be reached — defensive check should fire first.
            raise AssertionError("push should not run when branch is 0 ahead")
        return 0, ""

    w._git_in_workspace = fake_git

    ok = w._rebase_to_base_branch()
    assert ok is False
    row = w.store.get("T-X")
    assert row["state"] == State.BLOCKED.value
    # No push call was made
    assert not any(c[:1] == ["push"] for c in git_calls)
    # The note mentions the worktree path + base branch
    assert "0 commits ahead" in (row.get("last_error") or "")


def test_rebase_to_base_branch_proceeds_when_ahead_positive(tmp_path):
    """_rebase_to_base_branch: rebase succeeds, ahead-count > 0 → push
    proceeds normally."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-X")
    w.store.transition("T-X", State.PR_OPENING, branch="quikode/t-x-abc")
    w.handle = MagicMock(container_name="qk-stub")

    git_calls: list[list[str]] = []

    def fake_git(args):
        git_calls.append(args)
        if args[0] == "fetch":
            return 0, ""
        if args[:2] == ["rev-list", "--count"]:
            return 0, "2\n"
        if "rebase" in args:
            return 0, ""
        if args[0] == "push":
            return 0, ""
        return 0, ""

    w._git_in_workspace = fake_git

    ok = w._rebase_to_base_branch()
    assert ok is True
    push_calls = [c for c in git_calls if c[:1] == ["push"]]
    assert len(push_calls) == 1


# -------- run_rebase_to_main --------


def test_run_rebase_to_main_blocks_on_empty_branch(tmp_path, monkeypatch):
    """run_rebase_to_main: post-rebase ahead-count is 0 → BLOCK before
    force-push or PR retarget."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-X")
    w.store.transition("T-X", State.REBASING_TO_MAIN, branch="quikode/t-x-abc")
    w.store.set_field("T-X", worktree_path="/tmp/wt-x", pr_number=42)
    w.store.set_pre_rebase_state("T-X", State.PENDING_CI.value)
    w.handle = MagicMock(container_name="qk-stub")

    monkeypatch.setattr(TaskWorker, "_provision", lambda self, provision_worktree=True: None)
    monkeypatch.setattr("quikode.worker.docker_env.teardown", lambda h: None)
    retarget_called: list[int] = []
    monkeypatch.setattr(
        TaskWorker,
        "_safe_retarget_or_recreate",
        lambda self, pr: retarget_called.append(pr),
    )

    git_calls: list[list[str]] = []

    def fake_git(args):
        git_calls.append(args)
        if args[0] == "fetch":
            return 0, ""
        if args[:2] == ["rev-list", "--count"]:
            return 0, "0\n"
        if "rebase" in args:
            return 0, ""
        if args[0] == "rev-parse":
            return 0, "newmain\n"
        if args[0] == "push":
            raise AssertionError("push must not run when branch is 0 ahead")
        return 0, ""

    w._git_in_workspace = fake_git

    outcome = w.run_rebase_to_main()
    assert outcome.final_state == State.BLOCKED
    assert "post-rebase empty branch" in outcome.note
    # PR retarget never fires
    assert retarget_called == []
    row = w.store.get("T-X")
    assert "0 commits ahead" in (row.get("last_error") or "")


def test_run_rebase_to_main_proceeds_on_nonempty_branch(tmp_path, monkeypatch):
    """run_rebase_to_main: ahead-count > 0 → normal flow continues
    (push, retarget, restore pre-rebase state)."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-X")
    w.store.transition("T-X", State.REBASING_TO_MAIN, branch="quikode/t-x-abc")
    w.store.set_field("T-X", pr_number=42)
    w.store.set_pre_rebase_state("T-X", State.PENDING_CI.value)
    w.handle = MagicMock(container_name="qk-stub")

    monkeypatch.setattr(TaskWorker, "_provision", lambda self, provision_worktree=True: None)
    monkeypatch.setattr(TaskWorker, "_safe_retarget_or_recreate", lambda self, pr: None)
    monkeypatch.setattr("quikode.worker.docker_env.teardown", lambda h: None)

    def fake_git(args):
        if args[0] == "fetch":
            return 0, ""
        if args[:2] == ["rev-list", "--count"]:
            return 0, "3\n"
        if "rebase" in args:
            return 0, ""
        if args[0] == "rev-parse":
            return 0, "newmain\n"
        if args[0] == "push":
            return 0, ""
        return 0, ""

    w._git_in_workspace = fake_git

    outcome = w.run_rebase_to_main()
    assert outcome.final_state == State.PENDING_CI


# -------- _rebase_or_resolve (clean-rebase path) --------


def test_rebase_or_resolve_blocks_on_empty_branch(tmp_path, monkeypatch):
    """_rebase_or_resolve: clean rebase that produces a 0-commit-ahead
    branch → BLOCK before force-push."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-X")
    w.store.transition("T-X", State.PENDING_CI, branch="quikode/t-x-abc")
    w.store.set_field("T-X", worktree_path="/tmp/wt-x")
    w.handle = MagicMock(container_name="qk-stub")

    git_calls: list[list[str]] = []

    def fake_git(args):
        git_calls.append(args)
        if args[0] == "fetch":
            return 0, ""
        if "rebase" in args:
            return 0, ""
        if args[0] == "rev-parse":
            return 0, "newmain\n"
        if args[:2] == ["rev-list", "--count"]:
            return 0, "0\n"
        if args[0] == "push":
            raise AssertionError("push must not run when branch is 0 ahead")
        return 0, ""

    w._git_in_workspace = fake_git

    def fail_push(*a, **kw):
        raise AssertionError("github.push should not run when branch is 0 ahead")

    monkeypatch.setattr(worker_mod.github, "push", fail_push)
    outcome = w._rebase_or_resolve()

    assert outcome is not None
    assert outcome.final_state == State.BLOCKED
    row = w.store.get("T-X")
    assert "0 commits ahead" in (row.get("last_error") or "")
