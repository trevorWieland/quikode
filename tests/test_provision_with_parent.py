"""v3 Phase C: `_provision_worktree` consults `task.parent_pr_branch`.

When the orchestrator stamped a child task with `parent_pr_branch`, the
worker's worktree provision must branch off that ref instead of main.
The seam under test is `worktree.add_worktree_off_branch` (called when
parent_branch is set) vs `worktree.add_worktree` (default).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quikode import worktree
from quikode.config import Config
from quikode.dag import DAG
from quikode.state import Store
from quikode.worker import TaskWorker


def _make_dag(tmp_path: Path) -> DAG:
    raw = {
        "schema": "test",
        "milestones": [{"id": "M-1", "title": "x", "goal": "x", "status": "planned"}],
        "nodes": [
            {
                "id": "PARENT",
                "kind": "behavior",
                "milestone": "M-1",
                "title": "P",
                "scope": "x",
                "depends_on": [],
                "completes_behaviors": [],
                "supports_behaviors": [],
                "boundary_with_neighbors": "",
                "expected_evidence": [],
                "playbook": [],
                "rationale": "",
                "risks": [],
            },
            {
                "id": "CHILD",
                "kind": "behavior",
                "milestone": "M-1",
                "title": "C",
                "scope": "x",
                "depends_on": ["PARENT"],
                "completes_behaviors": [],
                "supports_behaviors": [],
                "boundary_with_neighbors": "",
                "expected_evidence": [],
                "playbook": [],
                "rationale": "",
                "risks": [],
            },
        ],
    }
    p = tmp_path / "dag.json"
    p.write_text(json.dumps(raw))
    return DAG.load(p)


def _worker(tmp_path: Path, *, child_id: str = "CHILD") -> TaskWorker:
    dag = _make_dag(tmp_path)
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
        stacking_strategy="within-milestone",
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.state_dir / "q.db")
    store.upsert_pending("PARENT")
    store.upsert_pending(child_id)
    node = dag.nodes[child_id]
    return TaskWorker(cfg, dag, store, node)


def test_provision_worktree_with_parent_pr_branch_calls_off_branch(tmp_path):
    """Child has `parent_pr_branch` stamped → uses add_worktree_off_branch
    with the parent_branch as base + the configured remote for fetch."""
    w = _worker(tmp_path)
    # Parent is in flight on a known branch.
    w.store.set_field("PARENT", branch="quikode/parent-deadbe")
    w.store.set_field(
        "CHILD",
        parent_pr_branch="quikode/parent-deadbe",
        parent_branch="quikode/parent-deadbe",
    )

    with (
        patch("quikode.worker.worktree.fetch_base") as fb_mock,
        patch("quikode.worker.worktree.add_worktree_off_branch") as off_mock,
        patch("quikode.worker.worktree.add_worktree") as add_mock,
        patch("quikode.worker.subprocess.run") as proc_mock,
    ):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "abcdef0\n"
        proc_mock.return_value = proc

        w._provision_worktree()

    fb_mock.assert_called_once()
    off_mock.assert_called_once()
    add_mock.assert_not_called()
    # Inspect args: (repo, wt_path, child_branch, parent_branch); remote kw.
    args, kwargs = off_mock.call_args
    assert args[3] == "quikode/parent-deadbe"
    assert kwargs.get("remote") == w.cfg.pr_remote

    row = w.store.get("CHILD")
    assert row["parent_branch"] == "quikode/parent-deadbe"
    assert row["parent_task_id"] == "PARENT"
    w.store.conn.close()


def test_provision_worktree_without_parent_pr_branch_calls_main(tmp_path):
    """No parent_pr_branch → falls back to v2 path (add_worktree off main).

    Note: when stacking is enabled and a dep has a branch in a stack-ready
    state, `_resolve_stack_parent` will still pick it up at provision time.
    To exercise the "off main" branch we make sure the parent has no
    branch yet.
    """
    w = _worker(tmp_path)
    # No parent branch, no parent_pr_branch on child — should branch off main.

    with (
        patch("quikode.worker.worktree.fetch_base") as fb_mock,
        patch("quikode.worker.worktree.add_worktree_off_branch") as off_mock,
        patch("quikode.worker.worktree.add_worktree") as add_mock,
        patch("quikode.worker.subprocess.run") as proc_mock,
    ):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "abcdef0\n"
        proc_mock.return_value = proc

        w._provision_worktree()

    fb_mock.assert_called_once()
    add_mock.assert_called_once()
    off_mock.assert_not_called()
    # add_worktree(repo, wt_path, branch, base_branch, remote)
    args, _ = add_mock.call_args
    assert args[3] == w.cfg.base_branch
    w.store.conn.close()


def test_add_worktree_off_branch_fetches_remote_when_given(tmp_path):
    """worktree.add_worktree_off_branch with remote= triggers a git fetch
    of `parent_branch:parent_branch` so the local ref exists before the
    worktree create."""
    repo = tmp_path / "repo"
    repo.mkdir()
    wt = tmp_path / "wt"
    with patch("quikode.worktree._run") as run_mock:
        worktree.add_worktree_off_branch(repo, wt, "child-br", "parent-br", remote="origin")
    calls = run_mock.call_args_list
    # First call: git fetch origin parent-br:parent-br
    assert calls[0].args[0][:5] == ["git", "fetch", "origin", "parent-br:parent-br"]
    # Last call: git worktree add -b child-br <wt> parent-br
    assert calls[-1].args[0][:5] == ["git", "worktree", "add", "-b", "child-br"]


def test_add_worktree_off_branch_skips_fetch_without_remote(tmp_path):
    """Backward compat: no remote kw → no fetch (legacy v2 path)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    wt = tmp_path / "wt"
    with patch("quikode.worktree._run") as run_mock:
        worktree.add_worktree_off_branch(repo, wt, "child-br", "parent-br")
    # Only one call — no fetch.
    assert len(run_mock.call_args_list) == 1
    assert run_mock.call_args_list[0].args[0][:3] == ["git", "worktree", "add"]


