"""Plan 32 PR-A 3/3 — merge-node id determinism, octopus / sequential
merge dispatch, and BLOCK behavior on sequential conflict.

These tests stub `git`/`exec_in` rather than spinning a real container.
The merge-node worker's contract is "drive the FSM through deterministic
git steps and fire MERGE_NODE_BUILT on local-CI success" — exercising
the FSM transitions + git command sequence is sufficient at the unit
layer. Real-git e2e coverage lives in `test_stacking_e2e_git.py` for the
stacking helpers; PR-B will add merge-doer-subloop e2e coverage.
"""

from __future__ import annotations

from typing import Any

import pytest

from quikode import fsm_runtime, merge_node, pre_pr_audit
from quikode.config import Config
from quikode.dag import DAG, Node
from quikode.fsm import State
from quikode.state import Store
from quikode.workers import merge_node_worker as mnw_mod
from quikode.workers.merge_node_worker import MergeNodeWorker


def _empty_dag(tmp_path) -> DAG:
    p = tmp_path / "dag.json"
    p.write_text(
        '{"schema": "test", "milestones": '
        '[{"id": "M-1", "title": "x", "goal": "x", "status": "planned"}], '
        '"nodes": []}'
    )
    return DAG.load(p)


def _seed_merge_node(tmp_path, parent_ids: list[str]) -> tuple[Store, str, list[str]]:
    """Create a merge-node with two source parents in PENDING_CI."""
    store = Store(tmp_path / "q.db")
    parent_branches = [f"quikode/{pid.lower()}-aaa" for pid in parent_ids]
    for pid, br in zip(parent_ids, parent_branches, strict=True):
        store.upsert_pending(pid)
        store.transition(pid, State.PENDING_CI, branch=br)
    mn_id = merge_node.lookup_or_create_merge_node(store, parent_ids, parent_branches)
    return store, mn_id, parent_branches


def _build_worker(
    tmp_path,
    cfg: Config,
    store: Store,
    mn_id: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    git_calls: list[list[str]] | None = None,
    octopus_succeeds: bool = True,
    sequential_succeeds: bool = True,
    local_ci_passes: bool = True,
) -> MergeNodeWorker:
    """Construct a MergeNodeWorker with all external touch-points stubbed."""
    dag = _empty_dag(tmp_path)
    parent_ids = store.get_parent_task_ids(mn_id)
    node = Node(
        id=mn_id,
        kind="merge",
        milestone="",
        title=f"merge-node integrating {','.join(parent_ids)}",
        scope="",
        depends_on=tuple(parent_ids),
        completes_behaviors=(),
        supports_behaviors=(),
        boundary_with_neighbors="",
        expected_evidence=(),
        playbook=(),
        rationale="",
        risks=(),
        raw={},
    )
    git_calls = git_calls if git_calls is not None else []

    class _StubbedWorker(MergeNodeWorker):
        """Subclass override of provisioning + git for unit testing."""

        def _provision_merge_node_worktree(self) -> None:
            if fsm_runtime.current_state(store, mn_id) is State.PENDING:
                fsm_runtime.start_task(store, mn_id, note="test stub")

        def _provision_container(self, wt_path) -> None:  # pragma: no cover - trivial
            del wt_path
            return None

        def _teardown(self) -> None:  # pragma: no cover - trivial
            return None

        def _git_in_workspace(self, args: list[str]) -> tuple[int, str]:
            git_calls.append(list(args))
            if "merge" in args and "--no-ff" in args and "--abort" not in args:
                remote_refs = [a for a in args if a.startswith("origin/quikode/")]
                if len(remote_refs) >= 2:
                    return (0 if octopus_succeeds else 1, "octopus merge attempt")
                if len(remote_refs) == 1:
                    return (0 if sequential_succeeds else 1, "sequential merge")
            return (0, "ok")

    worker = _StubbedWorker(cfg, dag, store, node)
    store.set_field(mn_id, worktree_path=str(tmp_path / "wt"), container_id="fake")

    class FakeHandle:
        def __init__(self) -> None:
            self.unit_id = "fake"
            self.metadata: dict[str, Any] = {"container_id": "fake"}

    worker.handle = FakeHandle()

    def fake_exec_in(handle, cmd, log_path=None, timeout=None):
        return (0, "fetched", "")

    monkeypatch.setattr(mnw_mod, "exec_in", fake_exec_in)

    def fake_local_ci(*, cfg, handle, log_path=None):
        return pre_pr_audit.StageOutcome(
            name="local_ci",
            passed=local_ci_passes,
            summary="local ci stub" if local_ci_passes else "local ci failed (stub)",
            raw_output="...",
        )

    monkeypatch.setattr(mnw_mod.pre_pr_audit, "run_local_ci_gate", fake_local_ci)
    return worker


