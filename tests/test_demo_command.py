"""`quikode demo <id>` materializes a task's PR branch in `<repo>-demo`.

Solves the "git worktree already in use" friction: instead of attaching a
second worktree to the daemon's repo, we maintain a separate clone at a
sibling path so the user can run/test without disturbing the active
worktree.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from quikode.cli import app
from quikode.config import DEFAULT_CONFIG_TOML
from quikode.state import Store


def _bootstrap(tmp_path: Path, repo_subdir: str = "myrepo"):
    """Create a fake workspace at tmp_path and a fake repo at tmp_path/myrepo
    so target_dir computes to tmp_path/myrepo-demo."""
    repo_path = tmp_path / repo_subdir
    repo_path.mkdir()
    qkdir = tmp_path / ".quikode"
    qkdir.mkdir()
    (qkdir / "config.toml").write_text(
        DEFAULT_CONFIG_TOML.format(repo_path=str(repo_path), dag_path=str(tmp_path / "dag.json"))
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


def _seed_task_with_branch(tmp_path: Path, branch: str = "quikode/r-001-abc"):
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.set_field("R-001", branch=branch)
    store.conn.close()


def test_demo_clones_when_target_does_not_exist(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    _seed_task_with_branch(tmp_path)
    monkeypatch.chdir(tmp_path)

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        # `gh repo view --json url --jq .url` returns a clone url.
        if "gh" in args and "repo" in args and "view" in args:
            return MagicMock(returncode=0, stdout="https://github.com/foo/myrepo\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("quikode.cli.subprocess.run", side_effect=fake_run):
        result = CliRunner().invoke(app, ["demo", "R-001"])
    assert result.exit_code == 0, result.output
    expected_target = tmp_path / "myrepo-demo"
    assert str(expected_target) in result.output
    # Should have invoked git clone with the resolved url.
    clone_call = next((c for c in calls if c and c[0] == "git" and len(c) > 1 and c[1] == "clone"), None)
    assert clone_call is not None, f"expected git clone call; got {calls}"
    assert "https://github.com/foo/myrepo.git" in clone_call
    assert str(expected_target) in clone_call
    # And a checkout of the branch.
    checkout_calls = [c for c in calls if c and c[0] == "git" and len(c) > 1 and c[1] == "checkout"]
    assert any("quikode/r-001-abc" in c for c in checkout_calls)


def test_demo_reuses_existing_target_with_fetch_and_checkout(tmp_path, monkeypatch):
    """If <repo>-demo already exists, demo should fetch + checkout, not re-clone."""
    _bootstrap(tmp_path)
    _seed_task_with_branch(tmp_path)
    monkeypatch.chdir(tmp_path)

    target = tmp_path / "myrepo-demo"
    target.mkdir()  # simulate prior demo

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("quikode.cli.subprocess.run", side_effect=fake_run):
        result = CliRunner().invoke(app, ["demo", "R-001"])
    assert result.exit_code == 0, result.output

    # No git clone — only fetch + checkout.
    assert not any(c and c[0] == "git" and len(c) > 1 and c[1] == "clone" for c in calls)
    assert any(c and "fetch" in c for c in calls)
    assert any(c and "checkout" in c and "quikode/r-001-abc" in c for c in calls)


def test_demo_clean_flag_removes_existing_target(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    _seed_task_with_branch(tmp_path)
    monkeypatch.chdir(tmp_path)

    target = tmp_path / "myrepo-demo"
    target.mkdir()
    (target / "stale-marker.txt").write_text("old")

    def fake_run(args, **kwargs):
        if "gh" in args and "repo" in args:
            return MagicMock(returncode=0, stdout="https://github.com/foo/myrepo.git\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("quikode.cli.subprocess.run", side_effect=fake_run):
        result = CliRunner().invoke(app, ["demo", "R-001", "--clean"])
    assert result.exit_code == 0, result.output
    # The stale marker is gone (rmtree happened); the dir was re-created
    # by the clone (which fake_run no-ops, so the dir won't exist anymore
    # — that's fine, we just want to verify the rmtree path was hit).
    assert not (target / "stale-marker.txt").exists()


def test_demo_unknown_task_exits_nonzero(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["demo", "R-DOES-NOT-EXIST"])
    assert result.exit_code != 0
    assert "no task" in result.output


def test_demo_task_without_branch_exits_nonzero(tmp_path, monkeypatch):
    """Tasks that haven't been provisioned have no branch — bail with a hint."""
    _bootstrap(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    # No branch set.
    store.conn.close()
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(app, ["demo", "R-001"])
    assert result.exit_code != 0
    assert "no branch" in result.output


def test_demo_falls_back_to_git_config_when_gh_fails(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    _seed_task_with_branch(tmp_path)
    monkeypatch.chdir(tmp_path)

    def fake_run(args, **kwargs):
        if "gh" in args and "repo" in args:
            # Simulate gh not authed / not installed.
            return MagicMock(returncode=1, stdout="", stderr="not authed")
        if args and args[0] == "git" and "config" in args:
            return MagicMock(returncode=0, stdout="git@github.com:foo/myrepo.git\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("quikode.cli.subprocess.run", side_effect=fake_run):
        result = CliRunner().invoke(app, ["demo", "R-001"])
    assert result.exit_code == 0, result.output
    assert "myrepo-demo" in result.output


def test_demo_subprocess_clone_failure_propagates_nonzero(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    _seed_task_with_branch(tmp_path)
    monkeypatch.chdir(tmp_path)

    def fake_run(args, **kwargs):
        if "gh" in args and "repo" in args:
            return MagicMock(returncode=0, stdout="https://github.com/foo/myrepo.git\n", stderr="")
        if args and args[0] == "git" and "clone" in args:
            return MagicMock(returncode=128, stdout="", stderr="fatal: ...")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("quikode.cli.subprocess.run", side_effect=fake_run):
        result = CliRunner().invoke(app, ["demo", "R-001"])
    assert result.exit_code != 0
    assert "git clone failed" in result.output


# Touch subprocess so unused-import lint doesn't fire if the dep is removed.
_ = subprocess
