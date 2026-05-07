"""Plan 27: `qk rewind <task> <subtask>` CLI behavior.

Pinned contract:
- Refuses (exit 2) on tasks not in BLOCKED or FAILED.
- Refuses (exit 2) on tasks with no worktree_path on disk.
- Refuses (exit 2) on tasks with no branch recorded.
- Unknown task → exit 1; unknown subtask → exit 1.
- Resets target + every subtask whose `created_at >= target.created_at` to
  PENDING with cleared retries/triage/last_error/commit_sha/retry_reasons.
- Clears `pre_pr_audit_summary`.
- Sets task state to PENDING with `resume_from_existing_subtasks=1`.
- `--dry-run` makes no DB or git changes; the plan is printed.
- `git reset --hard <target_sha>` and `git push --force-with-lease` are
  invoked against the worktree path (verified via subprocess monkeypatch).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from quikode.cli import app
from quikode.config_template import DEFAULT_CONFIG_TOML
from quikode.state import State, Store


def _bootstrap(tmp_path: Path):
    qkdir = tmp_path / ".quikode"
    qkdir.mkdir()
    (qkdir / "config.toml").write_text(
        DEFAULT_CONFIG_TOML.format(repo_path=str(tmp_path), dag_path=str(tmp_path / "dag.json"))
    )
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


def _put_task_in_blocked_with_subtasks(tmp_path: Path) -> Path:
    """Build a fixture task that's BLOCKED with three subtasks: S-01 done +
    committed, S-02 done + committed, S-10 blocked with retry baggage. Also
    creates a real worktree path so the validator passes the existence check.
    The sha values used are placeholders since the actual `git reset` is
    mocked in each test."""
    worktree_path = tmp_path / "worktrees" / "r-001"
    worktree_path.mkdir(parents=True)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.BLOCKED, last_error="same-signature stop-loss")
    store.set_field(
        "R-001",
        branch="quikode/r-001-abc",
        worktree_path=str(worktree_path),
        pre_pr_audit_summary='{"cycle": 1, "stages": []}',
    )
    store.upsert_subtasks(
        "R-001",
        [
            {"subtask_id": "S-01", "title": "first", "acceptance": ["a"]},
            {"subtask_id": "S-02", "title": "second", "acceptance": ["b"]},
            {"subtask_id": "S-10", "title": "tenth", "acceptance": ["c"]},
        ],
    )
    # S-01 done at t=100, S-02 done at t=200, S-10 blocked at t=300.
    s01_ts, s02_ts, s10_ts = 100.0, 200.0, 300.0
    store.update_subtask("R-001", "S-01", state="done", retries=0, commit_sha="aaa111")
    store.update_subtask("R-001", "S-02", state="done", retries=1, commit_sha="bbb222")
    store.update_subtask(
        "R-001",
        "S-10",
        state="blocked",
        retries=49,
        last_error="same-signature stop-loss",
        triage_notes="ROOT_CAUSE: toxic loop",
    )
    # Override created_at so the topo-after filter has stable values.
    with store.tx() as c:
        c.execute(
            "UPDATE subtasks SET created_at = ? WHERE task_id = ? AND subtask_id = ?",
            (s01_ts, "R-001", "S-01"),
        )
        c.execute(
            "UPDATE subtasks SET created_at = ? WHERE task_id = ? AND subtask_id = ?",
            (s02_ts, "R-001", "S-02"),
        )
        c.execute(
            "UPDATE subtasks SET created_at = ? WHERE task_id = ? AND subtask_id = ?",
            (s10_ts, "R-001", "S-10"),
        )
    store.conn.close()
    return worktree_path


def _patch_subprocess_run(monkeypatch, sha: str = "deadbeef0000"):
    """Capture all subprocess.run calls and return canned outputs.

    `git rev-parse HEAD` and `git rev-parse <sha>~1` return `sha`.
    `git reset --hard ...` and `git push --force-with-lease ...` succeed.
    All calls are recorded into the returned list for assertion.
    """
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=sha + "\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("quikode.cli_lifecycle.subprocess.run", fake_run)
    return calls


def test_dry_run_makes_no_changes(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    _put_task_in_blocked_with_subtasks(tmp_path)
    calls = _patch_subprocess_run(monkeypatch)

    result = CliRunner().invoke(app, ["rewind", "R-001", "S-10", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Rewind plan for R-001" in result.output
    assert "no changes made" in result.output

    # The dry-run resolves target sha (one rev-parse), but does NOT execute
    # git reset / push or any DB mutation.
    reset_calls = [c for c in calls if "reset" in c]
    push_calls = [c for c in calls if "push" in c]
    assert reset_calls == []
    assert push_calls == []

    store = Store(tmp_path / ".quikode" / "quikode.db")
    assert store.get("R-001")["state"] == "blocked"
    assert store.get_subtask("R-001", "S-10")["retries"] == 49
    store.conn.close()


def test_rewind_resets_target_and_topo_after(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    _put_task_in_blocked_with_subtasks(tmp_path)
    _patch_subprocess_run(monkeypatch)

    result = CliRunner().invoke(app, ["rewind", "R-001", "S-10"])
    assert result.exit_code == 0, result.output

    store = Store(tmp_path / ".quikode" / "quikode.db")
    # S-10 was the target, fully reset.
    s10 = store.get_subtask("R-001", "S-10")
    assert s10["state"] == "pending"
    assert s10["retries"] == 0
    assert s10["last_error"] is None
    assert s10["triage_notes"] is None
    assert s10["commit_sha"] is None
    # S-01 / S-02 had created_at < S-10's, so they are preserved untouched.
    s01 = store.get_subtask("R-001", "S-01")
    assert s01["state"] == "done"
    assert s01["commit_sha"] == "aaa111"
    s02 = store.get_subtask("R-001", "S-02")
    assert s02["state"] == "done"
    assert s02["commit_sha"] == "bbb222"
    # Task itself is back at PENDING with the resume marker.
    row = store.get("R-001")
    assert row["state"] == "pending"
    assert row["resume_from_existing_subtasks"] == 1
    assert row["last_error"] is None
    assert row["pre_pr_audit_summary"] is None
    store.conn.close()


def test_rewind_invokes_git_reset_and_push(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    worktree_path = _put_task_in_blocked_with_subtasks(tmp_path)
    calls = _patch_subprocess_run(monkeypatch, sha="cafebabe")

    result = CliRunner().invoke(app, ["rewind", "R-001", "S-10"])
    assert result.exit_code == 0, result.output

    reset_calls = [c for c in calls if c[:4] == ["git", "-C", str(worktree_path), "reset"]]
    assert len(reset_calls) == 1
    assert reset_calls[0] == ["git", "-C", str(worktree_path), "reset", "--hard", "cafebabe"]

    push_calls = [c for c in calls if "push" in c]
    assert len(push_calls) == 1
    assert push_calls[0] == [
        "git",
        "-C",
        str(worktree_path),
        "push",
        "--force-with-lease",
        "origin",
        "quikode/r-001-abc",
    ]


def test_keep_remote_skips_force_push(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    _put_task_in_blocked_with_subtasks(tmp_path)
    calls = _patch_subprocess_run(monkeypatch)

    result = CliRunner().invoke(app, ["rewind", "R-001", "S-10", "--keep-remote"])
    assert result.exit_code == 0, result.output

    push_calls = [c for c in calls if "push" in c]
    assert push_calls == []


def test_refuses_on_running_task(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.DOING_SUBTASK)
    store.conn.close()

    result = CliRunner().invoke(app, ["rewind", "R-001", "S-10"])
    assert result.exit_code == 2, result.output
    assert "doing_subtask" in result.output


def test_refuses_when_no_worktree_on_disk(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.BLOCKED)
    store.set_field(
        "R-001",
        branch="quikode/r-001-abc",
        worktree_path=str(tmp_path / "missing"),
    )
    store.upsert_subtasks("R-001", [{"subtask_id": "S-10", "title": "x", "acceptance": ["a"]}])
    store.conn.close()

    result = CliRunner().invoke(app, ["rewind", "R-001", "S-10"])
    assert result.exit_code == 2, result.output
    # Rich wraps long lines; collapse whitespace to assert the meaningful phrase.
    assert "exist on disk" in " ".join(result.output.split())


def test_unknown_task_exits_one(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["rewind", "R-DOES-NOT-EXIST", "S-10"])
    assert result.exit_code == 1
    assert "no such task" in result.output


def test_unknown_subtask_exits_one(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    _put_task_in_blocked_with_subtasks(tmp_path)
    _patch_subprocess_run(monkeypatch)

    result = CliRunner().invoke(app, ["rewind", "R-001", "S-NOPE"])
    assert result.exit_code == 1
    assert "no subtask 'S-NOPE'" in result.output


def test_target_topo_after_resets_are_inclusive(tmp_path, monkeypatch):
    """Rewinding to S-02 should reset S-02 AND S-10, but leave S-01."""
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    _put_task_in_blocked_with_subtasks(tmp_path)
    _patch_subprocess_run(monkeypatch, sha="cafebabe")

    result = CliRunner().invoke(app, ["rewind", "R-001", "S-02"])
    assert result.exit_code == 0, result.output

    store = Store(tmp_path / ".quikode" / "quikode.db")
    s01 = store.get_subtask("R-001", "S-01")
    assert s01["state"] == "done"
    assert s01["commit_sha"] == "aaa111"
    # S-02 (target) reset.
    s02 = store.get_subtask("R-001", "S-02")
    assert s02["state"] == "pending"
    assert s02["retries"] == 0
    assert s02["commit_sha"] is None
    # S-10 (after target's created_at) reset.
    s10 = store.get_subtask("R-001", "S-10")
    assert s10["state"] == "pending"
    assert s10["retries"] == 0
    store.conn.close()
