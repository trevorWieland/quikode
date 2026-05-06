"""Tests for the net_retry helper.

Behavior we lock in:
- ok on first call → no retries, returns immediately.
- transient on first → backoff, retry; succeed → returns success.
- transient × (retries + 1) → returns last failure (no infinite loop).
- hard failure → no retries, returns immediately.
- gh classifier recognises rate-limit + 5xx + DNS as transient.
- gh classifier treats 401/403/404 + "already exists" as hard.
- git classifier treats lease-stale / non-fast-forward as hard, network as transient.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from quikode import net_retry


def _proc(rc: int, stderr: str = "", stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


# ----- classifier coverage -----


def test_gh_classifier_ok_on_zero():
    assert net_retry.gh_classifier(_proc(0)) == "ok"


def test_gh_classifier_transient_on_429():
    assert net_retry.gh_classifier(_proc(1, "HTTP 429: rate limit exceeded")) == "transient"


def test_gh_classifier_transient_on_secondary_rate_limit():
    assert net_retry.gh_classifier(_proc(1, "You have exceeded a secondary rate limit")) == "transient"


def test_gh_classifier_transient_on_5xx():
    assert net_retry.gh_classifier(_proc(1, "Server Error (502)")) == "transient"


def test_gh_classifier_transient_on_dns():
    assert net_retry.gh_classifier(_proc(1, "Could not resolve host: api.github.com")) == "transient"


def test_gh_classifier_hard_on_404():
    assert net_retry.gh_classifier(_proc(1, "404: Not Found")) == "hard"


def test_gh_classifier_hard_on_403():
    assert net_retry.gh_classifier(_proc(1, "403 Forbidden: requires authentication")) == "hard"


def test_gh_classifier_hard_on_already_exists():
    assert net_retry.gh_classifier(_proc(1, "validation failed: already exists")) == "hard"


def test_gh_classifier_hard_on_unknown():
    """Unknown failures default to hard — better to surface than to loop."""
    assert net_retry.gh_classifier(_proc(1, "something unparseable")) == "hard"


def test_git_classifier_hard_on_non_fast_forward():
    assert net_retry.git_classifier(_proc(1, "! [rejected] foo (non-fast-forward)")) == "hard"


def test_git_classifier_hard_on_stale_lease():
    assert net_retry.git_classifier(_proc(1, "stale info: refs/heads/foo")) == "hard"


def test_git_classifier_transient_on_dns():
    assert net_retry.git_classifier(_proc(128, "fatal: unable to access 'https://...'")) == "transient"


def test_git_classifier_transient_on_rpc_failed():
    assert net_retry.git_classifier(_proc(128, "fatal: RPC failed; early EOF")) == "transient"


# ----- backoff loop -----


def test_run_with_backoff_returns_immediately_on_ok():
    sleeps: list[float] = []

    def fake_run(cmd, **kwargs):
        return _proc(0)

    with patch("quikode.net_retry.subprocess.run", side_effect=fake_run):
        proc = net_retry.run_with_backoff(["gh", "ok"], sleep_fn=sleeps.append)
    assert proc.returncode == 0
    assert sleeps == []


def test_run_with_backoff_retries_on_transient_then_succeeds():
    sleeps: list[float] = []
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _proc(1, "You have exceeded a secondary rate limit")
        return _proc(0)

    with patch("quikode.net_retry.subprocess.run", side_effect=fake_run):
        proc = net_retry.run_with_backoff(["gh", "x"], sleep_fn=sleeps.append, base_delay_s=2.0)
    assert proc.returncode == 0
    assert calls["n"] == 2
    assert sleeps == [2.0]  # one backoff before the retry


def test_run_with_backoff_exponential_schedule():
    sleeps: list[float] = []
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        return _proc(1, "API rate limit exceeded")

    with patch("quikode.net_retry.subprocess.run", side_effect=fake_run):
        proc = net_retry.run_with_backoff(["gh", "x"], retries=3, base_delay_s=2.0, sleep_fn=sleeps.append)
    assert proc.returncode == 1
    assert calls["n"] == 4  # initial + 3 retries
    assert sleeps == [2.0, 4.0, 8.0]


def test_run_with_backoff_hard_failure_no_retry():
    sleeps: list[float] = []
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        return _proc(1, "404 Not Found")

    with patch("quikode.net_retry.subprocess.run", side_effect=fake_run):
        proc = net_retry.run_with_backoff(["gh", "x"], sleep_fn=sleeps.append)
    assert proc.returncode == 1
    assert calls["n"] == 1
    assert sleeps == []


def test_run_with_backoff_uses_passed_classifier():
    """Caller can swap classifiers for git-vs-gh distinction."""
    sleeps: list[float] = []

    def fake_run(cmd, **kwargs):
        return _proc(1, "non-fast-forward rejected")

    with patch("quikode.net_retry.subprocess.run", side_effect=fake_run):
        proc = net_retry.run_with_backoff(
            ["git", "push"], classifier=net_retry.git_classifier, sleep_fn=sleeps.append
        )
    # git classifier marks non-fast-forward as hard → no retry.
    assert proc.returncode == 1
    assert sleeps == []
