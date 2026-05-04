"""Item 6: worktree-path race fix.

The orchestrator's _schedule_rebase_to_main can land a worker on a row
that lost its `worktree_path` value (rare race observed in Run 3). The
worker must:

1. If `branch` + reconstructed path on disk exist → recover and persist.
2. If reconstruction fails → raise; the run_rebase_to_main outer
   try/except restores pre-rebase state cleanly without crashing the
   daemon.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from quikode.config import Config
from quikode.dag import DAG, Node
from quikode.state import State, Store
from quikode.worker import TaskWorker


def _node(task_id: str = "T-003") -> Node:
    return Node(
        id=task_id,
        kind="behavior",
        milestone="M-1",
        title="x",
        scope="x",
        depends_on=(),
        completes_behaviors=(),
        supports_behaviors=(),
        boundary_with_neighbors="",
        expected_evidence=(),
        playbook=(),
        rationale="",
        risks=(),
        raw={},
    )


def _worker(tmp_path: Path, task_id: str = "T-003") -> TaskWorker:
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path,
        worktree_root=tmp_path / "wt",
    )
    cfg.worktree_root.mkdir(parents=True, exist_ok=True)
    store = Store(tmp_path / "q.db")

    class _DAG(DAG):
        def __init__(self):
            self.nodes = {task_id: _node(task_id)}

    return TaskWorker(cfg, _DAG(), store, _node(task_id))


def test_reconstructs_worktree_from_branch_when_path_missing(tmp_path):
    """Row has branch but no worktree_path; the candidate dir exists →
    reconstruct + persist. No raise."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-003")
    branch = "quikode/t-003-abc123"
    w.store.transition("T-003", State.AWAITING_MERGE, branch=branch)
    # Build the canonical wt path matching _provision_worktree's recipe.
    expected_dir = w.cfg.worktree_root / "t-003-abc123"
    expected_dir.mkdir(parents=True, exist_ok=True)

    out = w._existing_worktree_path()
    assert out == expected_dir.resolve()
    # Persisted back to the row
    assert w.store.get("T-003")["worktree_path"] == str(expected_dir.resolve())
    w.store.conn.close()


def test_existing_worktree_path_returns_stored_when_set(tmp_path):
    w = _worker(tmp_path)
    w.store.upsert_pending("T-003")
    real = (tmp_path / "wt" / "t-003-zzz").resolve()
    real.mkdir(parents=True, exist_ok=True)
    w.store.transition("T-003", State.AWAITING_MERGE, worktree_path=str(real), branch="quikode/t-003-zzz")
    assert w._existing_worktree_path() == Path(str(real))
    w.store.conn.close()


def test_raises_when_branch_present_but_recon_dir_missing(tmp_path):
    """No worktree_path, branch known, but the reconstructed candidate
    doesn't exist on disk → raise. Caller must handle, not crash."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-003")
    w.store.transition("T-003", State.AWAITING_MERGE, branch="quikode/t-003-missing")
    try:
        w._existing_worktree_path()
    except RuntimeError as e:
        assert "no worktree_path" in str(e)
    else:
        raise AssertionError("expected RuntimeError")
    w.store.conn.close()


def test_raises_when_branch_and_path_both_missing(tmp_path):
    w = _worker(tmp_path)
    w.store.upsert_pending("T-003")
    w.store.transition("T-003", State.AWAITING_MERGE)  # neither
    try:
        w._existing_worktree_path()
    except RuntimeError as e:
        assert "no worktree_path" in str(e)
    else:
        raise AssertionError("expected RuntimeError")
    w.store.conn.close()


def test_run_rebase_to_main_handles_missing_worktree_cleanly(tmp_path):
    """End-to-end: row enters REBASING_TO_MAIN with no worktree_path AND
    no recoverable on-disk dir. Worker must NOT crash; it must restore
    the pre-rebase state with last_error set."""
    w = _worker(tmp_path)
    w.store.upsert_pending("T-003")
    # Stash a pre-rebase state and put the row in REBASING_TO_MAIN with
    # branch but no worktree_path AND no actual dir.
    w.store.transition(
        "T-003",
        State.REBASING_TO_MAIN,
        branch="quikode/t-003-vanished",
        pr_number=42,
        pr_url="https://github.com/owner/repo/pull/42",
    )
    w.store.set_pre_rebase_state("T-003", State.AWAITING_MERGE.value)
    # Patch docker_env interactions to avoid real container spinups.
    with (
        patch("quikode.worker.docker_env.make_handle"),
        patch("quikode.worker.docker_env.workspace_label", return_value=""),
        patch("quikode.worker.docker_env.network_create"),
        patch("quikode.worker.docker_env.start_postgres"),
        patch("quikode.worker.docker_env.wait_postgres_healthy"),
        patch("quikode.worker.docker_env.start_dev_container"),
        patch("quikode.worker.docker_env.wait_dev_ready"),
        patch("quikode.worker.docker_env.teardown"),
    ):
        out = w.run_rebase_to_main()

    # Row is restored to AWAITING_MERGE (the stashed pre-rebase state)
    # rather than left dangling in REBASING_TO_MAIN.
    row = w.store.get("T-003")
    assert row["state"] == State.AWAITING_MERGE.value
    assert row.get("last_error")  # explanatory error captured
    assert out.final_state == State.AWAITING_MERGE
    w.store.conn.close()
