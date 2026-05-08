"""Subtask-doer timeout becomes a failed attempt + `quikode resume` flow.

Two related fixes:

1. `agents.base._exec` now catches subprocess.TimeoutExpired and returns a
   synthetic AgentResult(rc=124) instead of raising. The worker treats this
   as a failed attempt → triage → retry, rather than crashing the whole task.

2. `quikode resume <id>` preserves worktree, branch, and subtask state.
   The worker honors `resume_from_existing_subtasks=1` by skipping the
   planner agent and parsing the previously stored plan_text. The subtask
   loop skips DONE rows so already-completed work isn't re-run.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from quikode import worktree as wt
from quikode.agents.base import _exec
from quikode.cli import app
from quikode.config import Config
from quikode.config_template import DEFAULT_CONFIG_TOML
from quikode.state import State, Store
from quikode.types import AgentResult

# ----- timeout → synthetic AgentResult -----


class _StubHandle:
    container_name = "qk-stub"


def test_exec_returns_synthetic_result_on_timeout():
    """The worker relies on _exec swallowing TimeoutExpired into a failed
    AgentResult so subtask retries can fire. If TimeoutExpired bubbles up,
    the whole task fails — that was the production bug we're regression-testing."""

    def fake_exec_in(handle, cmd, log_path=None, stdin=None, timeout=None):
        # Simulate `subprocess.run(timeout=...)` firing
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout or 0, output=b"partial", stderr=b"")

    with patch("quikode.agents.base.exec_in", side_effect=fake_exec_in):
        result = _exec(_StubHandle(), ["bash", "-lc", "echo hi"], timeout=5)
    assert isinstance(result, AgentResult)
    assert result.rc == 124  # standard "timed out" exit code
    assert "timed out after 5s" in result.stderr
    assert result.stdout == "partial"
    assert result.duration_s is not None and result.duration_s >= 0


def test_exec_timeout_writes_to_log(tmp_path):
    log = tmp_path / "task.log"

    def fake_exec_in(handle, cmd, log_path=None, stdin=None, timeout=None):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout or 0)

    with patch("quikode.agents.base.exec_in", side_effect=fake_exec_in):
        _exec(_StubHandle(), ["x"], timeout=10, log_path=log)
    assert "timed out after 10s" in log.read_text()


def test_exec_passthrough_when_no_timeout():
    """Sanity: non-timeout path still returns the rc/stdout/stderr verbatim."""

    def fake_exec_in(handle, cmd, log_path=None, stdin=None, timeout=None):
        return 0, "ok", ""

    with patch("quikode.agents.base.exec_in", side_effect=fake_exec_in):
        result = _exec(_StubHandle(), ["x"], timeout=5)
    assert result.rc == 0
    assert result.stdout == "ok"


# ----- resume: schema + CLI + worker behavior -----


