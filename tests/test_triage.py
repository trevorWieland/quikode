"""Unit tests for `quikode.triage`.

Covers the CI log parser (deterministic, pure-python) and the review-thread
classifier scaffolding (subprocess invocation + envelope parsing). The
classifier's actual sonnet calls are stubbed via the conftest auto-fixture
in higher-level tests; here we exercise the helpers directly.
"""

from __future__ import annotations

import time

import pytest

from quikode import triage
from quikode.config import Config
from quikode.github_graphql import ReviewThread

# Opt out of the conftest's triage_review_threads autostub — these tests
# exercise the real classifier code paths with their own monkeypatches.
pytestmark = pytest.mark.no_classifier_stub


# ----- CI log parser -----


def test_parse_ci_failure_cargo_compile():
    log = """
   Compiling tanren-store v0.1.0
error[E0599]: no method named `find_by_id` found for struct `OrgRepo`
   --> crates/tanren-store/src/records.rs:142:18
    |
142 |         OrgRepo::find_by_id(id).await
    |                  ^^^^^^^^^^ method not found

error: aborting due to previous error
"""
    failures = triage.parse_ci_failure(log)
    assert len(failures) >= 1
    f = failures[0]
    assert f.kind == "compile"
    assert f.file == "crates/tanren-store/src/records.rs"
    assert f.line == 142
    assert "no method named" in f.message


def test_parse_ci_failure_clippy_lint():
    log = """
warning: unused variable: `x`
   --> src/lib.rs:42:5
    |
42  |     let x = 1;
    |         ^

warning: 1 warning emitted
"""
    failures = triage.parse_ci_failure(log)
    # Could be matched as either compile or lint depending on order; test
    # extracts the right file/line either way.
    assert any(f.file == "src/lib.rs" and f.line == 42 for f in failures)


def test_parse_ci_failure_ruff():
    log = """
quikode/state.py:42:1: F401 `os` imported but unused
quikode/state.py:55:80: E501 line too long (110 > 100)
"""
    failures = triage.parse_ci_failure(log)
    files = {(f.file, f.line) for f in failures}
    assert ("quikode/state.py", 42) in files
    assert ("quikode/state.py", 55) in files
    # Both are tagged as lint (ruff codes start with letter+digit).
    assert all(f.kind == "lint" for f in failures)


def test_parse_ci_failure_pytest_assertion():
    log = """
============================= FAILURES =============================
________ test_thing ________
tests/test_thing.py:42: in test_thing
    assert x == 1
AssertionError: assert 0 == 1
============================= short test summary info =============================
FAILED tests/test_thing.py::test_thing - AssertionError: assert 0 == 1
"""
    failures = triage.parse_ci_failure(log)
    assert any(f.kind == "test" for f in failures)


def test_parse_ci_failure_empty_returns_empty():
    assert triage.parse_ci_failure("") == []
    assert triage.parse_ci_failure("just some prose with no errors") == []


def test_parse_ci_failure_caps_at_25():
    """Pathological logs with hundreds of identical errors don't blow up the planner input."""
    # Use ruff format — most concise per-line failure pattern that matches
    # _PATTERN_RUFF deterministically.
    log = "\n".join(f"path/file_{i:03d}.py:{i}:1: F401 unused import {i}" for i in range(100))
    failures = triage.parse_ci_failure(log)
    assert len(failures) == 25


# ----- Classifier envelope parser -----


def test_parse_classifier_envelope_correct_verdict():
    raw = """
{
  "type": "result",
  "result": "{\\"verdict\\": \\"correct\\", \\"rationale\\": \\"missing test\\", \\"reply\\": \\"\\"}"
}
"""
    v = triage._parse_classifier_envelope(raw)
    assert v is not None
    assert v.verdict == "correct"
    assert "missing test" in v.rationale


def test_parse_classifier_envelope_incorrect_with_reply():
    raw = """
{
  "type": "result",
  "result": "{\\"verdict\\": \\"incorrect\\", \\"rationale\\": \\"out of scope\\", \\"reply\\": \\"This file is intentionally unchanged in this slice; the spec defers it to a later subtask.\\"}"
}
"""
    v = triage._parse_classifier_envelope(raw)
    assert v is not None
    assert v.verdict == "incorrect"
    assert "intentionally unchanged" in v.reply


