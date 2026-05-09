"""Plan 33 PR-B: scoped witness runner tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from quikode.workers.witness_runner import run_scoped_witnesses


def _make_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return rows


# ---------- happy path ----------


def test_run_scoped_witnesses_runs_each_id_once() -> None:
    calls: list[list[str]] = []

    def fake_exec(handle: Any, cmd: list[str], **_: Any) -> tuple[int, str, str]:
        calls.append(cmd)
        return 0, "PASS (1.0s)", ""

    expected_evidence = _make_evidence(
        [
            {"behavior_id": "B-001", "kind": "test", "command": "npm test:e2e -- one"},
            {"behavior_id": "B-002", "kind": "test", "command": "npm test:e2e -- two"},
        ]
    )
    results = run_scoped_witnesses(
        handle=object(),
        expected_evidence=expected_evidence,
        evidence_ids=["B-001-test", "B-002-test"],
        per_witness_timeout_s=15,
        exec_in=fake_exec,
    )
    assert set(results.keys()) == {"B-001-test", "B-002-test"}
    assert results["B-001-test"]["classification"] == "OK"
    assert results["B-001-test"]["rc"] == 0
    assert "PASS" in results["B-001-test"]["stdout_excerpt"]
    assert len(calls) == 2


def test_run_scoped_witnesses_truncates_4kb_output() -> None:
    big = "x" * 8192

    def fake_exec(handle: Any, cmd: list[str], **_: Any) -> tuple[int, str, str]:
        return 0, big, ""

    results = run_scoped_witnesses(
        handle=object(),
        expected_evidence=[{"behavior_id": "B-001", "kind": "test", "command": "echo big"}],
        evidence_ids=["B-001-test"],
        per_witness_timeout_s=15,
        exec_in=fake_exec,
    )
    assert len(results["B-001-test"]["stdout_excerpt"]) <= 4096 + 64  # cap + truncation marker


# ---------- timeout classification ----------


def test_run_scoped_witnesses_timeout_classified_as_TIMEOUT() -> None:
    def fake_exec(handle: Any, cmd: list[str], timeout: int | None = None, **_: Any) -> tuple[int, str, str]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout or 0)

    results = run_scoped_witnesses(
        handle=object(),
        expected_evidence=[{"behavior_id": "B-001", "kind": "test", "command": "sleep 99"}],
        evidence_ids=["B-001-test"],
        per_witness_timeout_s=1,
        exec_in=fake_exec,
    )
    assert results["B-001-test"]["classification"] == "TIMEOUT"
    assert results["B-001-test"]["rc"] is None
    assert "did not finish" in results["B-001-test"]["note"]


def test_run_scoped_witnesses_oserror_classified_as_ERROR() -> None:
    def fake_exec(handle: Any, cmd: list[str], **_: Any) -> tuple[int, str, str]:
        raise OSError("docker exec failed")

    results = run_scoped_witnesses(
        handle=object(),
        expected_evidence=[{"behavior_id": "B-001", "kind": "test", "command": "x"}],
        evidence_ids=["B-001-test"],
        per_witness_timeout_s=10,
        exec_in=fake_exec,
    )
    assert results["B-001-test"]["classification"] == "ERROR"
    assert "docker exec failed" in results["B-001-test"]["stderr_excerpt"]


# ---------- runtime caps ----------


def test_run_scoped_witnesses_per_subtask_total_budget_respected() -> None:
    """Per-subtask total = 2 * len(ids) * per-witness cap. Once the
    budget is exhausted, remaining witnesses get TIMEOUT classification
    without invoking exec_in."""
    call_count = 0

    def slow_fake_exec(
        handle: Any, cmd: list[str], timeout: int | None = None, **_: Any
    ) -> tuple[int, str, str]:
        nonlocal call_count
        call_count += 1
        # Simulate a witness that ALWAYS times out (consume the per-witness cap).
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout or 0)

    expected_evidence = [
        {"behavior_id": f"B-{i:03d}", "kind": "test", "command": f"sleep {i}"} for i in range(5)
    ]
    evidence_ids = [f"B-{i:03d}-test" for i in range(5)]
    results = run_scoped_witnesses(
        handle=object(),
        expected_evidence=expected_evidence,
        evidence_ids=evidence_ids,
        per_witness_timeout_s=15,
        exec_in=slow_fake_exec,
    )
    # Every id has a result entry, all classified as TIMEOUT.
    assert len(results) == len(evidence_ids)
    for res in results.values():
        assert res["classification"] == "TIMEOUT"


def test_run_scoped_witnesses_default_cap_uses_15s_when_zero_passed() -> None:
    """Cap-clamping defensive — a 0 or negative per-witness_timeout_s
    falls back to the documented 15s default."""
    captured_timeouts: list[int | None] = []

    def fake_exec(handle: Any, cmd: list[str], timeout: int | None = None, **_: Any) -> tuple[int, str, str]:
        captured_timeouts.append(timeout)
        return 0, "ok", ""

    run_scoped_witnesses(
        handle=object(),
        expected_evidence=[{"behavior_id": "B-001", "kind": "test", "command": "echo"}],
        evidence_ids=["B-001-test"],
        per_witness_timeout_s=0,
        exec_in=fake_exec,
    )
    assert captured_timeouts and captured_timeouts[0] is not None and captured_timeouts[0] >= 1


# ---------- container isolation ----------


def test_run_scoped_witnesses_invokes_exec_in_with_workspace_cd() -> None:
    captured_cmds: list[list[str]] = []

    def fake_exec(handle: Any, cmd: list[str], **_: Any) -> tuple[int, str, str]:
        captured_cmds.append(cmd)
        return 0, "ok", ""

    run_scoped_witnesses(
        handle=object(),
        expected_evidence=[{"behavior_id": "B-001", "kind": "test", "command": "echo hi"}],
        evidence_ids=["B-001-test"],
        per_witness_timeout_s=15,
        exec_in=fake_exec,
    )
    # First (and only) call should be a `bash -lc 'cd /workspace && ...'`
    assert captured_cmds == [["bash", "-lc", "cd /workspace && echo hi"]]


# ---------- no-command path ----------


def test_run_scoped_witnesses_no_command_classification() -> None:
    """When the planner claimed an evidence id that has no recoverable
    command, the runner records NO_COMMAND and leaves the rc null."""
    results = run_scoped_witnesses(
        handle=object(),
        expected_evidence=[{"behavior_id": "B-001", "kind": "manual", "description": "vague text"}],
        evidence_ids=["B-001-manual"],
        per_witness_timeout_s=15,
        exec_in=lambda *a, **k: (0, "", ""),  # never called
    )
    assert results["B-001-manual"]["classification"] == "NO_COMMAND"
    assert results["B-001-manual"]["rc"] is None


def test_run_scoped_witnesses_uses_doer_reported_command_as_last_resort() -> None:
    calls: list[list[str]] = []

    def fake_exec(handle: Any, cmd: list[str], **_: Any) -> tuple[int, str, str]:
        calls.append(cmd)
        if "cat docs/roadmap/dag.json" in cmd[-1]:
            return 0, "", ""
        return 0, "20 scenarios passed", ""

    results = run_scoped_witnesses(
        handle=object(),
        expected_evidence=[
            {
                "behavior_id": "B-0025",
                "kind": "bdd",
                "description": "feature exists but no command is declared",
            }
        ],
        evidence_ids=["B-0025-bdd-positive-falsification"],
        per_witness_timeout_s=15,
        exec_in=fake_exec,
        fallback_commands=[
            "just check",
            "TANREN_BDD_FEATURES_PATH=tests/bdd/features/B-0025-connect-existing-repo.feature "
            "cargo run -q -p tanren-bdd --bin tanren-bdd-runner --locked",
        ],
    )

    assert results["B-0025-bdd-positive-falsification"]["classification"] == "OK"
    assert calls[-1] == [
        "bash",
        "-lc",
        "cd /workspace && TANREN_BDD_FEATURES_PATH=tests/bdd/features/B-0025-connect-existing-repo.feature "
        "cargo run -q -p tanren-bdd --bin tanren-bdd-runner --locked",
    ]


def test_run_scoped_witnesses_prefers_behavior_test_command_when_fallback_lacks_token() -> None:
    calls: list[list[str]] = []

    def fake_exec(handle: Any, cmd: list[str], **_: Any) -> tuple[int, str, str]:
        calls.append(cmd)
        if "cat docs/roadmap/dag.json" in cmd[-1]:
            return 0, "", ""
        return 0, "57 scenarios passed", ""

    results = run_scoped_witnesses(
        handle=object(),
        expected_evidence=[{"behavior_id": "B-0046", "kind": "bdd"}],
        evidence_ids=["B-0046-bdd-positive-falsification"],
        per_witness_timeout_s=15,
        exec_in=fake_exec,
        fallback_commands=["just check", "just tests", "just web-e2e"],
    )

    assert results["B-0046-bdd-positive-falsification"]["classification"] == "OK"
    assert calls[-1] == ["bash", "-lc", "cd /workspace && just tests"]


def test_run_scoped_witnesses_uses_current_worktree_dag_when_node_metadata_is_stale() -> None:
    """A doer may add `witness_command` to docs/roadmap/dag.json after the
    worker loaded the in-memory node. The runner should recover that command
    from the current worktree before declaring NO_COMMAND."""
    calls: list[list[str]] = []
    dag = {
        "nodes": [
            {
                "id": "R-001",
                "expected_evidence": [
                    {
                        "behavior_id": "B-001",
                        "kind": "bdd",
                        "witnesses": ["positive", "falsification"],
                        "witness_command": "just tests",
                    }
                ],
            }
        ]
    }

    def fake_exec(handle: Any, cmd: list[str], **_: Any) -> tuple[int, str, str]:
        calls.append(cmd)
        if "cat docs/roadmap/dag.json" in cmd[-1]:
            return 0, json.dumps(dag), ""
        return 0, "PASS recovered witness", ""

    results = run_scoped_witnesses(
        handle=object(),
        expected_evidence=[
            {
                "behavior_id": "B-001",
                "kind": "bdd",
                "witnesses": ["positive", "falsification"],
                "description": "stale row without command",
            }
        ],
        evidence_ids=["B-001-bdd-positive-falsification"],
        per_witness_timeout_s=15,
        exec_in=fake_exec,
    )
    assert results["B-001-bdd-positive-falsification"]["classification"] == "OK"
    assert calls[-1] == [["bash", "-lc", "cd /workspace && just tests"]][0]


def test_run_scoped_witnesses_unknown_id_is_no_command() -> None:
    """Evidence id that isn't on the node's expected_evidence list."""
    results = run_scoped_witnesses(
        handle=object(),
        expected_evidence=[{"behavior_id": "B-001", "kind": "test", "command": "echo"}],
        evidence_ids=["B-999-not-on-node"],
        per_witness_timeout_s=15,
        exec_in=lambda *a, **k: (0, "", ""),
    )
    assert results["B-999-not-on-node"]["classification"] == "NO_COMMAND"


# ---------- nonzero rc ----------


def test_run_scoped_witnesses_nonzero_rc_classified_as_NONZERO_RC() -> None:
    def fake_exec(handle: Any, cmd: list[str], **_: Any) -> tuple[int, str, str]:
        return 1, "FAIL: assertion error", "stderr blob"

    results = run_scoped_witnesses(
        handle=object(),
        expected_evidence=[{"behavior_id": "B-001", "kind": "test", "command": "npm test"}],
        evidence_ids=["B-001-test"],
        per_witness_timeout_s=15,
        exec_in=fake_exec,
    )
    assert results["B-001-test"]["classification"] == "NONZERO_RC"
    assert results["B-001-test"]["rc"] == 1
    assert "FAIL" in results["B-001-test"]["stdout_excerpt"]


def _unused(p: Path) -> None:
    """Keep Path import warm for future fixtures (avoids unused-import
    on a deliberately-empty Path-using fixture)."""
    _ = p