# ----- merge-node id determinism -----


def test_merge_node_id_is_deterministic_independent_of_input_order():
    a = merge_node.compute_merge_node_id(["R-001", "R-002"])
    b = merge_node.compute_merge_node_id(["R-002", "R-001"])
    assert a == b
    assert a.startswith("M-")


def test_merge_node_id_differs_when_parent_set_differs():
    a = merge_node.compute_merge_node_id(["R-001", "R-002"])
    b = merge_node.compute_merge_node_id(["R-001", "R-002", "R-003"])
    assert a != b


def test_merge_node_id_empty_raises():
    with pytest.raises(ValueError):
        merge_node.compute_merge_node_id([])


# ----- worker lifecycle: octopus path -----


def test_octopus_merge_succeeds_transitions_to_ready(tmp_path, monkeypatch):
    """Trivial octopus merge → MERGE_NODE_BUILT → MERGE_NODE_READY."""
    store, mn_id, _ = _seed_merge_node(tmp_path, ["R-001", "R-002"])
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        local_ci_command="just ci",
    )
    git_calls: list[list[str]] = []
    worker = _build_worker(
        tmp_path, cfg, store, mn_id, monkeypatch, git_calls=git_calls, octopus_succeeds=True
    )

    outcome = worker.run()

    assert outcome.final_state == State.MERGE_NODE_READY
    assert fsm_runtime.current_state(store, mn_id) is State.MERGE_NODE_READY
    # Octopus merge should have been invoked with both remote refs.
    octopus_calls = [c for c in git_calls if "merge" in c and "--no-ff" in c and "--abort" not in c]
    assert any("origin/quikode/r-001-aaa" in c and "origin/quikode/r-002-aaa" in c for c in octopus_calls), (
        f"expected octopus merge call; got {octopus_calls}"
    )


# ----- worker lifecycle: sequential fallback -----


def test_octopus_fails_then_sequential_succeeds(tmp_path, monkeypatch):
    """Octopus fails → sequential succeeds → MERGE_NODE_READY."""
    store, mn_id, _ = _seed_merge_node(tmp_path, ["R-001", "R-002"])
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        local_ci_command="just ci",
    )
    git_calls: list[list[str]] = []
    worker = _build_worker(
        tmp_path,
        cfg,
        store,
        mn_id,
        monkeypatch,
        git_calls=git_calls,
        octopus_succeeds=False,
        sequential_succeeds=True,
    )

    outcome = worker.run()

    assert outcome.final_state == State.MERGE_NODE_READY
    # Sequential merges happened (one per parent, single-ref `merge`).
    sequential_calls = [
        c
        for c in git_calls
        if "merge" in c
        and "--no-ff" in c
        and "--abort" not in c
        and len([a for a in c if a.startswith("origin/quikode/")]) == 1
    ]
    assert len(sequential_calls) == 2, f"expected 2 sequential merges; got {sequential_calls}"


# ----- worker lifecycle: sequential conflict BLOCKs -----


def test_sequential_conflict_blocks_with_pr_b_pointer(tmp_path, monkeypatch):
    """Octopus fails AND sequential fails → BLOCKED with note pointing at PR-B."""
    store, mn_id, _ = _seed_merge_node(tmp_path, ["R-001", "R-002"])
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        local_ci_command="just ci",
    )
    worker = _build_worker(
        tmp_path,
        cfg,
        store,
        mn_id,
        monkeypatch,
        octopus_succeeds=False,
        sequential_succeeds=False,
    )

    outcome = worker.run()

    assert outcome.final_state == State.BLOCKED
    row = store.get(mn_id)
    assert row is not None
    assert row["state"] == State.BLOCKED.value
    last_error = row.get("last_error") or ""
    assert "sequential merge conflict" in last_error.lower()
    assert "pr-b" in last_error.lower()