def _bootstrap_workspace(tmp_path) -> None:
    qkdir = tmp_path / ".quikode"
    qkdir.mkdir()
    (qkdir / "config.toml").write_text(
        DEFAULT_CONFIG_TOML.format(repo_path=str(tmp_path), dag_path=str(tmp_path / "dag.json"))
    )
    # Minimal DAG so load_config doesn't choke (it doesn't read the DAG, but
    # CLI commands that do load it will fail without one).
    (tmp_path / "dag.json").write_text(
        json.dumps(
            {
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
        )
    )


def test_tasks_schema_has_resume_column(tmp_path):
    """resume_from_existing_subtasks must be an INTEGER column on tasks
    in the fresh schema."""
    db = tmp_path / "fresh.db"
    Store(db).conn.close()
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    conn.close()
    assert "resume_from_existing_subtasks" in cols


def test_resume_cli_command_sets_flag_and_preserves_state(tmp_path, monkeypatch):
    """`quikode resume R-001` should set the resume marker, leave branch +
    worktree_path intact, and reset non-done subtasks to pending."""
    _bootstrap_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.transition(
        "R-001",
        State.FAILED,
        branch="quikode/r-001-abc",
        worktree_path=str(tmp_path / "wt"),
        plan_text='```json\n{"node_id":"R-001","summary":"x","subtasks":[{"id":"S-01","acceptance":["a"]}],"final_acceptance":["b"]}\n```',
    )
    store.upsert_subtasks(
        "R-001",
        [
            {"subtask_id": "S-01", "title": "domain", "acceptance": ["a"]},
            {"subtask_id": "S-02", "title": "store", "acceptance": ["b"]},
        ],
    )
    store.update_subtask("R-001", "S-01", state="done")
    store.update_subtask("R-001", "S-02", state="doing")
    store.conn.close()

    runner = CliRunner()
    result = runner.invoke(app, ["resume", "R-001"])
    assert result.exit_code == 0, result.output
    assert "resume R-001 → pending" in result.output

    store2 = Store(tmp_path / ".quikode" / "quikode.db")
    row = store2.get("R-001")
    assert row is not None
    assert row["state"] == "pending"
    assert row["resume_from_existing_subtasks"] == 1
    # Branch + worktree path preserved
    assert row["branch"] == "quikode/r-001-abc"
    assert row["worktree_path"] == str(tmp_path / "wt")
    # plan_text preserved
    assert "S-01" in row["plan_text"]
    # Subtask states: S-01 stays done, S-02 reset to pending (was "doing")
    subs = store2.list_subtasks("R-001")
    by_id = {s["subtask_id"]: s["state"] for s in subs}
    assert by_id["S-01"] == "done"
    assert by_id["S-02"] == "pending"
    store2.conn.close()


def test_resume_repends_cascade_skipped_subtasks(tmp_path, monkeypatch):
    """When an upstream subtask blocks, the worker pre-emptively marks all
    pending downstream subtasks as `skipped` (cascade-skip — for visibility,
    not intent). After the upstream block is resolved (e.g. operator fixes
    the underlying bug), `quikode resume` must un-skip those so the loop
    actually picks them up — otherwise downstream work is silently lost."""
    _bootstrap_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.transition(
        "R-001",
        State.BLOCKED,
        plan_text='```json\n{"node_id":"R-001","summary":"x","subtasks":[{"id":"S-01","acceptance":["a"]}],"final_acceptance":["b"]}\n```',
    )
    store.upsert_subtasks(
        "R-001",
        [
            {"subtask_id": "S-01", "title": "domain", "acceptance": ["a"]},
            {"subtask_id": "S-02", "title": "store", "acceptance": ["b"]},
            {"subtask_id": "S-03", "title": "api", "acceptance": ["c"]},
        ],
    )
    # Simulate a runaway-then-recovery scenario: S-01 finally settled DONE,
    # but S-02 / S-03 were cascade-skipped during the block.
    store.update_subtask("R-001", "S-01", state="done")
    store.update_subtask("R-001", "S-02", state="skipped")
    store.update_subtask("R-001", "S-03", state="skipped")
    store.conn.close()

    result = CliRunner().invoke(app, ["resume", "R-001"])
    assert result.exit_code == 0, result.output

    store2 = Store(tmp_path / ".quikode" / "quikode.db")
    by_id = {s["subtask_id"]: s["state"] for s in store2.list_subtasks("R-001")}
    assert by_id["S-01"] == "done"  # done preserved
    assert by_id["S-02"] == "pending"  # cascade-skipped → re-pended
    assert by_id["S-03"] == "pending"  # cascade-skipped → re-pended
    store2.conn.close()


def test_resume_refuses_when_no_plan_text(tmp_path, monkeypatch):
    _bootstrap_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.FAILED)  # no plan_text set
    store.conn.close()

    result = CliRunner().invoke(app, ["resume", "R-001"])
    assert result.exit_code == 1
    assert "no stored plan_text" in result.output


def test_resume_refuses_when_no_subtasks(tmp_path, monkeypatch):
    _bootstrap_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.FAILED, plan_text="some plan text")
    # No subtasks upserted
    store.conn.close()

    result = CliRunner().invoke(app, ["resume", "R-001"])
    assert result.exit_code == 1
    assert "no subtasks" in result.output


def test_resume_refuses_for_unknown_task(tmp_path, monkeypatch):
    _bootstrap_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    Store(tmp_path / ".quikode" / "quikode.db").conn.close()
    result = CliRunner().invoke(app, ["resume", "R-DOES-NOT-EXIST"])
    assert result.exit_code == 1


# ----- timeout default -----


def test_default_subtask_doer_timeout_is_30_minutes():
    """Default is 1800s (30 min). Plan 33 calibration (after the tanren
    deploy where 7 consecutive opencode/glm-5.1 doer calls rc=124'd at
    duration_s ~= 1314s): bumped from 1200s to 1800s because the
    targeted EvaluationContract makes the doer prompt meaningfully
    heavier and smaller models need the headroom to land both the diff
    and the DoerEnvelope JSON before SIGTERM."""
    cfg = Config(repo_path=Path("/tmp"), dag_path=Path("/tmp"))
    assert cfg.subtask_doer_timeout_s == 1800


# ----- --reason flag on manual-action commands -----


def _last_state_log_note(db_path: Path, task_id: str) -> str:
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT note FROM state_log WHERE task_id = ? ORDER BY ts DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else ""


def test_retry_reason_logged_in_state_log(tmp_path, monkeypatch):
    """`quikode retry --reason "<note>"` records the reason in state_log so
    later analysis can correlate retries with their motivation."""
    _bootstrap_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / ".quikode" / "quikode.db"
    store = Store(db)
    store.upsert_pending("R-001")
    store.transition("R-001", State.BLOCKED)
    store.conn.close()

    result = CliRunner().invoke(app, ["retry", "R-001", "--reason", "doer prompt updated"])
    assert result.exit_code == 0, result.output
    assert _last_state_log_note(db, "R-001") == "manual retry: doer prompt updated"


