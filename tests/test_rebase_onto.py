"""v3 stacked-diffs fix: `git rebase --onto` semantics.

When a parent's branch was squash-merged to main, plain
`git rebase origin/main` re-applies the parent's individual commits onto
main and conflicts with the squash. `--onto origin/main <parent_sha>`
drops those commits from the replay.

These tests validate the command shape produced by the worker by
mocking out `_git_in_workspace` and the conflict-resolver/push paths.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from quikode.config import Config
from quikode.dag import Node
from quikode.state import State, Store
from quikode.worker import TaskWorker


def _node(task_id: str = "T-CHILD") -> Node:
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
            self.nodes = {"T-CHILD": _node()}

    return TaskWorker(cfg, _DAG(), store, _node())


def _setup_child_with_parent(w: TaskWorker, parent_branch: str | None) -> None:
    w.store.upsert_pending("T-CHILD")
    w.store.transition("T-CHILD", State.PR_OPENING)
    w.store.set_field("T-CHILD", branch="quikode/t-child-abc123")
    if parent_branch:
        w.store.set_parent_chain(
            "T-CHILD",
            parent_task_ids=["T-PARENT"],
            parent_branches=[parent_branch],
            parent_pr_branches=[parent_branch],
        )
    # Stub container handle so worker._h works.
    w.handle = MagicMock(container_name="qk-stub")


def test_rebase_to_base_branch_uses_onto_when_parent_resolves(tmp_path):
    """When `parent_branch` is set on the row AND `git rev-parse --verify`
    returns a sha, the rebase command uses `--onto <base> <parent_sha>`."""
    w = _worker(tmp_path)
    _setup_child_with_parent(w, "quikode/parent-xyz")

    parent_sha = "deadbeef" * 5
    git_calls: list[list[str]] = []

    def fake_git(args):
        git_calls.append(args)
        if args[:2] == ["rev-parse", "--verify"]:
            return 0, parent_sha + "\n"
        if args[0] == "fetch":
            return 0, ""
        if args[:2] == ["rev-list", "--count"]:
            # Defensive ahead-count check: report 2 commits ahead so the
            # rebase path proceeds (vs BLOCKED on empty branch).
            return 0, "2\n"
        # rebase or push
        if args[:1] == ["push"] or (len(args) > 1 and args[1] == "push"):
            return 0, ""
        # the rebase invocation
        return 0, ""

    w._git_in_workspace = fake_git

    ok = w._rebase_inline("main")
    assert ok is True

    # The rebase call should include --onto <base> <parent_sha>
    rebase_calls = [c for c in git_calls if "rebase" in c]
    assert len(rebase_calls) == 1
    rc = rebase_calls[0]
    assert "--onto" in rc
    onto_idx = rc.index("--onto")
    assert rc[onto_idx + 1] == f"{w.cfg.pr_remote}/{w.cfg.base_branch}"
    assert rc[onto_idx + 2] == parent_sha
    # core.editor=true is in place to skip the editor prompt mid-rebase
    assert "core.editor=true" in rc


def test_rebase_to_base_branch_falls_back_when_parent_ref_missing(tmp_path):
    """When `parent_branch` is set but `rev-parse --verify` fails (local ref
    gone for some reason), the worker falls back to plain rebase."""
    w = _worker(tmp_path)
    _setup_child_with_parent(w, "quikode/parent-gone")

    git_calls: list[list[str]] = []

    def fake_git(args):
        git_calls.append(args)
        if args[:2] == ["rev-parse", "--verify"]:
            return 1, "fatal: bad revision\n"
        if args[:2] == ["rev-list", "--count"]:
            return 0, "2\n"
        return 0, ""

    w._git_in_workspace = fake_git

    ok = w._rebase_inline("main")
    assert ok is True

    rebase_calls = [c for c in git_calls if "rebase" in c]
    assert len(rebase_calls) == 1
    rc = rebase_calls[0]
    assert "--onto" not in rc
    # plain rebase form: rebase <remote>/<base>
    assert rc[-1] == f"{w.cfg.pr_remote}/{w.cfg.base_branch}"
    assert "core.editor=true" in rc


def test_rebase_to_base_branch_falls_back_with_no_parent_branch(tmp_path):
    """No `parent_branch` on the row at all → plain rebase, no rev-parse."""
    w = _worker(tmp_path)
    _setup_child_with_parent(w, parent_branch=None)

    git_calls: list[list[str]] = []

    def fake_git(args):
        git_calls.append(args)
        if args[:2] == ["rev-list", "--count"]:
            return 0, "2\n"
        return 0, ""

    w._git_in_workspace = fake_git

    ok = w._rebase_inline("main")
    assert ok is True

    # No rev-parse --verify call at all
    assert not any(c[:2] == ["rev-parse", "--verify"] for c in git_calls)
    rebase_calls = [c for c in git_calls if "rebase" in c]
    assert len(rebase_calls) == 1
    assert "--onto" not in rebase_calls[0]


def test_run_rebase_to_main_uses_onto_when_parent_resolves(tmp_path, monkeypatch):
    """The full rebase-to-main worker entry uses --onto + parent_sha when
    possible. Mocks out provision/push/retarget; just verifies the rebase
    command shape."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-CHILD")
    w.store.transition(
        "T-CHILD",
        State.REBASING_TO_MAIN,
        branch="quikode/t-child-abc123",
    )
    w.store.set_field(
        "T-CHILD",
        parent_branches='["quikode/parent-xyz"]',
        pr_number=42,
    )
    w.store.set_pre_rebase_state("T-CHILD", State.PENDING_CI.value)
    w.handle = MagicMock(container_name="qk-stub")

    # Stub provision so we don't touch docker.
    monkeypatch.setattr(TaskWorker, "_provision", lambda self, provision_worktree=True: None)
    # Stub PR retarget (subprocess).
    monkeypatch.setattr(TaskWorker, "_retarget_pr_to_main", lambda self, pr: True)
    # Avoid teardown trying to teardown a real container.
    monkeypatch.setattr("quikode.worker.docker_env.teardown", lambda h: None)

    parent_sha = "cafef00d" * 5
    git_calls: list[list[str]] = []

    def fake_git(args):
        git_calls.append(args)
        responses = [
            (args[:2] == ["rev-parse", "--verify"], (0, parent_sha + "\n")),
            (args[0] == "fetch", (0, "")),
            (args[:2] == ["rev-list", "--count"], (0, "2\n")),
            ("rebase" in args, (0, "")),
            (args[0] == "rev-parse", (0, "newmain\n")),
            (args[0] == "push", (0, "")),
        ]
        return next((response for matched, response in responses if matched), (0, ""))

    w._git_in_workspace = fake_git

    outcome = w.run_rebase_to_main()
    assert outcome.final_state == State.PENDING_CI

    rebase_calls = [c for c in git_calls if "rebase" in c]
    assert len(rebase_calls) == 1
    rc = rebase_calls[0]
    assert "--onto" in rc
    onto_idx = rc.index("--onto")
    assert rc[onto_idx + 1] == f"{w.cfg.pr_remote}/{w.cfg.base_branch}"
    assert rc[onto_idx + 2] == parent_sha
    assert "core.editor=true" in rc

    # Stacking metadata cleared on success
    row = w.store.get("T-CHILD")
    assert row["parent_branches"] is None
    assert row["parent_pr_branches"] is None


