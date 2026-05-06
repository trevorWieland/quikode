"""v3 settled-task notifications: ntfy + slack backends with retry.

Mocks `urllib.request.urlopen` so tests don't make real HTTP calls.
Exercises the multi-channel dispatch, transient retry, and
fallback-to-False paths.
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from quikode.config import Config
from quikode.notify import SettledMessage, notify_settled, ntfy_send, slack_send


def _cfg(tmp_path: Path, **kw: Any) -> Config:
    defaults: dict[str, Any] = {
        "notify_ntfy_url": "https://ntfy.example",
        "notify_ntfy_topic": "quikode-test-secret",
        "notify_slack_webhook_url": "https://hooks.slack.com/services/T/B/X",
    }
    defaults.update(kw)
    return Config(repo_path=tmp_path, dag_path=tmp_path, **defaults)


def _msg() -> SettledMessage:
    return SettledMessage(
        task_id="R-0002",
        title="Create an organization",
        pr_url="https://github.com/x/y/pull/143",
        summary="19 subtasks · 9 review rounds · 20 threads resolved",
        cost_usd=14.20,
    )


def _ok_response() -> MagicMock:
    m = MagicMock()
    m.__enter__ = lambda s: s
    m.__exit__ = lambda *a: None
    m.status = 200
    return m


# ----- ntfy backend -----


def test_ntfy_send_posts_to_topic_url(tmp_path):
    cfg = _cfg(tmp_path)
    msg = _msg()
    captured: list[dict] = []

    def fake_urlopen(req, timeout=None):
        captured.append(
            {
                "url": req.full_url,
                "method": req.get_method(),
                "headers": dict(req.headers),
                "body": req.data.decode("utf-8") if req.data else "",
            }
        )
        return _ok_response()

    with patch("quikode.notify.urllib.request.urlopen", side_effect=fake_urlopen):
        ok = ntfy_send(cfg, msg)
    assert ok is True
    assert len(captured) == 1
    c = captured[0]
    assert c["url"] == "https://ntfy.example/quikode-test-secret"
    assert c["method"] == "POST"
    assert "R-0002" in c["headers"].get("Title", "")
    assert "ready for review" in c["headers"].get("Title", "")
    assert c["headers"].get("Click") == msg.pr_url
    assert "R-0002" in c["body"]
    assert msg.pr_url in c["body"]


def test_ntfy_send_title_is_ascii_only_for_latin1_header(tmp_path):
    """urllib encodes HTTP headers as latin-1, which can't carry the ✅
    emoji. The Title header must be ASCII; the emoji is delivered via
    the `Tags` header (e.g. `white_check_mark`) which the ntfy app
    renders in the push notification UI. Regression for the live
    UnicodeEncodeError on first notify-test."""
    cfg = _cfg(tmp_path)
    captured: list[dict] = []

    def fake_urlopen(req, timeout=None):
        captured.append({"headers": dict(req.headers)})
        return _ok_response()

    with patch("quikode.notify.urllib.request.urlopen", side_effect=fake_urlopen):
        ok = ntfy_send(cfg, _msg())
    assert ok is True
    title = captured[0]["headers"].get("Title", "")
    # Pure ASCII (latin-1 safe).
    title.encode("ascii")
    # No raw ✅.
    assert "✅" not in title
    # Tags header carries the icon shortcode for app-side rendering.
    assert "white_check_mark" in captured[0]["headers"].get("Tags", "")


def test_ntfy_send_skips_when_topic_empty(tmp_path):
    cfg = _cfg(tmp_path, notify_ntfy_topic="")
    msg = _msg()
    with patch("quikode.notify.urllib.request.urlopen") as mock_open:
        ok = ntfy_send(cfg, msg)
    assert ok is False
    mock_open.assert_not_called()


def test_ntfy_send_retries_once_on_transient_then_succeeds(tmp_path):
    cfg = _cfg(tmp_path)
    msg = _msg()
    call_count = {"n": 0}

    def fake_urlopen(req, timeout=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise urllib.error.URLError("connection reset")
        return _ok_response()

    with (
        patch("quikode.notify.urllib.request.urlopen", side_effect=fake_urlopen),
        patch("quikode.notify.time.sleep"),
    ):
        ok = ntfy_send(cfg, msg)
    assert ok is True
    assert call_count["n"] == 2


def test_ntfy_send_returns_false_on_persistent_failure(tmp_path):
    cfg = _cfg(tmp_path)
    msg = _msg()

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("network unreachable")

    with (
        patch("quikode.notify.urllib.request.urlopen", side_effect=fake_urlopen),
        patch("quikode.notify.time.sleep"),
    ):
        ok = ntfy_send(cfg, msg)
    assert ok is False


def test_ntfy_send_returns_false_on_4xx(tmp_path):
    cfg = _cfg(tmp_path)
    msg = _msg()
    bad = MagicMock()
    bad.__enter__ = lambda s: s
    bad.__exit__ = lambda *a: None
    bad.status = 403

    with (
        patch("quikode.notify.urllib.request.urlopen", return_value=bad),
        patch("quikode.notify.time.sleep"),
    ):
        ok = ntfy_send(cfg, msg)
    assert ok is False


# ----- slack backend -----


def test_slack_send_posts_json_text(tmp_path):
    cfg = _cfg(tmp_path)
    msg = _msg()
    captured: list[dict] = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        captured.append({"url": req.full_url, "body": body, "headers": dict(req.headers)})
        return _ok_response()

    with patch("quikode.notify.urllib.request.urlopen", side_effect=fake_urlopen):
        ok = slack_send(cfg, msg)
    assert ok is True
    assert captured[0]["url"] == "https://hooks.slack.com/services/T/B/X"
    assert captured[0]["headers"].get("Content-type") == "application/json"
    text = captured[0]["body"]["text"]
    assert "R-0002" in text
    assert "ready for review" in text
    assert "$14.20" in text
    # Slack-style link: <url|label>
    assert f"<{msg.pr_url}|R-0002>" in text
    # Slack uses :emoji: shortcodes (not raw Unicode)
    assert ":white_check_mark:" in text


def test_slack_send_skips_when_url_empty(tmp_path):
    cfg = _cfg(tmp_path, notify_slack_webhook_url="")
    msg = _msg()
    with patch("quikode.notify.urllib.request.urlopen") as mock_open:
        ok = slack_send(cfg, msg)
    assert ok is False
    mock_open.assert_not_called()


# ----- multi-channel dispatch -----


def test_notify_settled_none_channel_is_noop(tmp_path):
    cfg = _cfg(tmp_path, notify_settled_channel="none")
    with patch("quikode.notify.urllib.request.urlopen") as mock_open:
        ok = notify_settled(cfg, _msg())
    assert ok is False
    mock_open.assert_not_called()


def test_notify_settled_ntfy_channel_only_calls_ntfy(tmp_path):
    cfg = _cfg(tmp_path, notify_settled_channel="ntfy")
    calls: list[str] = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _ok_response()

    with patch("quikode.notify.urllib.request.urlopen", side_effect=fake_urlopen):
        ok = notify_settled(cfg, _msg())
    assert ok is True
    assert len(calls) == 1
    assert "ntfy.example" in calls[0]


def test_notify_settled_both_calls_both_backends(tmp_path):
    cfg = _cfg(tmp_path, notify_settled_channel="both")
    calls: list[str] = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _ok_response()

    with patch("quikode.notify.urllib.request.urlopen", side_effect=fake_urlopen):
        ok = notify_settled(cfg, _msg())
    assert ok is True
    assert len(calls) == 2
    assert any("ntfy.example" in c for c in calls)
    assert any("hooks.slack.com" in c for c in calls)


def test_notify_settled_returns_true_when_one_backend_succeeds(tmp_path):
    """If ntfy fails but slack succeeds (or vice versa), notify_settled
    returns True so the daemon stamps last_notified_settled_ts. The whole
    point of `both` is redundancy."""
    cfg = _cfg(tmp_path, notify_settled_channel="both")
    call_count = {"n": 0}

    def fake_urlopen(req, timeout=None):
        call_count["n"] += 1
        if "ntfy" in req.full_url:
            raise urllib.error.URLError("ntfy down")
        return _ok_response()

    with (
        patch("quikode.notify.urllib.request.urlopen", side_effect=fake_urlopen),
        patch("quikode.notify.time.sleep"),
    ):
        ok = notify_settled(cfg, _msg())
    assert ok is True


def test_notify_settled_returns_false_when_all_backends_fail(tmp_path):
    cfg = _cfg(tmp_path, notify_settled_channel="both")

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("everything is down")

    with (
        patch("quikode.notify.urllib.request.urlopen", side_effect=fake_urlopen),
        patch("quikode.notify.time.sleep"),
    ):
        ok = notify_settled(cfg, _msg())
    assert ok is False
