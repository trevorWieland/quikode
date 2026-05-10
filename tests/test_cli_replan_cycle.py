"""Plan 52: `qk replan-cycle <task>` CLI behavior.

Pinned contract:
- Refuses (exit 2) on tasks not in BLOCKED or FAILED.
- Refuses (exit 2) on tasks with no worktree_path on disk.
- Refuses (exit 2) on tasks with no branch recorded.
- Refuses (exit 2) when the latest cycle is 1 ("initial") — operator
  must opt into a full restart explicitly via `qk retry`.
- Unknown task → exit 1.
- Resets every subtask in the latest planning cycle by DELETING the
  rows so the worker's natural fixup / replan / merge flow re-emits
  them at the same cycle ordinal (next emission increments from N-1
  back to N).
- Earlier-cycle subtasks survive untouched (state, retries,
  commit_sha all intact).
- Clears `pre_pr_audit_summary`.
- Sets task state to PENDING with `resume_from_existing_subtasks=1`
  and a `replan_cycle_marker` JSON blob carrying (cycle, kind, ts).
- `--dry-run` makes no DB or git changes; the plan is printed.
- `git reset --hard <target_sha>` and `git push --no-verify
  --force-with-lease` are invoked against the worktree path
  (verified via subprocess monkeypatch).
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


def _put_task_in_blocked_with_two_cycles(tmp_path: Path) -> Path:
    """Build a fixture task with cycle-1 (initial) + cycle-2 (fixup)
    subtasks. Cycle-1: S-01 done @aaa111, S-02 done @bbb222. Cycle-2:
    F-1-1 blocked with retry baggage. Worktree dir is real so the
    validator passes."""
    worktree_path = tmp_path / "worktrees" / "r-001"
    worktree_path.mkdir(parents=True)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.BLOCKED, last_error="fixup cycle stuck")
    store.set_field(
        "R-001",
        branch="quikode/r-001-abc",
        worktree_path=str(worktree_path),
        pre_pr_audit_summary='{"cycle": 2, "stages": []}',
    )
    # Cycle-1 (initial) subtasks: tag explicitly.
    store.upsert_subtasks(
        "R-001",
        [
            {"subtask_id": "S-01-foo", "title": "first", "acceptance": ["a"]},
            {"subtask_id": "S-02-bar", "title": "second", "acceptance": ["b"]},
        ],
        planning_cycle=1,
        planning_kind="initial",
    )
    store.update_subtask("R-001", "S-01-foo", state="done", retries=0, commit_sha="aaa111")
    store.update_subtask("R-001", "S-02-bar", state="done", retries=1, commit_sha="bbb222")
    # Cycle-2 (fixup) subtask appended with explicit cycle/kind.
    store.append_subtasks(
        "R-001",
        [
            {
                "subtask_id": "F-1-1-fix",
                "title": "fix one",
                "acceptance": ["c"],
                "kind": "fixup-pre-pr-audit",
            }
        ],
        planning_cycle=2,
        planning_kind="fixup",
    )
    store.update_subtask(
        "R-001",
        "F-1-1-fix",
        state="blocked",
        retries=49,
        last_error="same-signature stop-loss",
        triage_notes="ROOT_CAUSE: toxic loop",
    )
    store.conn.close()
    return worktree_path


def _put_task_with_only_initial_cycle(tmp_path: Path) -> None:
    """Cycle-1-only fixture: replan-cycle should refuse since there's no
    later cycle to roll back to."""
    worktree_path = tmp_path / "worktrees" / "r-001"
    worktree_path.mkdir(parents=True)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.BLOCKED, last_error="initial cycle stuck")
    store.set_field(
        "R-001",
        branch="quikode/r-001-abc",
        worktree_path=str(worktree_path),
    )
    store.upsert_subtasks(
        "R-001",
        [{"subtask_id": "S-01-foo", "title": "first", "acceptance": ["a"]}],
        planning_cycle=1,
        planning_kind="initial",
    )
    store.update_subtask("R-001", "S-01-foo", state="blocked", retries=49)
    store.conn.close()


def _patch_subprocess_run(monkeypatch, sha: str = "deadbeef0000"):
    """Capture all subprocess.run calls and return canned outputs.

    `git rev-parse <sha>~1` returns `sha`. `git reset --hard ...` and
    `git push ...` succeed. All calls are recorded for assertion.
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
    _put_task_in_blocked_with_two_cycles(tmp_path)
    calls = _patch_subprocess_run(monkeypatch)

    result = CliRunner().invoke(app, ["replan-cycle", "R-001", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Replan-cycle plan for R-001" in result.output
    assert "no changes made" in result.output

    reset_calls = [c for c in calls if "reset" in c]
    push_calls = [c for c in calls if "push" in c]
    assert reset_calls == []
    assert push_calls == []

    store = Store(tmp_path / ".quikode" / "quikode.db")
    assert store.get("R-001")["state"] == "blocked"
    assert store.get_subtask("R-001", "F-1-1-fix")["retries"] == 49
    assert store.get_subtask("R-001", "F-1-1-fix")["state"] == "blocked"
    store.conn.close()


def test_replan_cycle_deletes_only_latest_cycle(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    _put_task_in_blocked_with_two_cycles(tmp_path)
    _patch_subprocess_run(monkeypatch, sha="cafebabe")

    result = CliRunner().invoke(app, ["replan-cycle", "R-001"])
    assert result.exit_code == 0, result.output

    store = Store(tmp_path / ".quikode" / "quikode.db")
    # Cycle-1 subtasks survive untouched.
    s01 = store.get_subtask("R-001", "S-01-foo")
    assert s01["state"] == "done"
    assert s01["commit_sha"] == "aaa111"
    s02 = store.get_subtask("R-001", "S-02-bar")
    assert s02["state"] == "done"
    assert s02["commit_sha"] == "bbb222"
    # Cycle-2 subtask deleted (so the worker's fixup planner re-emits cleanly).
    f = store.get_subtask("R-001", "F-1-1-fix")
    assert f is None
    # Task itself is back at PENDING with the resume + replan markers.
    row = store.get("R-001")
    assert row["state"] == "pending"
    assert row["resume_from_existing_subtasks"] == 1
    assert row["last_error"] is None
    assert row["pre_pr_audit_summary"] is None
    marker = json.loads(row["replan_cycle_marker"])
    assert marker["cycle"] == 2
    assert marker["kind"] == "fixup"
    assert "ts" in marker
    store.conn.close()


def test_replan_cycle_invokes_git_reset_and_push(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    worktree_path = _put_task_in_blocked_with_two_cycles(tmp_path)
    calls = _patch_subprocess_run(monkeypatch, sha="cafebabe")

    result = CliRunner().invoke(app, ["replan-cycle", "R-001"])
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
        "--no-verify",
        "--force-with-lease",
        "origin",
        "quikode/r-001-abc",
    ]


def test_keep_remote_skips_force_push(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    _put_task_in_blocked_with_two_cycles(tmp_path)
    calls = _patch_subprocess_run(monkeypatch)

    result = CliRunner().invoke(app, ["replan-cycle", "R-001", "--keep-remote"])
    assert result.exit_code == 0, result.output

    push_calls = [c for c in calls if "push" in c]
    assert push_calls == []


def test_refuses_on_initial_cycle_only(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    _put_task_with_only_initial_cycle(tmp_path)

    result = CliRunner().invoke(app, ["replan-cycle", "R-001"])
    assert result.exit_code == 2, result.output
    # Rich wraps long lines; collapse whitespace to assert the meaningful phrase.
    flat = " ".join(result.output.split())
    assert "no later cycle to replan" in flat
    assert "qk retry" in flat


def test_refuses_on_running_task(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.DOING_SUBTASK)
    store.conn.close()

    result = CliRunner().invoke(app, ["replan-cycle", "R-001"])
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
    store.upsert_subtasks(
        "R-001",
        [{"subtask_id": "S-10", "title": "x", "acceptance": ["a"]}],
    )
    store.conn.close()

    result = CliRunner().invoke(app, ["replan-cycle", "R-001"])
    assert result.exit_code == 2, result.output
    flat = " ".join(result.output.split())
    assert "exist on disk" in flat


def test_unknown_task_exits_one(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["replan-cycle", "R-DOES-NOT-EXIST"])
    assert result.exit_code == 1
    assert "no such task" in result.output


def test_target_sha_resolved_from_first_cycle_n_commit(tmp_path, monkeypatch):
    """When the first cycle-N row carries a commit_sha, target = sha~1.
    When it doesn't (cycle-N never landed any commits), target = HEAD."""
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    worktree_path = tmp_path / "worktrees" / "r-001"
    worktree_path.mkdir(parents=True)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.BLOCKED)
    store.set_field("R-001", branch="b", worktree_path=str(worktree_path))
    store.upsert_subtasks(
        "R-001",
        [{"subtask_id": "S-01", "title": "a", "acceptance": ["a"]}],
        planning_cycle=1,
        planning_kind="initial",
    )
    store.update_subtask("R-001", "S-01", state="done", commit_sha="prior111")
    # Cycle-2 row that DID commit → target should be its sha~1.
    store.append_subtasks(
        "R-001",
        [{"subtask_id": "F-1-1", "title": "fixup", "acceptance": ["b"]}],
        planning_cycle=2,
        planning_kind="fixup",
    )
    store.update_subtask("R-001", "F-1-1", state="blocked", commit_sha="badbeef0")
    store.conn.close()

    sha_returns = {"badbeef0~1": "predecessor00", "HEAD": "head00000"}
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if "rev-parse" in cmd:
            target = cmd[-1]
            return subprocess.CompletedProcess(
                cmd, 0, stdout=sha_returns.get(target, "fallback00") + "\n", stderr=""
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("quikode.cli_lifecycle.subprocess.run", fake_run)

    result = CliRunner().invoke(app, ["replan-cycle", "R-001"])
    assert result.exit_code == 0, result.output

    rev_parse_calls = [c for c in calls if "rev-parse" in c]
    assert any(c[-1] == "badbeef0~1" for c in rev_parse_calls), rev_parse_calls
    reset_calls = [c for c in calls if "reset" in c]
    assert reset_calls and reset_calls[0][-1] == "predecessor00"


def test_planning_cycle_columns_persisted_on_emit(tmp_path):
    """Initial / append upserts populate planning_cycle + planning_kind."""
    _bootstrap(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.upsert_subtasks(
        "R-001",
        [{"subtask_id": "S-01", "title": "a", "acceptance": ["a"]}],
        planning_cycle=1,
        planning_kind="initial",
    )
    store.append_subtasks(
        "R-001",
        [{"subtask_id": "F-1-1", "title": "b", "acceptance": ["c"]}],
        planning_kind="fixup",
    )
    rows = store.list_subtasks("R-001")
    by_id = {r["subtask_id"]: r for r in rows}
    assert by_id["S-01"]["planning_cycle"] == 1
    assert by_id["S-01"]["planning_kind"] == "initial"
    # Append auto-bumps to MAX(cycle) + 1.
    assert by_id["F-1-1"]["planning_cycle"] == 2
    assert by_id["F-1-1"]["planning_kind"] == "fixup"
    # latest_planning_cycle reads the right tuple.
    assert store.latest_planning_cycle("R-001") == (2, "fixup")
    store.conn.close()


def test_append_with_explicit_cycle_replaces(tmp_path):
    """Re-emission after replan-cycle: passing planning_cycle=N produces
    rows at cycle N (the same number as the prior cycle, since the
    prior rows were deleted by `qk replan-cycle` first)."""
    _bootstrap(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.upsert_subtasks(
        "R-001",
        [{"subtask_id": "S-01", "title": "a", "acceptance": ["a"]}],
        planning_cycle=1,
        planning_kind="initial",
    )
    store.append_subtasks(
        "R-001",
        [{"subtask_id": "F-1-1", "title": "fix", "acceptance": ["c"]}],
        planning_cycle=2,
        planning_kind="fixup",
    )
    # Simulate `qk replan-cycle`: delete cycle-2 rows.
    deleted = store.delete_subtasks_in_cycle("R-001", 2)
    assert deleted == 1
    # Worker re-fires fixup planner; default-path append now sees MAX = 1
    # and emits the regenerated rows at cycle 2 again — same number, not 3.
    store.append_subtasks(
        "R-001",
        [{"subtask_id": "F-1-1-redux", "title": "fix again", "acceptance": ["d"]}],
        planning_kind="fixup",
    )
    rows = store.list_subtasks("R-001")
    redux = next(r for r in rows if r["subtask_id"] == "F-1-1-redux")
    assert redux["planning_cycle"] == 2
    assert redux["planning_kind"] == "fixup"
    store.conn.close()