def test_parse_classifier_envelope_bare_inner_json():
    """Sonnet sometimes ignores the 'just JSON' instruction and emits prose
    around the JSON object. Parser should still find it."""
    raw = """
{
  "type": "result",
  "result": "Here is my analysis. {\\"verdict\\": \\"needs_discussion\\", \\"rationale\\": \\"depends on intent\\", \\"reply\\": \\"\\"}"
}
"""
    v = triage._parse_classifier_envelope(raw)
    assert v is not None
    assert v.verdict == "needs_discussion"


def test_parse_classifier_envelope_garbage_returns_none():
    assert triage._parse_classifier_envelope("") is None
    assert triage._parse_classifier_envelope("not json at all") is None
    # Non-verdict JSON.
    assert triage._parse_classifier_envelope('{"foo": "bar"}') is None


# ----- triage_review_threads end-to-end (with classifier mocked) -----


def _thread(tid: str, body: str = "Consider X.") -> ReviewThread:
    return ReviewThread(
        thread_id=tid,
        is_resolved=False,
        is_outdated=False,
        path="src/x.py",
        line=10,
        last_comment_id=f"PRRC_{tid}",
        last_comment_database_id=12345,
        last_comment_author="reviewer",
        last_comment_body=body,
        last_comment_created_at=time.time() - 60,
        last_comment_is_bot=False,
    )


@pytest.fixture
def cfg(tmp_path):
    return Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json")


def test_triage_split_correct_incorrect_discussion(monkeypatch, cfg):
    """Classifier returns each verdict for a different thread; outcome
    splits them into the right buckets."""
    threads = [_thread("T1"), _thread("T2"), _thread("T3")]
    verdict_map = {
        "T1": triage.ReviewVerdict("T1", "correct", "real bug", ""),
        "T2": triage.ReviewVerdict("T2", "incorrect", "out of scope", "This is intentionally unchanged."),
        "T3": triage.ReviewVerdict("T3", "needs_discussion", "design choice", ""),
    }

    def _stub(*, thread, **kw):
        return verdict_map[thread.thread_id]

    monkeypatch.setattr(triage, "classify_review_thread", _stub)

    outcome = triage.triage_review_threads(cfg=cfg, plan_text="", threads=threads)
    assert [t.thread_id for t in outcome.actionable_threads] == ["T1"]
    assert [t.thread_id for t, _ in outcome.auto_resolved] == ["T2"]
    assert [t.thread_id for t, _ in outcome.deferred] == ["T3"]
    assert outcome.classifier_errors == 0


def test_triage_falls_through_on_classifier_error(monkeypatch, cfg):
    """If the classifier returns None (subprocess error, parse fail), the
    thread defaults to actionable so we never silently drop work."""
    threads = [_thread("T1"), _thread("T2")]
    monkeypatch.setattr(triage, "classify_review_thread", lambda **kw: None)

    outcome = triage.triage_review_threads(cfg=cfg, plan_text="", threads=threads)
    assert {t.thread_id for t in outcome.actionable_threads} == {"T1", "T2"}
    assert outcome.classifier_errors == 2


def test_triage_skips_resolved_threads(monkeypatch, cfg):
    """Resolved threads aren't classified — they're already done."""
    threads = [_thread("T1"), _thread("T2")]
    threads[1] = threads[1].model_copy(update={"is_resolved": True})

    calls: list[str] = []

    def _stub(*, thread, **kw):
        calls.append(thread.thread_id)
        return triage.ReviewVerdict(thread.thread_id, "correct", "", "")

    monkeypatch.setattr(triage, "classify_review_thread", _stub)
    outcome = triage.triage_review_threads(cfg=cfg, plan_text="", threads=threads)
    assert calls == ["T1"]  # T2 skipped
    assert [t.thread_id for t in outcome.actionable_threads] == ["T1"]