def test_run_rebase_to_main_falls_back_without_parent(tmp_path, monkeypatch):
    """When no parent_branch on the row, run_rebase_to_main does a plain
    rebase. Forward-compat for non-stacked rebase callers."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-CHILD")
    w.store.transition("T-CHILD", State.REBASING_TO_MAIN, branch="quikode/t-child-abc123")
    w.store.set_pre_rebase_state("T-CHILD", State.PENDING_CI.value)
    w.handle = MagicMock(container_name="qk-stub")

    monkeypatch.setattr(TaskWorker, "_provision", lambda self, provision_worktree=True: None)
    monkeypatch.setattr("quikode.worker.docker_env.teardown", lambda h: None)

    git_calls: list[list[str]] = []

    def fake_git(args):
        git_calls.append(args)
        if args[0] == "fetch":
            return 0, ""
        if args[:2] == ["rev-list", "--count"]:
            return 0, "2\n"
        return 0, ""

    w._git_in_workspace = fake_git

    outcome = w.run_rebase_to_main()
    assert outcome.final_state == State.PENDING_CI

    # No rev-parse --verify call (no parent_branch)
    assert not any(c[:2] == ["rev-parse", "--verify"] for c in git_calls)
    rebase_calls = [c for c in git_calls if "rebase" in c]
    assert len(rebase_calls) == 1
    rc = rebase_calls[0]
    assert "--onto" not in rc