# ----- v3 cleanup-4: resume must reuse existing worktree -----


def test_provision_worktree_reuses_existing_on_resume(tmp_path):
    """cleanup-4 regression: when a task row already has `worktree_path` +
    `branch` AND that worktree is registered with git, `_provision_worktree`
    must NOT generate a fresh branch/dir. Otherwise `quikode resume` orphans
    any fix the human pushed into the existing worktree (the unblock flow
    would be silently broken).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    existing_wt = tmp_path / "existing_wt"
    existing_wt.mkdir()
    cfg = Config(
        repo_path=repo,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        prompts_dir=tmp_path / "missing-prompts",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    dag = _make_dag(tmp_path)
    store = Store(cfg.state_dir / "quikode.db")
    store.upsert_pending("CHILD")
    # Pre-populate as if a prior run had provisioned + committed work here.
    store.set_field(
        "CHILD",
        worktree_path=str(existing_wt),
        branch="quikode/child-7b8f9c",
    )
    w = TaskWorker(cfg, dag, store, dag.nodes["CHILD"])

    fake_listing = MagicMock()
    fake_listing.stdout = f"worktree {existing_wt}\n"

    with (
        patch("quikode.worker.subprocess.run", return_value=fake_listing) as run_mock,
        patch.object(worktree, "branch_for") as branch_mock,
        patch.object(worktree, "add_worktree") as add_mock,
        patch.object(worktree, "add_worktree_off_branch") as add_off_mock,
    ):
        w._provision_worktree()
        # 1. git worktree list --porcelain was called to verify registration
        first = run_mock.call_args_list[0].args[0]
        assert first[:3] == ["git", "worktree", "list"]
    # 2. NEITHER add_worktree nor branch_for fired (reuse path).
    assert branch_mock.call_count == 0
    assert add_mock.call_count == 0
    assert add_off_mock.call_count == 0
    # 3. Row preserved.
    row = store.get("CHILD")
    assert row["branch"] == "quikode/child-7b8f9c"
    assert row["worktree_path"] == str(existing_wt)
    store.conn.close()


def test_provision_worktree_creates_new_when_path_missing(tmp_path):
    """If row says worktree_path=X but X doesn't exist on disk, treat as
    fresh and create a new worktree (don't try to reuse a nonexistent path)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = Config(
        repo_path=repo,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        prompts_dir=tmp_path / "missing-prompts",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    dag = _make_dag(tmp_path)
    store = Store(cfg.state_dir / "quikode.db")
    store.upsert_pending("CHILD")
    store.set_field("CHILD", worktree_path=str(tmp_path / "ghost"), branch="quikode/child-aaaaaa")
    w = TaskWorker(cfg, dag, store, dag.nodes["CHILD"])

    fake_proc = MagicMock(returncode=0, stdout="abc123\n")
    with (
        patch("quikode.worker.subprocess.run", return_value=fake_proc),
        patch.object(worktree, "branch_for", return_value="quikode/child-newhex") as branch_mock,
        patch.object(worktree, "fetch_base"),
        patch.object(worktree, "add_worktree") as add_mock,
    ):
        w._provision_worktree()
    # Fresh creation path fired.
    assert branch_mock.call_count == 1
    assert add_mock.call_count == 1
    store.conn.close()


def test_provision_worktree_creates_new_when_not_registered(tmp_path):
    """If row says worktree_path=X and X exists on disk, but git doesn't list
    it as a registered worktree, treat as fresh — the directory is unrelated
    debris (e.g., from a half-completed prior run)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    existing_wt = tmp_path / "stale"
    existing_wt.mkdir()
    cfg = Config(
        repo_path=repo,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        prompts_dir=tmp_path / "missing-prompts",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    dag = _make_dag(tmp_path)
    store = Store(cfg.state_dir / "quikode.db")
    store.upsert_pending("CHILD")
    store.set_field("CHILD", worktree_path=str(existing_wt), branch="quikode/child-stale")
    w = TaskWorker(cfg, dag, store, dag.nodes["CHILD"])

    # First call: worktree list (returns no matching entry).
    # Subsequent calls: rev-parse for base_sha.
    seq = [
        MagicMock(stdout="worktree /elsewhere\n"),
        MagicMock(returncode=0, stdout="abc123\n"),
    ]
    call_idx = {"n": 0}

    def fake_run(*args, **kwargs):
        i = call_idx["n"]
        call_idx["n"] += 1
        return seq[min(i, len(seq) - 1)]

    with (
        patch("quikode.worker.subprocess.run", side_effect=fake_run),
        patch.object(worktree, "branch_for", return_value="quikode/child-fresh") as branch_mock,
        patch.object(worktree, "fetch_base"),
        patch.object(worktree, "add_worktree") as add_mock,
    ):
        w._provision_worktree()
    assert branch_mock.call_count == 1
    assert add_mock.call_count == 1
    store.conn.close()


# ----- pytest pieces (no actual fixtures needed beyond above) -----


@pytest.fixture(autouse=True)
def _close_store_on_exit(request):
    yield
