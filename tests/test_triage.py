"""Unit tests for `quikode.triage` — CI log parser only.

Plan 28 retired the per-thread review classifier (`classify_review_thread`,
`triage_review_threads`, `ReviewVerdict`, `TriageOutcome`). The classifier's
tests went with it. `parse_ci_failure` survives because the CI-fix path
still extracts structured failures from a CI log to feed the fixup planner.
"""

from __future__ import annotations

from quikode import triage


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
    log = "\n".join(f"path/file_{i:03d}.py:{i}:1: F401 unused import {i}" for i in range(100))
    failures = triage.parse_ci_failure(log)
    assert len(failures) == 25
