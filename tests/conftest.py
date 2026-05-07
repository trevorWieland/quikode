"""Shared test scaffolding.

Plan 28 retired the in-process per-thread review classifier
(`triage.triage_review_threads`). The legacy `_disable_review_classifier`
autouse fixture is now a no-op kept only for backward compatibility with the
`no_classifier_stub` marker some tests still carry. New post-PR tests should
exercise `bundle_pr_context` and the formal-review polling path directly.
"""

from __future__ import annotations


def pytest_configure(config):
    """Register the custom marker so pytest doesn't warn about unknown markers."""
    config.addinivalue_line(
        "markers",
        "no_classifier_stub: legacy marker (plan 28 deleted the classifier; kept for back-compat)",
    )
