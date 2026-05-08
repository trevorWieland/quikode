"""worktree.branch_for / sanitize_branch_name tests + commit_subtask."""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

from quikode.subtask_schema import Subtask
from quikode.worktree import (
    _is_transient_git_failure,
    branch_for,
    commit_subtask,
    sanitize_branch_name,
)


def test_sanitize_strips_unsafe_chars():
    assert sanitize_branch_name("R-0001") == "R-0001"
    assert sanitize_branch_name("foo/bar") == "foo/bar"
    assert sanitize_branch_name("foo bar") == "foo-bar"
    assert sanitize_branch_name("weird#name@1") == "weird-name-1"


def test_branch_for_unique_each_call():
    a = branch_for("R-0001")
    b = branch_for("R-0001")
    assert a != b
    assert a.startswith("quikode/r-0001-")
    assert b.startswith("quikode/r-0001-")
    # Suffix length and shape: 6 hex chars
    assert re.match(r"^quikode/r-0001-[0-9a-f]{6}$", a)


def test_branch_for_deterministic_when_disabled():
    a = branch_for("R-0001", unique_suffix=False)
    b = branch_for("R-0001", unique_suffix=False)
    assert a == b == "quikode/r-0001"


def test_custom_prefix():
    b = branch_for("F-0001", prefix="qk")
    assert b.startswith("qk/f-0001-")


# ----- v3 Phase A: commit_subtask -----


def _stub_subtask(files: tuple[str, ...] = ("foo.py", "bar/baz.py")) -> Subtask:
    return Subtask(
        id="S-01",
        title="x",
        depends_on=(),
        files_to_touch=files,
        boundary="",
        acceptance=("x",),
        notes="",
    )


def _handle():
    h = MagicMock()
    h.container_name = "qk-stub"
    return h


def _diff_response(files: tuple[str, ...]) -> str:
    """Mimic `git diff --cached --name-only` output: one path per line."""
    return "\n".join(files) + ("\n" if files else "")


def test_commit_subtask_happy_path_runs_add_commit_push(tmp_path):
    """Each git step succeeds; CommitResult carries the new HEAD sha."""
    calls: list[str] = []
    files = ("foo.py", "bar/baz.py")

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        calls.append(cmd[2])
        if "git diff --cached --name-only" in cmd[2]:
            return 0, _diff_response(files), ""
        if "rev-parse HEAD" in cmd[2]:
            return 0, "deadbeef" * 5 + "\n", ""
        return 0, "", ""

    with patch("quikode.worktree.exec_in", side_effect=fake_exec):
        result = commit_subtask(
            _handle(),
            _stub_subtask(files=files),
            "subtask(S-01): x",
            branch="quikode/r-001-abc",
            remote="origin",
        )

    assert result.success is True
    assert result.commit_sha == "deadbeef" * 5
    assert result.transient is False
    # add-all, diff, commit, rev-parse, push
    assert any("git add -A" in c for c in calls)
    assert any("git diff --cached --name-only" in c for c in calls)
    assert any("git commit -m" in c for c in calls)
    assert any("git push" in c for c in calls)
    # Subset of declared lane → no scope review needed (legitimate).
    assert set(result.accepted_files) == set(files)


def test_commit_subtask_z99_gate_only_success_no_edits():
    """Plan 24 fix: Z-99-style stabilization subtask (`files_to_touch=()`).

    When the doer reports the gate already passes and produces no edits,
    `commit_subtask` must return SUCCESS with no new commit (gate-only
    success). Pre-fix, this was treated as a permanent failure mode; R-0041
    looped 16+ times on it before anyone noticed.
    """
    sub = _stub_subtask(files=())

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        if "git add -A" in cmd[2]:
            return 0, "", ""
        if "git diff --cached --name-only" in cmd[2]:
            return 0, "", ""  # nothing staged
        if "git rev-parse HEAD" in cmd[2]:
            return 0, "abc1234567\n", ""
        return 0, "", ""

    with patch("quikode.worktree.exec_in", side_effect=fake_exec):
        result = commit_subtask(_handle(), sub, "msg", branch="b")
    assert result.success is True
    assert result.transient is False
    assert result.commit_sha == "abc1234567"
    assert "gate-only success" in result.output
    assert result.accepted_files == []


def test_commit_subtask_declared_files_but_no_edits_is_failure():
    """When the subtask declared specific files_to_touch but the doer
    produced no edits, that's the real failure case (doer didn't do
    its job)."""
    sub = _stub_subtask(files=("foo.py", "bar.py"))

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        if "git add -A" in cmd[2]:
            return 0, "", ""
        if "git diff --cached --name-only" in cmd[2]:
            return 0, "", ""  # nothing staged despite declared files
        return 0, "", ""

    with patch("quikode.worktree.exec_in", side_effect=fake_exec):
        result = commit_subtask(_handle(), sub, "msg", branch="b")
    assert result.success is False
    assert result.transient is False
    assert "doer produced no edits" in result.output


def test_commit_subtask_commit_failure_is_real(tmp_path):
    """`git commit` exiting non-zero (e.g. nothing-to-commit, ahead==0) is a real
    failure, never transient."""

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        if "git diff --cached --name-only" in cmd[2]:
            return 0, _diff_response(("foo.py", "bar/baz.py")), ""
        if "git commit" in cmd[2]:
            return 1, "nothing to commit, working tree clean", ""
        if "rev-list --count" in cmd[2]:
            return 0, "0\n", ""
        return 0, "", ""

    with patch("quikode.worktree.exec_in", side_effect=fake_exec):
        result = commit_subtask(_handle(), _stub_subtask(), "msg", branch="b")
    assert result.success is False
    assert result.transient is False
    assert "nothing to commit" in result.output


