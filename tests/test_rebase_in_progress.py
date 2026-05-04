"""v3 stacked-diffs fix: improved `_rebase_in_progress()` accuracy.

The legacy detector relied on `git rev-parse --verify REBASE_HEAD`, which
only exists during specific rebase phases. The fixed implementation
walks `git rev-parse --git-path rebase-merge` and `rebase-apply`, then
checks for the resolved directory's existence via `test -d` — which is
the canonical way git itself probes its own rebase state.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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

    class _DAG:
        def __init__(self):
            self.nodes = {"T-1": _node()}

    return TaskWorker(cfg, _DAG(), store, _node())


def test_rebase_in_progress_true_for_rebase_merge(tmp_path):
    """When rebase-merge dir exists, returns True."""
    w = _worker(tmp_path)
    w.handle = MagicMock(container_name="qk-stub")

    def fake_git(args):
        if args[:2] == ["rev-parse", "--git-path"] and args[2] == "rebase-merge":
            return 0, ".git/rebase-merge\n"
        return 0, ""

    w._git_in_workspace = fake_git  # type: ignore[method-assign]
    with patch("quikode.worker.exec_in", return_value=(0, "", "")):
        assert w._rebase_in_progress() is True


def test_rebase_in_progress_true_for_rebase_apply(tmp_path):
    """rebase-merge missing, rebase-apply present → True."""
    w = _worker(tmp_path)
    w.handle = MagicMock(container_name="qk-stub")

    def fake_git(args):
        if args[:2] == ["rev-parse", "--git-path"]:
            return 0, f".git/{args[2]}\n"
        return 0, ""

    w._git_in_workspace = fake_git  # type: ignore[method-assign]

    test_calls: list[list[str]] = []

    def fake_exec_in(handle, cmd, **kwargs):
        test_calls.append(cmd)
        # Simulate: rebase-merge dir doesn't exist (rc=1), rebase-apply does (rc=0).
        if "rebase-merge" in " ".join(cmd):
            return 1, "", ""
        return 0, "", ""

    with patch("quikode.worker.exec_in", side_effect=fake_exec_in):
        assert w._rebase_in_progress() is True
    # Should have probed both directories.
    assert any("rebase-merge" in " ".join(c) for c in test_calls)
    assert any("rebase-apply" in " ".join(c) for c in test_calls)


def test_rebase_in_progress_false_when_no_state_dirs(tmp_path):
    """No rebase state dir on disk → False, even if rev-parse paths resolved."""
    w = _worker(tmp_path)
    w.handle = MagicMock(container_name="qk-stub")

    def fake_git(args):
        if args[:2] == ["rev-parse", "--git-path"]:
            return 0, f".git/{args[2]}\n"
        return 0, ""

    w._git_in_workspace = fake_git  # type: ignore[method-assign]
    # Both `test -d` checks fail.
    with patch("quikode.worker.exec_in", return_value=(1, "", "")):
        assert w._rebase_in_progress() is False


def test_rebase_in_progress_false_when_rev_parse_fails(tmp_path):
    """rev-parse --git-path failing for both kinds → False."""
    w = _worker(tmp_path)
    w.handle = MagicMock(container_name="qk-stub")

    def fake_git(args):
        if args[:2] == ["rev-parse", "--git-path"]:
            return 1, "fatal\n"
        return 0, ""

    w._git_in_workspace = fake_git  # type: ignore[method-assign]
    assert w._rebase_in_progress() is False


def test_resolver_loop_no_conflicts_tries_continue_first(tmp_path, monkeypatch):
    """When the conflict-resolver loop sees no UD files mid-rebase, it tries
    a no-agent --continue before bailing — covers the case where a previous
    --continue advanced past one commit and git is between commits."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-1")
    w.handle = MagicMock(container_name="qk-stub")

    git_calls: list[list[str]] = []
    continue_responses: list[tuple[int, str]] = [(0, "")]  # successful --continue

    def fake_git(args):
        git_calls.append(args)
        if args[0] == "diff" and "--diff-filter=U" in args:
            return 0, ""  # no conflicted files
        if "rebase" in args and "--continue" in args:
            return continue_responses.pop(0) if continue_responses else (1, "fail")
        # Defaults: diff/log lookups
        return 0, ""

    w._git_in_workspace = fake_git  # type: ignore[method-assign]

    out = w._resolve_one_conflict_step(iteration=2)
    assert out is None  # the bare --continue path returns None on success
    # We did NOT abort
    assert not any("--abort" in c for c in git_calls)
    # We DID call --continue (with core.editor=true)
    cont_calls = [c for c in git_calls if "rebase" in c and "--continue" in c]
    assert any("core.editor=true" in c for c in cont_calls)


def test_resolver_loop_no_conflicts_aborts_when_continue_fails(tmp_path, monkeypatch):
    """When `--continue` also fails AND no conflicts surfaced, abort + BLOCKED."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-1")
    w.handle = MagicMock(container_name="qk-stub")

    def fake_git(args):
        if args[0] == "diff" and "--diff-filter=U" in args:
            return 0, ""  # no conflicted files
        if "rebase" in args and "--continue" in args:
            return 1, "boom"
        if "rebase" in args and "--abort" in args:
            return 0, ""
        if args[:1] == ["symbolic-ref"]:
            return 0, "branch\n"
        return 0, ""

    w._git_in_workspace = fake_git  # type: ignore[method-assign]

    out = w._resolve_one_conflict_step(iteration=2)
    assert out is not None
    assert out.final_state == State.BLOCKED


def test_ensure_on_branch_noop_when_already_on_branch(tmp_path):
    """When `symbolic-ref --short -q HEAD` succeeds, nothing happens."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-1")
    w.handle = MagicMock(container_name="qk-stub")

    git_calls: list[list[str]] = []

    def fake_git(args):
        git_calls.append(args)
        if args[:1] == ["symbolic-ref"]:
            return 0, "main\n"
        return 0, ""

    w._git_in_workspace = fake_git  # type: ignore[method-assign]
    w._ensure_on_branch()
    # Only the probe call, no fix-up call.
    assert len(git_calls) == 1


def test_ensure_on_branch_fixes_detached_head(tmp_path):
    """When detached, `symbolic-ref HEAD refs/heads/<branch>` is run."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-1")
    w.store.set_field("T-1", branch="quikode/t-1-abc")
    w.handle = MagicMock(container_name="qk-stub")

    git_calls: list[list[str]] = []

    def fake_git(args):
        git_calls.append(args)
        if args[:3] == ["symbolic-ref", "--short", "-q"]:
            return 1, ""  # detached
        return 0, ""

    w._git_in_workspace = fake_git  # type: ignore[method-assign]
    w._ensure_on_branch()
    assert any(c == ["symbolic-ref", "HEAD", "refs/heads/quikode/t-1-abc"] for c in git_calls)
