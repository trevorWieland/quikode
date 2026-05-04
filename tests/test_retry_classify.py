"""Unit tests for `quikode.retry_classify`.

Pattern matchers + classify_retry hint logic + histogram aggregation.
"""

from __future__ import annotations

from quikode import retry_classify
from quikode.state import State, Store

# ----- Pattern matching -----


def test_classify_oom_via_rc_137():
    cat, sig = retry_classify.classify_retry(rc=137, stderr="", stdout="")
    assert cat == "container_oom"
    assert "137" in sig


def test_classify_oom_via_message():
    cat, _ = retry_classify.classify_retry(
        rc=1,
        stderr="container OOMKilled",
        stdout="",
    )
    assert cat == "container_oom"


def test_classify_rate_limit_429():
    cat, sig = retry_classify.classify_retry(
        rc=1,
        stderr="HTTP 429: too many requests on /v1/messages",
        stdout="",
    )
    assert cat == "agent_cli_rate_limit"
    assert "429" in sig or "too many requests" in sig.lower()


def test_classify_rate_limit_quota_exceeded():
    cat, _ = retry_classify.classify_retry(
        rc=1,
        stderr="usage limit exceeded; reset at 2026-05-04T18:00",
        stdout="",
    )
    assert cat == "agent_cli_rate_limit"


def test_classify_container_vanished():
    cat, _ = retry_classify.classify_retry(
        rc=1,
        stderr="Error: No such container: qk-r-0019-abc-dev",
        stdout="",
    )
    assert cat == "container_vanished"


def test_classify_network_timeout():
    cat, _ = retry_classify.classify_retry(
        rc=128,
        stderr="fatal: unable to access 'https://github.com/...': Could not resolve host: github.com",
        stdout="",
    )
    assert cat == "network_timeout"


def test_classify_pre_commit_via_pattern():
    cat, _ = retry_classify.classify_retry(
        rc=1,
        stderr="lefthook pre-commit FAIL: ruff returned non-zero",
        stdout="",
    )
    assert cat == "pre_commit_hook_fail"


def test_classify_pre_commit_via_hint():
    """Hint-only path — patterns don't match but caller knows it's pre-commit."""
    cat, _ = retry_classify.classify_retry(
        rc=1,
        stderr="exit 1",
        stdout="",
        hint="pre_commit",
    )
    assert cat == "pre_commit_hook_fail"


def test_classify_checker_timeout():
    cat, _ = retry_classify.classify_retry(
        rc=124,
        stderr="codex: timed out after 600s",
        stdout="",
    )
    assert cat == "checker_timeout"


def test_classify_checker_hint_with_fail_verdict():
    """When the checker emits a structured verdict and the hint is 'checker',
    classify as a real checker_fail."""
    cat, sig = retry_classify.classify_retry(
        rc=0,
        stderr="",
        stdout='{"verdict": "FAIL", "root_cause": "missing handler"}',
        hint="checker",
    )
    assert cat == "checker_fail"
    assert "FAIL" in sig


def test_classify_checker_hint_no_verdict_falls_to_doer_invalid():
    """Checker hint + no recognizable pattern + no FAIL verdict → the doer
    just produced output the checker rejected. Most common 'real' retry."""
    cat, _ = retry_classify.classify_retry(
        rc=1,
        stderr="some random failure detail",
        stdout="",
        hint="checker",
    )
    assert cat == "doer_output_invalid"


def test_classify_unknown_falls_through_to_other():
    cat, _ = retry_classify.classify_retry(rc=1, stderr="weird", stdout="")
    assert cat == "other"


# ----- Histogram + format helpers -----


def test_histogram_aggregates_categories():
    reasons = [
        {"category": "checker_fail"},
        {"category": "checker_fail"},
        {"category": "agent_cli_rate_limit"},
        {"category": "doer_output_invalid"},
        {"category": "checker_fail"},
    ]
    hist = retry_classify.histogram(reasons)
    assert hist["checker_fail"] == 3
    assert hist["agent_cli_rate_limit"] == 1
    assert hist["doer_output_invalid"] == 1


def test_histogram_unknown_categories_bucket_to_other():
    reasons = [{"category": "totally_made_up"}, {"category": "another_bogus"}]
    hist = retry_classify.histogram(reasons)
    assert hist.get("other") == 2


def test_format_histogram_orders_by_count_desc():
    counts = {
        "checker_fail": 5,
        "agent_cli_rate_limit": 1,
        "doer_output_invalid": 3,
    }
    out = retry_classify.format_histogram(counts)
    # checker_fail (5) first, then doer_output_invalid (3), then rate-limit (1)
    parts = out.split()
    assert parts[0].startswith("checker_fail=")
    assert parts[1].startswith("doer_output_invalid=")
    assert parts[2].startswith("agent_cli_rate_limit=")


def test_format_histogram_empty():
    assert retry_classify.format_histogram({}) == ""


# ----- Store integration -----


def test_store_append_retry_reason_round_trip(tmp_path):
    store = Store(tmp_path / "q.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.DOING_SUBTASK)
    store.upsert_subtasks("R-001", [{"subtask_id": "S-01"}])

    store.append_retry_reason(
        "R-001",
        "S-01",
        attempt=1,
        category="checker_fail",
        signature="verdict=FAIL",
    )
    store.append_retry_reason(
        "R-001",
        "S-01",
        attempt=2,
        category="agent_cli_rate_limit",
        signature="429",
        transient=True,
    )

    reasons = store.retry_reasons("R-001", "S-01")
    assert len(reasons) == 2
    assert reasons[0]["category"] == "checker_fail"
    assert reasons[0]["transient"] is False
    assert reasons[1]["category"] == "agent_cli_rate_limit"
    assert reasons[1]["transient"] is True


def test_store_retry_reasons_caps_at_50(tmp_path):
    store = Store(tmp_path / "q.db")
    store.upsert_pending("R-001")
    store.upsert_subtasks("R-001", [{"subtask_id": "S-01"}])

    for i in range(75):
        store.append_retry_reason(
            "R-001",
            "S-01",
            attempt=i,
            category="checker_fail",
            signature=f"sig {i}",
        )

    reasons = store.retry_reasons("R-001", "S-01")
    assert len(reasons) == 50
    # Tail preserved: most recent attempt is i=74.
    assert reasons[-1]["attempt"] == 74
    assert reasons[0]["attempt"] == 25  # 25..74 = 50 entries


def test_store_retry_reasons_handles_missing_column_gracefully(tmp_path):
    """Fresh DB without the column yet (defensive) — append + read are no-op safe."""
    store = Store(tmp_path / "q.db")
    store.upsert_pending("R-001")
    store.upsert_subtasks("R-001", [{"subtask_id": "S-01"}])

    # Empty list when nothing recorded.
    assert store.retry_reasons("R-001", "S-01") == []
