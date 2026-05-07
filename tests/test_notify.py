"""Plan 30 ntfy.sh delivery tests.

Covers happy path, missing-topic skip, http failure tolerance, and headers.
We patch `urllib.request.urlopen` directly to avoid actual network I/O.
"""

from __future__ import annotations

import urllib.error
from contextlib import contextmanager
from unittest.mock import patch

from quikode.notify import ReviewReadyMessage, notify_review_ready


class _FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _msg(**overrides: str) -> ReviewReadyMessage:
    base: dict[str, str] = {
        "task_id": "R-0001",
        "title": "Add notification preferences",
        "pr_url": "https://github.com/x/y/pull/42",
        "summary": "settled 18min · 2 review round(s) · CI green",
    }
    base.update(overrides)
    return ReviewReadyMessage(
        task_id=base["task_id"],
        title=base["title"],
        pr_url=base["pr_url"],
        summary=base["summary"],
    )


def test_notify_review_ready_no_topic_returns_false():
    """Empty topic → caller hasn't configured ntfy → no-op + False."""
    assert (
        notify_review_ready(
            ntfy_url="https://ntfy.sh",
            ntfy_topic="",
            msg=_msg(),
        )
        is False
    )
    assert (
        notify_review_ready(
            ntfy_url="https://ntfy.sh",
            ntfy_topic="   ",
            msg=_msg(),
        )
        is False
    )


def test_notify_review_ready_happy_path_posts_to_topic_url():
    captured_url: list[str] = []
    captured_headers: list[dict[str, str]] = []
    captured_body: list[str] = []

    @contextmanager
    def _stub(req, timeout):
        del timeout
        captured_url.append(req.full_url)
        captured_headers.append(dict(req.header_items()))
        captured_body.append(req.data.decode("utf-8") if req.data else "")
        yield _FakeResponse(status=200)

    with patch("urllib.request.urlopen", _stub):
        ok = notify_review_ready(
            ntfy_url="https://ntfy.sh/",  # trailing slash gets normalized
            ntfy_topic="quikode-test",
            msg=_msg(),
        )
    assert ok is True
    assert captured_url[0] == "https://ntfy.sh/quikode-test"
    assert captured_headers[0].get("Title") == "R-0001: ready for review"
    assert captured_headers[0].get("Click") == "https://github.com/x/y/pull/42"
    assert "settled 18min" in captured_body[0]
    assert "https://github.com/x/y/pull/42" in captured_body[0]


def test_notify_review_ready_http_non_2xx_returns_false():
    @contextmanager
    def _stub(req, timeout):
        del req, timeout
        yield _FakeResponse(status=429)

    with patch("urllib.request.urlopen", _stub):
        ok = notify_review_ready(
            ntfy_url="https://ntfy.sh",
            ntfy_topic="quikode-test",
            msg=_msg(),
        )
    assert ok is False


def test_notify_review_ready_url_error_returns_false_without_raising():
    def _stub(req, timeout):
        del req, timeout
        raise urllib.error.URLError("connection refused")

    with patch("urllib.request.urlopen", _stub):
        ok = notify_review_ready(
            ntfy_url="https://ntfy.sh",
            ntfy_topic="quikode-test",
            msg=_msg(),
        )
    assert ok is False


def test_notify_review_ready_no_pr_url_skips_click_header():
    captured_headers: list[dict[str, str]] = []

    @contextmanager
    def _stub(req, timeout):
        del timeout
        captured_headers.append(dict(req.header_items()))
        yield _FakeResponse(status=200)

    with patch("urllib.request.urlopen", _stub):
        ok = notify_review_ready(
            ntfy_url="https://ntfy.sh",
            ntfy_topic="quikode-test",
            msg=_msg(pr_url=""),
        )
    assert ok is True
    assert "Click" not in captured_headers[0]