def test_retry_without_reason_uses_default_note(tmp_path, monkeypatch):
    _bootstrap_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / ".quikode" / "quikode.db"
    store = Store(db)
    store.upsert_pending("R-001")
    store.transition("R-001", State.BLOCKED)
    store.conn.close()

    result = CliRunner().invoke(app, ["retry", "R-001"])
    assert result.exit_code == 0, result.output
    assert _last_state_log_note(db, "R-001") == "manual retry"


def test_resume_reason_logged_in_state_log(tmp_path, monkeypatch):
    _bootstrap_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / ".quikode" / "quikode.db"
    store = Store(db)
    store.upsert_pending("R-001")
    store.transition(
        "R-001",
        State.FAILED,
        plan_text='```json\n{"node_id":"R-001","summary":"x","subtasks":[{"id":"S-01","acceptance":["a"]}],"final_acceptance":["b"]}\n```',
    )
    store.upsert_subtasks("R-001", [{"subtask_id": "S-01", "title": "x", "acceptance": ["a"]}])
    store.conn.close()

    result = CliRunner().invoke(app, ["resume", "R-001", "-r", "transient network hang"])
    assert result.exit_code == 0, result.output
    note = _last_state_log_note(db, "R-001")
    assert "manual resume" in note
    assert "transient network hang" in note


def test_abort_reason_logged_in_state_log(tmp_path, monkeypatch):
    _bootstrap_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / ".quikode" / "quikode.db"
    store = Store(db)
    store.upsert_pending("R-001")
    store.conn.close()

    # Patch docker shells so the test doesn't actually talk to docker.
    with (
        patch("quikode.cli.docker_env.list_quikode_containers", return_value=[]),
        patch("quikode.cli.subprocess.run"),
    ):
        result = CliRunner().invoke(app, ["abort", "R-001", "--reason", "user changed scope"])
    assert result.exit_code == 0, result.output
    assert _last_state_log_note(db, "R-001") == "aborted by user: user changed scope"


def test_abort_only_targets_per_task_containers(tmp_path, monkeypatch):
    """Regression for the 2026-05-04 abort-blast-radius bug: aborting R-001
    must NOT touch containers belonging to other tasks (R-002 etc.). The
    previous implementation called `cleanup_all_quikode` which killed every
    qk-* container in the workspace, breaking unrelated in-flight work."""
    _bootstrap_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / ".quikode" / "quikode.db"
    store = Store(db)
    store.upsert_pending("R-001")
    store.upsert_pending("R-002")
    store.conn.close()

    # Three containers across two tasks. Abort R-001 should only kill the
    # two `qk-r-001-*` containers — `qk-r-002-foo-dev` must survive.
    fake_containers = [
        {"id": "1", "name": "qk-r-001-abc-dev", "status": "Up"},
        {"id": "2", "name": "qk-r-001-abc-pg", "status": "Up"},
        {"id": "3", "name": "qk-r-002-foo-dev", "status": "Up"},
    ]
    rm_calls: list[list[str]] = []

    def fake_subprocess_run(cmd, *args, **kwargs):
        rm_calls.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("quikode.cli.docker_env.list_quikode_containers", return_value=fake_containers),
        patch("quikode.cli.subprocess.run", side_effect=fake_subprocess_run),
    ):
        result = CliRunner().invoke(app, ["abort", "R-001"])

    assert result.exit_code == 0, result.output
    # Only R-001's containers were touched.
    targeted = sorted(c[-1] for c in rm_calls if c[:3] == ["docker", "rm", "-f"])
    assert targeted == ["qk-r-001-abc-dev", "qk-r-001-abc-pg"]
    # R-002's container was NOT in any rm call.
    assert all("qk-r-002" not in (c[-1] if c else "") for c in rm_calls)


# ----- worktree.add_worktree idempotent on existing path -----


def test_add_worktree_reuses_existing_registered_path(tmp_path):
    """Resume relies on add_worktree being idempotent when the path is
    already a registered worktree. Without this guard, resume would crash
    in _provision."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "x").write_text("x")
    subprocess.run(["git", "add", "x"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    # Stand up a fake `origin` remote pointing at this repo so add_worktree's
    # `origin/main` reference resolves. Mirrors how quikode runs against a
    # real upstream.
    subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=repo, check=True)
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=repo, check=True)

    wt_path = tmp_path / "wt"
    wt.add_worktree(repo, wt_path, "feature-x", "main", remote="origin")
    # Modify the worktree to simulate in-progress edits
    (wt_path / "edit.txt").write_text("in progress")
    # Second call must not crash and must preserve the edit
    wt.add_worktree(repo, wt_path, "feature-x", "main", remote="origin")
    assert (wt_path / "edit.txt").read_text() == "in progress"