def test_commit_subtask_transient_push_failure(tmp_path):
    """A push failure with a network marker is transient."""

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        if "git diff --cached --name-only" in cmd[2]:
            return 0, _diff_response(("foo.py", "bar/baz.py")), ""
        if "git push" in cmd[2]:
            return 128, "", "fatal: unable to access 'https://...': Could not resolve host: github.com\n"
        if "rev-parse" in cmd[2]:
            return 0, "abc\n", ""
        return 0, "", ""

    with patch("quikode.worktree.exec_in", side_effect=fake_exec):
        result = commit_subtask(_handle(), _stub_subtask(), "msg", branch="b")
    assert result.success is False
    assert result.transient is True
    assert result.commit_sha == "abc"  # commit landed locally
    assert "Could not resolve host" in result.output


def test_commit_subtask_real_push_failure_not_transient(tmp_path):
    """A push failure without a network marker (e.g. non-fast-forward)
    is real, not transient."""

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        if "git diff --cached --name-only" in cmd[2]:
            return 0, _diff_response(("foo.py", "bar/baz.py")), ""
        if "git push" in cmd[2]:
            return 1, "", "! [rejected] foo -> foo (non-fast-forward)\nerror: failed to push some refs\n"
        if "rev-parse" in cmd[2]:
            return 0, "abc\n", ""
        return 0, "", ""

    with patch("quikode.worktree.exec_in", side_effect=fake_exec):
        result = commit_subtask(_handle(), _stub_subtask(), "msg", branch="b")
    assert result.success is False
    assert result.transient is False


def test_commit_subtask_non_fast_forward_auto_rebases_and_succeeds(tmp_path):
    """When push is rejected non-fast-forward (typically because the doer
    rewrote local history), the worker fetches origin, rebases on top, and
    retries the push. R-0004's F-1-2 spent 8 attempts on this divergence
    before the fix."""
    push_calls: list[int] = []

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        body = cmd[2]
        if "git diff --cached --name-only" in body:
            return 0, _diff_response(("foo.py",)), ""
        if "rev-parse" in body:
            return 0, "abc\n", ""
        if "git push" in body:
            push_calls.append(1)
            if len(push_calls) == 1:
                return 1, "", "! [rejected] foo -> foo (non-fast-forward)\nerror: failed to push some refs\n"
            return 0, "", ""  # second push (post-rebase) succeeds
        if "git fetch" in body and "git rebase" in body:
            return 0, "", ""  # rebase clean
        return 0, "", ""

    with patch("quikode.worktree.exec_in", side_effect=fake_exec):
        result = commit_subtask(_handle(), _stub_subtask(), "msg", branch="b")

    assert result.success is True
    assert len(push_calls) == 2, "should retry push after rebase"


def test_commit_subtask_non_fast_forward_rebase_conflict_fails(tmp_path):
    """If fetch+rebase fails (conflict), the worker aborts the rebase and
    returns a failure with the rebase output — caller's triage sees it."""

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        body = cmd[2]
        if "git diff --cached --name-only" in body:
            return 0, _diff_response(("foo.py",)), ""
        if "rev-parse" in body and "HEAD" in body:
            return 0, "abc\n", ""
        if "git push" in body:
            return 1, "", "! [rejected] foo -> foo (non-fast-forward)\nerror: failed to push some refs\n"
        if "git fetch" in body and "git rebase" in body:
            return 1, "", "CONFLICT (content): Merge conflict in foo.py\n"
        if "git rebase --abort" in body:
            return 0, "", ""
        return 0, "", ""

    with patch("quikode.worktree.exec_in", side_effect=fake_exec):
        result = commit_subtask(_handle(), _stub_subtask(), "msg", branch="b")

    assert result.success is False
    assert "auto-rebase failed" in result.output
    assert "CONFLICT" in result.output


def test_commit_subtask_push_false_skips_push(tmp_path):
    seen_push = {"n": 0}

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        if "git diff --cached --name-only" in cmd[2]:
            return 0, _diff_response(("foo.py", "bar/baz.py")), ""
        if "git push" in cmd[2]:
            seen_push["n"] += 1
        if "rev-parse" in cmd[2]:
            return 0, "deadbeef\n", ""
        return 0, "", ""

    with patch("quikode.worktree.exec_in", side_effect=fake_exec):
        result = commit_subtask(_handle(), _stub_subtask(), "msg", branch="b", push=False)
    assert result.success is True
    assert seen_push["n"] == 0


def test_is_transient_git_failure_detection():
    """The transient marker classifier matches obvious network blip
    phrases and rejects normal failures + rc=0."""
    assert _is_transient_git_failure(128, "Could not resolve host: github.com")
    assert _is_transient_git_failure(128, "Connection refused")
    assert _is_transient_git_failure(128, "Connection timed out")
    assert _is_transient_git_failure(128, "fatal: unable to access 'https://...'")
    assert _is_transient_git_failure(128, "remote end hung up unexpectedly")

    # Real failures — must be classified non-transient.
    assert not _is_transient_git_failure(1, "! [rejected] non-fast-forward")
    assert not _is_transient_git_failure(1, "error: failed to push some refs")
    assert not _is_transient_git_failure(1, "")
    # rc=0 is never transient.
    assert not _is_transient_git_failure(0, "Could not resolve host")
