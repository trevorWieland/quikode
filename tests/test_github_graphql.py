"""Tests for the v3 GitHub review-thread GraphQL helper.

These tests mock `subprocess.run` so we can drive `gh api graphql` without
hitting the network. The helper is best-effort — bad json/non-zero rc/timeouts
all return safe empty values rather than raising.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from quikode.github_graphql import (
    ReviewThread,
    get_review_threads,
    is_bot_author,
    resolve_thread,
)


def _make_completed_process(
    stdout: str, returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)


# --------------------------- is_bot_author ----------------------------------


def test_is_bot_author_bot_suffix():
    assert is_bot_author("dependabot[bot]") is True
    assert is_bot_author("github-actions[bot]") is True
    assert is_bot_author("anything[bot]") is True


def test_is_bot_author_allowlist():
    assert is_bot_author("chatgpt-codex-connector") is True
    assert is_bot_author("github-actions") is True
    assert is_bot_author("dependabot") is True
    assert is_bot_author("claude") is True
    assert is_bot_author("codecov-commenter") is True


def test_is_bot_author_human():
    assert is_bot_author("trevorWieland") is False
    assert is_bot_author("alice") is False
    assert is_bot_author("") is False


def test_is_bot_author_env_extension(monkeypatch):
    monkeypatch.setenv("QUIKODE_BOT_ALLOWLIST", "myci-bot, snazzy-reviewer")
    assert is_bot_author("myci-bot") is True
    assert is_bot_author("snazzy-reviewer") is True
    assert is_bot_author("not-listed") is False


# --------------------------- get_review_threads -----------------------------


def test_get_review_threads_empty():
    """No threads at all → empty list, no raise."""
    payload = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}}
    fake = _make_completed_process(json.dumps(payload))
    with patch("quikode.github_graphql.subprocess.run", return_value=fake):
        result = get_review_threads("owner/repo", 42)
    assert result == []


def test_get_review_threads_multi_thread():
    payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "id": "PRRT_kw1",
                                "isResolved": False,
                                "isOutdated": False,
                                "path": "src/foo.py",
                                "line": 42,
                                "comments": {
                                    "nodes": [
                                        {
                                            "id": "PRC_1",
                                            "body": "Please rename this",
                                            "createdAt": "2026-05-02T14:31:45Z",
                                            "author": {"login": "trevorWieland"},
                                        }
                                    ]
                                },
                            },
                            {
                                "id": "PRRT_kw2",
                                "isResolved": True,
                                "isOutdated": True,
                                "path": None,
                                "line": None,
                                "comments": {
                                    "nodes": [
                                        {
                                            "id": "PRC_2",
                                            "body": "LGTM",
                                            "createdAt": "2026-05-02T15:00:00Z",
                                            "author": {"login": "chatgpt-codex-connector"},
                                        }
                                    ]
                                },
                            },
                        ]
                    }
                }
            }
        }
    }
    fake = _make_completed_process(json.dumps(payload))
    with patch("quikode.github_graphql.subprocess.run", return_value=fake):
        result = get_review_threads("owner/repo", 42)
    assert len(result) == 2
    a, b = result
    assert isinstance(a, ReviewThread)
    assert a.thread_id == "PRRT_kw1"
    assert a.is_resolved is False
    assert a.is_outdated is False
    assert a.path == "src/foo.py"
    assert a.line == 42
    assert a.last_comment_id == "PRC_1"
    assert a.last_comment_author == "trevorWieland"
    assert a.last_comment_body == "Please rename this"
    assert a.last_comment_is_bot is False
    assert a.last_comment_created_at > 0
    # Resolved + bot author second thread
    assert b.is_resolved is True
    assert b.is_outdated is True
    assert b.last_comment_is_bot is True
    assert b.last_comment_author == "chatgpt-codex-connector"


def test_get_review_threads_bad_json_returns_empty():
    fake = _make_completed_process("not json {{{")
    with patch("quikode.github_graphql.subprocess.run", return_value=fake):
        result = get_review_threads("owner/repo", 42)
    assert result == []


def test_get_review_threads_gh_nonzero_returns_empty():
    fake = _make_completed_process("", returncode=2, stderr="some gh error")
    with patch("quikode.github_graphql.subprocess.run", return_value=fake):
        result = get_review_threads("owner/repo", 42)
    assert result == []


def test_get_review_threads_invalid_repo_returns_empty():
    """Repo string without a slash → empty list, no raise."""
    result = get_review_threads("not-a-repo", 42)
    assert result == []


def test_get_review_threads_subprocess_oserror_returns_empty():
    with patch("quikode.github_graphql.subprocess.run", side_effect=OSError("gh missing")):
        result = get_review_threads("owner/repo", 42)
    assert result == []


def test_get_review_threads_missing_comments_skipped():
    """A thread with no comment nodes is skipped (nothing to act on)."""
    payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "id": "PRRT_empty",
                                "isResolved": False,
                                "isOutdated": False,
                                "path": "x.py",
                                "line": 1,
                                "comments": {"nodes": []},
                            }
                        ]
                    }
                }
            }
        }
    }
    fake = _make_completed_process(json.dumps(payload))
    with patch("quikode.github_graphql.subprocess.run", return_value=fake):
        result = get_review_threads("owner/repo", 42)
    assert result == []


# --------------------------- resolve_thread ---------------------------------


def test_resolve_thread_success():
    payload = {"data": {"resolveReviewThread": {"thread": {"id": "PRRT_xxx", "isResolved": True}}}}
    fake = _make_completed_process(json.dumps(payload))
    with patch("quikode.github_graphql.subprocess.run", return_value=fake):
        ok = resolve_thread("PRRT_xxx")
    assert ok is True


def test_resolve_thread_not_resolved_returns_false():
    payload = {"data": {"resolveReviewThread": {"thread": {"id": "PRRT_xxx", "isResolved": False}}}}
    fake = _make_completed_process(json.dumps(payload))
    with patch("quikode.github_graphql.subprocess.run", return_value=fake):
        ok = resolve_thread("PRRT_xxx")
    assert ok is False


def test_resolve_thread_error_response_returns_false_no_raise():
    """gh returns nonzero (e.g. permission error) → False, no raise."""
    fake = _make_completed_process("", returncode=1, stderr="permission denied")
    with patch("quikode.github_graphql.subprocess.run", return_value=fake):
        ok = resolve_thread("PRRT_xxx")
    assert ok is False


def test_resolve_thread_bad_json_returns_false():
    fake = _make_completed_process("garbage")
    with patch("quikode.github_graphql.subprocess.run", return_value=fake):
        ok = resolve_thread("PRRT_xxx")
    assert ok is False


def test_resolve_thread_empty_id_returns_false():
    ok = resolve_thread("")
    assert ok is False


def test_resolve_thread_subprocess_oserror_returns_false():
    with patch("quikode.github_graphql.subprocess.run", side_effect=OSError("gh missing")):
        ok = resolve_thread("PRRT_xxx")
    assert ok is False
