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


def _exists_check_response(cmd: str, files: tuple[str, ...]) -> str:
    """Mimic the on-disk existence check the new commit_subtask runs before
    `git add`. The shell loop emits one line per existing path."""
    if "[ -e " in cmd:
        return "\n".join(files) + "\n"
    return ""


def test_commit_subtask_happy_path_runs_add_commit_push(tmp_path):
    """Each git step succeeds; CommitResult carries the new HEAD sha."""
    calls: list[str] = []
    files = ("foo.py", "bar/baz.py")

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        calls.append(cmd[2])
        if "[ -e " in cmd[2]:
            return 0, _exists_check_response(cmd[2], files), ""
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
    # exists-check, add, commit, rev-parse, push
    assert any("[ -e " in c for c in calls)
    assert any("git add --" in c for c in calls)
    assert any("git commit -m" in c for c in calls)
    assert any("git push" in c for c in calls)
    # Files-to-touch are quoted into the add command
    add_cmd = next(c for c in calls if "git add --" in c)
    assert "foo.py" in add_cmd
    assert "bar/baz.py" in add_cmd


def test_commit_subtask_no_files_short_circuits():
    sub = _stub_subtask(files=())
    with patch("quikode.worktree.exec_in") as mock_exec:
        result = commit_subtask(_handle(), sub, "msg", branch="b")
    assert result.success is False
    assert result.transient is False
    assert "no files_to_touch" in result.output
    assert mock_exec.call_count == 0


def test_commit_subtask_commit_failure_is_real(tmp_path):
    """`git commit` exiting non-zero (e.g. nothing-to-commit, ahead==0) is a real
    failure, never transient."""

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        if "[ -e " in cmd[2]:
            return 0, _exists_check_response(cmd[2], ("foo.py", "bar/baz.py")), ""
        if "git add --" in cmd[2]:
            return 0, "", ""
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
        if "[ -e " in cmd[2]:
            return 0, _exists_check_response(cmd[2], ("foo.py", "bar/baz.py")), ""
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
        if "[ -e " in cmd[2]:
            return 0, _exists_check_response(cmd[2], ("foo.py", "bar/baz.py")), ""
        if "git push" in cmd[2]:
            return 1, "", "! [rejected] foo -> foo (non-fast-forward)\nerror: failed to push some refs\n"
        if "rev-parse" in cmd[2]:
            return 0, "abc\n", ""
        return 0, "", ""

    with patch("quikode.worktree.exec_in", side_effect=fake_exec):
        result = commit_subtask(_handle(), _stub_subtask(), "msg", branch="b")
    assert result.success is False
    assert result.transient is False


def test_commit_subtask_push_false_skips_push(tmp_path):
    seen_push = {"n": 0}

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        if "[ -e " in cmd[2]:
            return 0, _exists_check_response(cmd[2], ("foo.py", "bar/baz.py")), ""
        if "git push" in cmd[2]:
            seen_push["n"] += 1
        if "rev-parse" in cmd[2]:
            return 0, "deadbeef\n", ""
        return 0, "", ""

    with patch("quikode.worktree.exec_in", side_effect=fake_exec):
        result = commit_subtask(_handle(), _stub_subtask(), "msg", branch="b", push=False)
    assert result.success is True
    assert seen_push["n"] == 0


def test_commit_subtask_skips_missing_files_in_files_to_touch(tmp_path):
    """The bug that burned 9 retries on R-0002/S-09-web: planner declared
    `apps/web/src/i18n/paraglide/messages.ts` but Paraglide auto-generates
    `messages.js` instead. `git add -- <missing>` failed rc=1, the worker
    synthesized a checker FAIL, triage retried, doer no-oped because the
    real implementation was already committed. Lock in: missing files in
    `files_to_touch` are filtered out, the existing files commit normally."""
    calls: list[str] = []
    files = ("apps/web/src/page.tsx", "apps/web/src/i18n/paraglide/messages.ts")
    existing_only = ("apps/web/src/page.tsx",)

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        calls.append(cmd[2])
        if "[ -e " in cmd[2]:
            # Only page.tsx exists; messages.ts doesn't.
            return 0, _exists_check_response(cmd[2], existing_only), ""
        if "rev-parse" in cmd[2]:
            return 0, "feedface" * 5 + "\n", ""
        return 0, "", ""

    with patch("quikode.worktree.exec_in", side_effect=fake_exec):
        result = commit_subtask(
            _handle(),
            _stub_subtask(files=files),
            "subtask(S-09-web): web",
            branch="b",
        )
    assert result.success is True
    assert result.commit_sha == "feedface" * 5
    add_cmd = next(c for c in calls if "git add --" in c)
    assert "page.tsx" in add_cmd
    # The ghost path must NOT be in the add command (would re-trigger the bug).
    assert "messages.ts" not in add_cmd


def test_commit_subtask_all_files_missing_returns_real_failure(tmp_path):
    """If ALL declared files are absent, fail loudly (not a free retry)
    so triage can re-prompt with corrected paths."""

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        if "[ -e " in cmd[2]:
            return 0, "", ""  # nothing exists
        return 0, "", ""

    with patch("quikode.worktree.exec_in", side_effect=fake_exec):
        result = commit_subtask(_handle(), _stub_subtask(), "msg", branch="b")
    assert result.success is False
    assert result.transient is False
    assert "none of the planner-declared files_to_touch exist" in result.output


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
