"""Shared test scaffolding.

Auto-fixtures applied to every test:

- `_disable_review_classifier`: bypass the in-process sonnet review-thread
  classifier wired into `_poll_review_threads`. The real classifier shells
  out to `claude -p`, which (a) requires authentication and a binary not
  guaranteed in CI, (b) is non-deterministic per the model's reply, and
  (c) takes seconds per thread. Tests that exercise the orchestrator's
  scheduling logic don't want any of that — they want to assert "given
  these threads, is the right future scheduled?" with deterministic input.

  The fixture replaces `quikode.triage.triage_review_threads` with a stub
  that mirrors the legacy "all unresolved threads are actionable" behavior.
  Tests that specifically want to verify classifier wiring should patch
  `triage_review_threads` themselves to override.
"""

from __future__ import annotations

import pytest

from quikode import triage as triage_mod


@pytest.fixture(autouse=True)
def _disable_review_classifier(monkeypatch, request):
    """Default: stub out the classifier; mirror legacy "all unresolved → actionable" semantics.

    Tests in `test_triage.py` (which exercise the classifier internals
    directly) opt out via the file-level `pytestmark = pytest.mark.no_classifier_stub`.
    """
    if request.node.get_closest_marker("no_classifier_stub"):
        yield
        return

    def _passthrough_triage(
        *, cfg, plan_text, threads, recent_diff_excerpt="", classifier_timeout_total_s=120.0
    ):
        del cfg, plan_text, recent_diff_excerpt, classifier_timeout_total_s
        return triage_mod.TriageOutcome(actionable_threads=list(threads))

    monkeypatch.setattr(triage_mod, "triage_review_threads", _passthrough_triage)
    yield


def pytest_configure(config):
    """Register the custom marker so pytest doesn't warn about unknown markers."""
    config.addinivalue_line(
        "markers",
        "no_classifier_stub: opt out of the conftest's triage_review_threads autostub",
    )
