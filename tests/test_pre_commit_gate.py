"""v3 Phase A: pre-commit hook gate.

`_pre_commit_gate` runs the configured hook runner against the subtask's
declared files and returns (passed, output). Detection auto-finds
`lefthook.yml` first, then `.pre-commit-config.yaml`. Explicit `none`
always skips. A timeout is treated as a real (non-transient) failure
because a hanging hook is a problem the operator should see.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Literal
from unittest.mock import MagicMock, patch

from quikode.config import Config
from quikode.dag import DAG
from quikode.state import State, Store, SubtaskState
from quikode.subtask_schema import Plan, Subtask
from quikode.types import Verdict
from quikode.worker import (
    TaskWorker,
    _CheckerOutcome,
)
from quikode.worktree import CommitResult


def _build_dag(tmp_path: Path) -> DAG:
    raw = {
        "schema": "test",
        "milestones": [{"id": "M-1", "title": "x", "goal": "x", "status": "planned"}],
        "nodes": [
            {
                "id": "R-001",
                "kind": "behavior",
                "milestone": "M-1",
                "title": "x",
                "scope": "x",
                "depends_on": [],
                "completes_behaviors": [],
                "supports_behaviors": [],
                "boundary_with_neighbors": "",
                "expected_evidence": [],
                "playbook": [],
                "rationale": "",
                "risks": [],
            }
        ],
    }
    p = tmp_path / "dag.json"
    p.write_text(json.dumps(raw))
    return DAG.load(p)


def _build_worker(
    tmp_path: Path,
    *,
    pre_commit_runner: Literal["auto", "lefthook", "pre-commit", "none"] = "auto",
) -> TaskWorker:
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        prompts_dir=tmp_path / "missing-prompts",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
        pre_commit_runner=pre_commit_runner,
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    dag = _build_dag(tmp_path)
    store = Store(cfg.state_dir / "quikode.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.PLANNING)
    store.set_field("R-001", branch="quikode/r-001-abc")
    worker = TaskWorker(cfg, dag, store, dag.nodes["R-001"])
    worker.handle = MagicMock()
    worker.handle.container_name = "qk-stub"
    return worker


def _subtask(files: tuple[str, ...] = ("foo.py", "bar/baz.py")) -> Subtask:
    return Subtask(
        id="S-01",
        title="x",
        depends_on=(),
        files_to_touch=files,
        boundary="",
        acceptance=("x",),
        notes="",
    )


# ----- explicit runner: none -----


def test_runner_none_skips_gate(tmp_path):
    worker = _build_worker(tmp_path, pre_commit_runner="none")
    ok, out = worker._pre_commit_gate(_subtask())
    assert ok is True
    assert out == "skipped"
    worker.store.conn.close()


# ----- explicit runner: lefthook -----


def test_runner_lefthook_pass(tmp_path):
    """lefthook v2 takes file lists via --files-from-stdin (the v1 --files
    flag is rejected as 'flag provided but not defined: -files'). Verify the
    worker uses the stdin form and pipes the planner-declared files in."""
    worker = _build_worker(tmp_path, pre_commit_runner="lefthook")
    captured: list[dict] = []

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        captured.append({"cmd": cmd, "stdin": stdin})
        return 0, "lefthook ok\n", ""

    with patch("quikode.worker.exec_in", side_effect=fake_exec):
        ok, out = worker._pre_commit_gate(_subtask())

    assert ok is True
    assert "lefthook ok" in out
    cmd_str = captured[0]["cmd"][2]
    assert "lefthook run pre-commit --files-from-stdin" in cmd_str
    # File list goes in via stdin, NOT as argv (v1 --files form would be rejected by v2).
    assert "--files " not in cmd_str  # bare --files (no -from-stdin) must not appear
    assert captured[0]["stdin"] is not None
    assert "foo.py" in captured[0]["stdin"]
    assert "bar/baz.py" in captured[0]["stdin"]
    worker.store.conn.close()


def test_runner_lefthook_fail_captures_output(tmp_path):
    worker = _build_worker(tmp_path, pre_commit_runner="lefthook")

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        return 1, "rustfmt: needs reformat\nfoo.rs:12: bad indent\n", "stderr-info"

    with patch("quikode.worker.exec_in", side_effect=fake_exec):
        ok, out = worker._pre_commit_gate(_subtask())

    assert ok is False
    assert "rustfmt" in out
    assert "stderr-info" in out
    worker.store.conn.close()


# ----- explicit runner: pre-commit -----


def test_runner_pre_commit(tmp_path):
    worker = _build_worker(tmp_path, pre_commit_runner="pre-commit")
    captured: list[list[str]] = []

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        captured.append(cmd)
        return 0, "pre-commit ok\n", ""

    with patch("quikode.worker.exec_in", side_effect=fake_exec):
        ok, _out = worker._pre_commit_gate(_subtask())

    assert ok is True
    cmd_str = captured[0][2]
    assert "pre-commit run --files" in cmd_str
    worker.store.conn.close()


# ----- runner: auto -----


def test_auto_detects_lefthook_when_present(tmp_path):
    worker = _build_worker(tmp_path, pre_commit_runner="auto")

    # First call probes lefthook → rc=0; subsequent call runs the hook.
    calls: list[str] = []

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        cmd_str = cmd[2] if len(cmd) >= 3 else " ".join(cmd)
        calls.append(cmd_str)
        if "test -f /workspace/lefthook" in cmd_str:
            return 0, "", ""
        if "test -f /workspace/.pre-commit-config.yaml" in cmd_str:
            return 1, "", ""
        if "lefthook run" in cmd_str:
            return 0, "lefthook ran\n", ""
        return 1, "", "unexpected cmd"

    with patch("quikode.worker.exec_in", side_effect=fake_exec):
        ok, out = worker._pre_commit_gate(_subtask())
    assert ok is True
    assert "lefthook ran" in out
    # We should have probed lefthook and then invoked it (no pre-commit-config probe needed).
    assert any("test -f /workspace/lefthook" in c for c in calls)
    assert any("lefthook run" in c for c in calls)
    worker.store.conn.close()


def test_auto_falls_back_to_pre_commit(tmp_path):
    worker = _build_worker(tmp_path, pre_commit_runner="auto")

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        cmd_str = cmd[2]
        if "lefthook" in cmd_str and "test -f" in cmd_str:
            return 1, "", ""  # no lefthook
        if ".pre-commit-config.yaml" in cmd_str and "test -f" in cmd_str:
            return 0, "", ""  # found pre-commit
        if "pre-commit run" in cmd_str:
            return 0, "ok", ""
        return 99, "", "unexpected"

    with patch("quikode.worker.exec_in", side_effect=fake_exec):
        ok, _ = worker._pre_commit_gate(_subtask())
    assert ok is True
    worker.store.conn.close()


def test_auto_skips_when_no_hook_configured(tmp_path):
    worker = _build_worker(tmp_path, pre_commit_runner="auto")

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        # Both probes return rc=1 (file missing).
        return 1, "", ""

    with patch("quikode.worker.exec_in", side_effect=fake_exec):
        ok, out = worker._pre_commit_gate(_subtask())
    assert ok is True
    assert "no hook" in out.lower()
    worker.store.conn.close()


# ----- timeout -----


def test_timeout_is_real_failure_not_transient(tmp_path):
    """A pre-commit hook that hangs past the timeout should surface as a
    real FAIL (not transient). The operator needs to see the hang."""
    worker = _build_worker(tmp_path, pre_commit_runner="lefthook")

    def fake_exec(handle, cmd, log_path=None, stdin=None, timeout=None):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout or 0)

    with patch("quikode.worker.exec_in", side_effect=fake_exec):
        ok, out = worker._pre_commit_gate(_subtask())

    assert ok is False
    assert "timed out" in out.lower()
    assert "300" in out  # default cfg.pre_commit_timeout_s
    worker.store.conn.close()


def test_no_files_to_touch_skips_gate(tmp_path):
    """A subtask the planner declared with no files_to_touch shouldn't
    have anything to gate against — skip rather than running an empty
    --files invocation."""
    worker = _build_worker(tmp_path, pre_commit_runner="lefthook")
    sub = _subtask(files=())

    # exec_in shouldn't be called at all.
    with patch("quikode.worker.exec_in") as mock_exec:
        ok, out = worker._pre_commit_gate(sub)
    assert ok is True
    assert out == "skipped"
    assert mock_exec.call_count == 0
    worker.store.conn.close()


# ----- end-to-end via _subtask_loop -----


def test_e2e_pre_commit_failure_then_pass_converges(tmp_path):
    """End-to-end: first attempt's hook fails, doer fixes it, second
    attempt's hook passes. retries++ exactly once for the real failure."""
    plan = Plan(
        node_id="R-001",
        summary="x",
        subtasks=(_subtask(),),
        final_acceptance=("ok",),
    )
    worker = _build_worker(tmp_path, pre_commit_runner="lefthook")
    worker.plan = plan
    worker.store.upsert_subtasks(
        "R-001",
        [
            {
                "subtask_id": "S-01",
                "title": "x",
                "depends_on": [],
                "files_to_touch": ["foo.py", "bar/baz.py"],
                "boundary": "",
                "acceptance": ["x"],
                "notes": "",
            }
        ],
    )

    gate_calls = {"n": 0}

    def fake_gate(subtask):
        gate_calls["n"] += 1
        if gate_calls["n"] == 1:
            return False, "rustfmt: needs reformat"
        return True, "ok"

    def fake_commit(handle, subtask, message, *, branch, remote, push, log_path, timeout=300):
        return CommitResult(success=True, commit_sha="abc123", transient=False, output="ok")

    with (
        patch.object(worker, "_do_subtask", side_effect=lambda s, a, t: None),
        patch.object(
            worker,
            "_check_subtask",
            return_value=_CheckerOutcome(
                verdict=Verdict.PASS, checker_text="VERDICT: PASS", transient=False, rc=0, stderr=""
            ),
        ),
        patch.object(worker, "_pre_commit_gate", side_effect=fake_gate),
        patch.object(worker, "_triage_subtask", return_value=("reformat the code", None)),
        patch("quikode.worker.worktree.commit_subtask", side_effect=fake_commit),
    ):
        outcome = worker._subtask_loop()

    assert outcome is None  # converged
    s1 = worker.store.get_subtask("R-001", "S-01")
    assert s1["state"] == SubtaskState.DONE.value
    assert s1["commit_sha"] == "abc123"
    assert (s1["pre_commit_failures"] or 0) == 1
    assert (s1["retries"] or 0) == 1
    # task itself not BLOCKED
    task = worker.store.get("R-001")
    assert task["state"] != State.BLOCKED.value
    worker.store.conn.close()
