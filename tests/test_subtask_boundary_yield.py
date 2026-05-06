"""v3 subtask-boundary yield: a worker can surrender its slot to a higher-
priority queued candidate at a subtask completion boundary.

Off by default; enabled by `cfg.preempt_at_subtask_boundary`. The yielded
task transitions back to PENDING with `resume_from_existing_subtasks=1` so
the orchestrator's next pick fills the slot with the priority winner; the
yielded task picks up where it left off when its priority becomes highest.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from quikode.config import Config
from quikode.dag import DAG
from quikode.state import State, Store, SubtaskState
from quikode.subtask_schema import Plan, Subtask
from quikode.worker import TaskWorker
from quikode.workers.outcomes import WorkerOutcome


def _build_dag(tmp_path: Path, ids_with_deps: list[tuple[str, list[str]]]) -> DAG:
    nodes = []
    for nid, deps in ids_with_deps:
        nodes.append(
            {
                "id": nid,
                "kind": "behavior",
                "milestone": "M-1",
                "title": nid,
                "scope": "x",
                "depends_on": deps,
                "completes_behaviors": [],
                "supports_behaviors": [],
                "boundary_with_neighbors": "",
                "expected_evidence": [],
                "playbook": [],
                "rationale": "",
                "risks": [],
            }
        )
    p = tmp_path / "dag.json"
    p.write_text(
        json.dumps(
            {
                "schema": "test",
                "milestones": [{"id": "M-1", "title": "x", "goal": "x", "status": "planned"}],
                "nodes": nodes,
            }
        )
    )
    return DAG.load(p)


def _build_worker(
    tmp_path: Path,
    *,
    self_id: str,
    dag_edges: list[tuple[str, list[str]]],
    preempt_on: bool = True,
    threshold: int = 50,
) -> TaskWorker:
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        prompts_dir=tmp_path / "missing-prompts",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
        preempt_at_subtask_boundary=preempt_on,
        preempt_yield_threshold=threshold,
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    dag = _build_dag(tmp_path, dag_edges)
    store = Store(cfg.state_dir / "quikode.db")
    for nid, _ in dag_edges:
        store.upsert_pending(nid)
    worker = TaskWorker(cfg, dag, store, dag.nodes[self_id])
    worker.handle = MagicMock(container_name="qk-stub")
    return worker


def test_yield_off_by_default(tmp_path):
    """When `preempt_at_subtask_boundary` is False the yield check is a no-op
    even if a higher-priority candidate is queued."""
    edges = [
        ("R-001", []),  # high fan-out — others depend on it
        ("R-099", []),  # the one we're "running"
        ("R-010", ["R-001"]),
        ("R-011", ["R-001"]),
        ("R-012", ["R-001"]),
    ]
    worker = _build_worker(tmp_path, self_id="R-099", dag_edges=edges, preempt_on=False)
    outcome = worker._maybe_yield_at_boundary()
    assert outcome is None
    worker.store.conn.close()


def test_yields_when_higher_priority_queued(tmp_path):
    """Strict FSM mode does not re-pend active work for preemptive yielding."""
    # R-001 has 3 dependents → score = 0 + 15 - 0 = 15
    # R-099 has 0 dependents → score = 0 + 0 - 9 = -9
    # delta = 15 - (-9) = 24. With threshold=10, yield triggers.
    edges = [
        ("R-001", []),
        ("R-099", []),
        ("R-010", ["R-001"]),
        ("R-011", ["R-001"]),
        ("R-012", ["R-001"]),
    ]
    worker = _build_worker(tmp_path, self_id="R-099", dag_edges=edges, preempt_on=True, threshold=10)
    outcome = worker._maybe_yield_at_boundary()
    assert outcome is None
    row = worker.store.get("R-099")
    assert row["state"] == State.PENDING.value
    assert (row.get("resume_from_existing_subtasks") or 0) == 0
    worker.store.conn.close()


def test_does_not_yield_below_threshold(tmp_path):
    """Even with priority delta > 0, yield doesn't fire unless delta exceeds
    the threshold. Avoids thrash on small priority shifts."""
    edges = [
        ("R-001", []),
        ("R-099", []),
        ("R-010", ["R-001"]),  # only 1 dependent → small delta
    ]
    worker = _build_worker(tmp_path, self_id="R-099", dag_edges=edges, preempt_on=True, threshold=200)
    outcome = worker._maybe_yield_at_boundary()
    assert outcome is None
    worker.store.conn.close()


def test_no_yield_when_no_pending_candidates(tmp_path):
    """If nothing is pending, yield is a no-op."""
    edges = [("R-001", [])]
    worker = _build_worker(tmp_path, self_id="R-001", dag_edges=edges, preempt_on=True)
    # Mark R-001 as DOING so it's not in PENDING (and there's nothing else pending).
    worker.store.transition("R-001", State.DOING_SUBTASK)
    outcome = worker._maybe_yield_at_boundary()
    assert outcome is None
    worker.store.conn.close()


def test_run_subtask_set_returns_yield_outcome(tmp_path):
    """If a boundary check returns an outcome, the subtask set propagates it."""
    edges = [
        ("R-001", []),
        ("R-099", []),
        ("R-010", ["R-001"]),
        ("R-011", ["R-001"]),
        ("R-012", ["R-001"]),
    ]
    worker = _build_worker(tmp_path, self_id="R-099", dag_edges=edges, preempt_on=True, threshold=10)
    worker.plan = Plan(
        node_id="R-099",
        summary="x",
        subtasks=(
            Subtask(
                id="S-01",
                title="x",
                depends_on=(),
                files_to_touch=("a.rs",),
                boundary="",
                acceptance=("ok",),
                notes="",
            ),
        ),
        final_acceptance=("just ci",),
    )
    worker.store.upsert_subtasks(
        "R-099",
        [{"subtask_id": "S-01", "title": "x", "acceptance": ["ok"]}],
    )
    do_called = []
    with (
        patch.object(worker, "_do_subtask", side_effect=lambda *a, **k: do_called.append(1)),
        patch.object(worker, "_check_subtask"),
        patch.object(worker, "_handle_parent_rebase_if_needed", return_value=None),
        patch.object(
            worker, "_maybe_yield_at_boundary", return_value=WorkerOutcome(State.PENDING, "yielded")
        ),
    ):
        outcome = worker._run_subtask_set([worker.plan.subtasks[0]])
    assert outcome is not None
    assert outcome.final_state is State.PENDING
    # The doer was NOT invoked because we yielded at the boundary first.
    assert not do_called
    # Subtask state stays PENDING (we didn't get to mark it doing).
    sub = worker.store.get_subtask("R-099", "S-01")
    assert sub["state"] == SubtaskState.PENDING.value
    worker.store.conn.close()
