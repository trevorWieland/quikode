"""Plan 56: `qk detect-merged` CLI coverage.

The command walks every non-MERGED task and runs `git merge-base
--is-ancestor <branch_tip> origin/main`. Dry-run by default; `--apply`
fires the same FSM call the worker uses for ancestry-matches.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from quikode.cli import app
from quikode.config_template import DEFAULT_CONFIG_TOML
from quikode.state import State, Store


def _bootstrap(tmp_path: Path) -> None:
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


def _seed_three_tasks(tmp_path: Path) -> None:
    """Fixture: R-AHEAD (commits in main), R-BEHIND (commits not in main),
    R-NOBRANCH (no branch recorded), and R-MERGED (already merged — should
    be skipped). Covers the four classification buckets the sweep handles.
    """
    store = Store(tmp_path / ".quikode" / "quikode.db")
    for tid in ("R-AHEAD", "R-BEHIND", "R-NOBRANCH", "R-MERGED"):
        store.upsert_pending(tid)
    store.transition(
        "R-AHEAD",
        State.AWAITING_REVIEW,
        branch="quikode/r-ahead-abc",
        pr_number=11,
        pr_url="https://github.com/owner/repo/pull/11",
    )
    store.transition(
        "R-BEHIND",
        State.AWAITING_REVIEW,
        branch="quikode/r-behind-def",
        pr_number=12,
        pr_url="https://github.com/owner/repo/pull/12",
    )
    store.transition("R-NOBRANCH", State.BLOCKED)
    # R-MERGED needs to be moved through PENDING_CI → AWAITING_REVIEW → MERGED
    # because the FSM has no PENDING → MERGED direct transition except via
    # MARK_MERGED, which we don't want to fire here (it would write extra
    # state_log rows the test would have to filter).
    store.transition("R-MERGED", State.PENDING_CI, pr_number=13)
    store.transition("R-MERGED", State.AWAITING_REVIEW)
    store.transition("R-MERGED", State.MERGED)
    store.conn.close()


def _patch_subprocess(monkeypatch, *, ancestor_branches: set[str]):
    """Stub subprocess.run for git invocations.

    `rev-parse <branch>` returns a fake SHA based on the branch name.
    `merge-base --is-ancestor <tip> origin/main` returns rc=0 iff the
    branch name (parsed back from the resolved tip) is in
    `ancestor_branches`. `fetch` always succeeds. Everything else 0/empty.
    """
    calls: list[list[str]] = []

    def _sha_for(branch: str) -> str:
        return "sha_" + branch.replace("/", "_")

    sha_to_branch = {_sha_for(b): b for b in ancestor_branches | {"quikode/r-behind-def"}}

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        # Strip leading `git -C <path>` envelope for matching.
        try:
            i = cmd.index("git")
        except ValueError:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        sub = cmd[i + 1 :]
        if sub[:1] == ["-C"]:
            sub = sub[2:]
        if sub[:1] == ["fetch"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if sub[:1] == ["rev-parse"]:
            branch = sub[1]
            return subprocess.CompletedProcess(cmd, 0, stdout=_sha_for(branch) + "\n", stderr="")
        if sub[:2] == ["merge-base", "--is-ancestor"]:
            tip = sub[2]
            branch = sha_to_branch.get(tip, "")
            rc = 0 if branch in ancestor_branches else 1
            return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("quikode.cli_detect_merged.subprocess.run", fake_run)
    return calls


def test_dry_run_reports_without_applying(tmp_path, monkeypatch):
    """`qk detect-merged` (no flag) prints a report; no FSM transitions fire."""
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    _seed_three_tasks(tmp_path)
    _patch_subprocess(monkeypatch, ancestor_branches={"quikode/r-ahead-abc"})

    result = CliRunner().invoke(app, ["detect-merged"])
    assert result.exit_code == 0, result.output
    # Report mentions both candidate tasks; merged task is skipped.
    assert "R-AHEAD" in result.output
    assert "R-BEHIND" in result.output
    assert "R-NOBRANCH" in result.output
    assert "R-MERGED" not in result.output
    # Dry-run wording surfaces for the ancestor-match row.
    assert "would mark MERGED" in result.output
    # No transitions fired — R-AHEAD is still AWAITING_REVIEW.
    store = Store(tmp_path / ".quikode" / "quikode.db")
    assert store.get("R-AHEAD")["state"] == State.AWAITING_REVIEW.value
    store.conn.close()


def test_apply_marks_ancestor_match_merged(tmp_path, monkeypatch):
    """`--apply` actually fires `fsm_runtime.mark_merged` for ancestor-matches."""
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    _seed_three_tasks(tmp_path)
    _patch_subprocess(monkeypatch, ancestor_branches={"quikode/r-ahead-abc"})

    result = CliRunner().invoke(app, ["detect-merged", "--apply"])
    assert result.exit_code == 0, result.output
    assert "marked R-AHEAD MERGED" in result.output

    store = Store(tmp_path / ".quikode" / "quikode.db")
    # Ancestor match → MERGED.
    assert store.get("R-AHEAD")["state"] == State.MERGED.value
    # Non-ancestor → untouched.
    assert store.get("R-BEHIND")["state"] == State.AWAITING_REVIEW.value
    # No-branch → untouched.
    assert store.get("R-NOBRANCH")["state"] == State.BLOCKED.value
    # Audit trail carries the ancestry attribution.
    notes = store.conn.execute(
        "SELECT note FROM state_log WHERE task_id = ? AND to_state = ?",
        ("R-AHEAD", State.MERGED.value),
    ).fetchall()
    assert any("ancestry" in (n["note"] or "") for n in notes)
    store.conn.close()


def test_apply_bridges_blocked_task_to_merged(tmp_path, monkeypatch):
    """`--apply` against a BLOCKED task whose commits ARE in main bridges
    BLOCKED → PENDING → MERGED via the extended `mark_merged` helper.

    Documents the retroactive-cleanup use case: a task got BLOCKED on
    something unrelated (e.g. review-response stalled), the operator
    integrated its commits via release-batch, then runs the sweep to
    settle the audit trail."""
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-BLK")
    store.transition(
        "R-BLK",
        State.PENDING_CI,
        branch="quikode/r-blk-abc",
        pr_number=21,
    )
    store.transition("R-BLK", State.AWAITING_REVIEW)
    store.transition("R-BLK", State.ADDRESSING_FEEDBACK)
    store.transition("R-BLK", State.BLOCKED, last_error="feedback exhausted")
    store.conn.close()
    _patch_subprocess(monkeypatch, ancestor_branches={"quikode/r-blk-abc"})

    result = CliRunner().invoke(app, ["detect-merged", "--apply"])
    assert result.exit_code == 0, result.output

    store = Store(tmp_path / ".quikode" / "quikode.db")
    assert store.get("R-BLK")["state"] == State.MERGED.value
    store.conn.close()


def test_no_candidate_tasks_emits_friendly_message(tmp_path, monkeypatch):
    """Empty workspace (or all-MERGED workspace) prints a clean message
    and exits 0."""
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    # No tasks seeded — store is created lazily by the CLI.
    _patch_subprocess(monkeypatch, ancestor_branches=set())

    result = CliRunner().invoke(app, ["detect-merged"])
    assert result.exit_code == 0, result.output
    assert "no non-MERGED tasks" in result.output


def test_no_fetch_skips_initial_remote_refresh(tmp_path, monkeypatch):
    """`--no-fetch` suppresses the up-front `git fetch` call.

    Lets the operator chain `qk detect-merged --no-fetch` after a manual
    `git fetch` without round-tripping the remote a second time."""
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    _seed_three_tasks(tmp_path)
    calls = _patch_subprocess(monkeypatch, ancestor_branches=set())

    result = CliRunner().invoke(app, ["detect-merged", "--no-fetch"])
    assert result.exit_code == 0, result.output
    fetch_calls = [c for c in calls if "fetch" in c]
    assert fetch_calls == []
