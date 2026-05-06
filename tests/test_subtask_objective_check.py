"""Tests for the v3.7 layered subtask gate: `cfg.subtask_check_command`
runs as Layer-1 BEFORE the LLM checker. Layer-1 failure short-circuits
to a synthesized FAIL outcome (no LLM call); pass proceeds to the LLM
checker as before.

Why this gate exists: the LLM checker explicitly does NOT run `just ci`
(too slow per subtask), so workspace-wide compile errors, line-budget
violations, and lint regressions accumulate undetected across the
subtask DAG until `local_ci_command` fires post-DAG. R-0021 burned 10
subtasks before the audit stage caught a missing `check_host_access`
method that any `cargo check` would have flagged at S-04. The layered
gate catches that class of failure at the slice boundary.
"""

from __future__ import annotations

import subprocess as sp
from typing import Any
from unittest.mock import MagicMock, patch

from quikode.config import Config
from quikode.subtask_schema import Subtask
from quikode.types import Verdict
from quikode.worker import TaskWorker


def _stub_subtask() -> Subtask:
    return Subtask(
        id="S-01",
        title="x",
        depends_on=(),
        files_to_touch=("foo.rs",),
        boundary="",
        acceptance=("compiles",),
        notes="",
    )


def _make_worker(cfg: Config) -> Any:
    """Construct a TaskWorker with just enough wiring for `_run_subtask_check_command`."""
    w = TaskWorker.__new__(TaskWorker)  # bypass __init__ — we don't need a real container
    w.cfg = cfg
    w.node = MagicMock()
    w.node.id = "R-0001"
    w.handle = MagicMock()
    w.handle.container_name = "qk-stub"
    w.log_path = None
    w.store = MagicMock()
    return w


def test_objective_gate_skipped_when_command_empty(tmp_path):
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json", subtask_check_command="")
    w = _make_worker(cfg)
    out = w._run_subtask_check_command(_stub_subtask())
    assert out is None  # caller proceeds to LLM checker


def test_objective_gate_pass_returns_none(tmp_path):
    """rc=0 → None → caller proceeds to LLM checker."""
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json", subtask_check_command="just check")
    w = _make_worker(cfg)
    with patch("quikode.worker.exec_in", return_value=(0, "all checks pass\n", "")):
        out = w._run_subtask_check_command(_stub_subtask())
    assert out is None


def test_objective_gate_fail_synthesizes_checker_fail(tmp_path):
    """rc!=0 → FAIL `_CheckerOutcome` carrying the command output as
    triage feedback; no LLM call needed."""
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json", subtask_check_command="just check")
    w = _make_worker(cfg)
    fake_output = (
        "==> line budget\n"
        "FAIL: crates/tanren-identity-policy/src/lib.rs has 667 lines (max 500)\n"
        "FAIL: crates/tanren-cli-app/src/lib.rs has 579 lines (max 500)\n"
    )
    with patch("quikode.worker.exec_in", return_value=(1, fake_output, "")):
        out = w._run_subtask_check_command(_stub_subtask())
    assert out is not None
    assert out.verdict is Verdict.FAIL
    assert out.transient is False
    assert out.rc == 1
    assert "line budget" in out.checker_text
    assert "667 lines" in out.checker_text
    assert "ROOT_CAUSE: objective subtask check" in out.checker_text
    # Structured artifact recorded for `quikode show`.
    w.store.add_artifact.assert_called_once()
    args, _kwargs = w.store.add_artifact.call_args
    assert args[1] == "subtask_objective_check:S-01"


def test_objective_gate_timeout_returns_transient(tmp_path):
    """The exec_in path can raise TimeoutExpired or OSError — both should
    return a transient FAIL so the caller free-retries instead of burning
    the attempt budget."""
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json", subtask_check_command="just check")
    w = _make_worker(cfg)
    with patch("quikode.worker.exec_in", side_effect=sp.TimeoutExpired("just check", 300)):
        out = w._run_subtask_check_command(_stub_subtask())
    assert out is not None
    assert out.transient is True
    assert out.rc == 124


def test_objective_gate_records_artifact_on_fail(tmp_path):
    """The full check-command output should land on
    `subtask_objective_check:<id>` so `quikode show` surfaces it
    distinctly from the LLM checker artifact."""
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json", subtask_check_command="just check")
    w = _make_worker(cfg)
    fake_blob = "==> line budget\nFAIL: x.rs 600/500\n"
    with patch("quikode.worker.exec_in", return_value=(1, fake_blob, "")):
        out = w._run_subtask_check_command(_stub_subtask())
    assert out is not None
    # add_artifact called with the full blob (truncated to 20000)
    args, _kwargs = w.store.add_artifact.call_args
    assert args[2] == fake_blob[:20000]
