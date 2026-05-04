"""HeadlineStats math — depth, rolling avgs, ETA serial + parallel."""

from __future__ import annotations

import json
import time
from pathlib import Path

from quikode.dag import DAG
from quikode.state import State, Store
from quikode.tui.dag_view.stats import compute_headline_stats


def _write_dag(tmp_path: Path, nodes: list[dict]) -> Path:
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
    return p


def _node(nid: str, deps: list[str] | None = None, **kw) -> dict:
    return {
        "id": nid,
        "kind": "behavior",
        "milestone": "M-1",
        "title": nid,
        "scope": "",
        "depends_on": deps or [],
        "completes_behaviors": [],
        "supports_behaviors": [],
        "boundary_with_neighbors": "",
        "expected_evidence": [],
        "playbook": [],
        "rationale": "",
        "risks": [],
        **kw,
    }


def _seed_merged_task(store: Store, task_id: str, *, runtime_s: float, cost_usd: float) -> None:
    """Insert a row that's MERGED with a fake provisioning + merge timeline.

    runtime_s = merged_at - first_provisioned_at. cost_usd is split into
    a single agent_call row for simplicity.
    """
    store.upsert_pending(task_id)
    now = time.time()
    # Backdate the provisioning + merge events so runtime is what we want.
    prov_at = now - runtime_s
    with store.tx() as c:
        c.execute(
            "UPDATE tasks SET state = ? WHERE id = ?",
            (State.MERGED.value, task_id),
        )
        c.execute(
            "INSERT INTO state_log (task_id, from_state, to_state, ts) VALUES (?, ?, ?, ?)",
            (task_id, State.PENDING.value, State.PROVISIONING.value, prov_at),
        )
        c.execute(
            "INSERT INTO state_log (task_id, from_state, to_state, ts) VALUES (?, ?, ?, ?)",
            (task_id, State.PENDING_CI.value, State.MERGED.value, now),
        )
        c.execute(
            "INSERT INTO agent_calls "
            "(task_id, phase, cli, model, rc, duration_s, tokens_used, cost_usd, ts) "
            "VALUES (?, 'doer', 'opencode', 'm', 0, 100, 0, ?, ?)",
            (task_id, cost_usd, now),
        )


def test_project_depth(tmp_path):
    p = _write_dag(
        tmp_path,
        [_node("A"), _node("B", ["A"]), _node("C", ["B"])],
    )
    d = DAG.load(p)
    store = Store(tmp_path / "qk.db")
    s = compute_headline_stats(d, store)
    # 3 ranks: 0, 1, 2 → depth = 3.
    assert s.project_depth == 3
    assert s.total_nodes == 3
    assert s.merged == 0
    assert s.remaining_depth == 3


def test_remaining_depth_drops_when_top_rank_merges(tmp_path):
    p = _write_dag(
        tmp_path,
        [_node("A"), _node("B", ["A"]), _node("C", ["B"])],
    )
    d = DAG.load(p)
    store = Store(tmp_path / "qk.db")
    _seed_merged_task(store, "C", runtime_s=600, cost_usd=1.0)
    s = compute_headline_stats(d, store)
    # A and B are unseeded (=pending) at ranks 0 and 1. C is merged.
    # remaining_depth = max rank of unmerged = 1 → depth value 2.
    assert s.remaining_depth == 2


def test_rolling_averages_filled(tmp_path):
    p = _write_dag(
        tmp_path,
        [_node(f"R-{i:04d}") for i in range(1, 11)],
    )
    d = DAG.load(p)
    store = Store(tmp_path / "qk.db")
    # Seed 5 merged R-* tasks with known runtimes + costs.
    runtimes = [100, 200, 300, 400, 500]
    costs = [1.0, 2.0, 3.0, 4.0, 5.0]
    for i, (rt, c) in enumerate(zip(runtimes, costs, strict=True), start=1):
        _seed_merged_task(store, f"R-{i:04d}", runtime_s=rt, cost_usd=c)
    s = compute_headline_stats(d, store, rolling_window=5)
    # Average runtime = 300, average cost = 3.0
    assert s.avg_runtime_per_node_s is not None
    assert abs(s.avg_runtime_per_node_s - 300) < 5
    assert s.avg_cost_per_node_usd is not None
    assert abs(s.avg_cost_per_node_usd - 3.0) < 0.01


def test_rolling_averages_unfilled_returns_none(tmp_path):
    p = _write_dag(
        tmp_path,
        [_node(f"R-{i:04d}") for i in range(1, 6)],
    )
    d = DAG.load(p)
    store = Store(tmp_path / "qk.db")
    # Only 2 merged → not enough for the default window of 5.
    _seed_merged_task(store, "R-0001", runtime_s=300, cost_usd=2.0)
    _seed_merged_task(store, "R-0002", runtime_s=300, cost_usd=2.0)
    s = compute_headline_stats(d, store, rolling_window=5)
    assert s.avg_runtime_per_node_s is None
    assert s.avg_cost_per_node_usd is None


def test_eta_serial_is_unmerged_times_avg_runtime(tmp_path):
    p = _write_dag(
        tmp_path,
        [_node(f"R-{i:04d}") for i in range(1, 11)],
    )
    d = DAG.load(p)
    store = Store(tmp_path / "qk.db")
    for i in range(1, 6):
        _seed_merged_task(store, f"R-{i:04d}", runtime_s=300, cost_usd=2.0)
    s = compute_headline_stats(d, store, rolling_window=5)
    # 5 unmerged * 300s avg = 1500s
    assert s.eta_serial_s is not None
    assert abs(s.eta_serial_s - 1500) < 10


def test_eta_parallel_bounded_below_by_critical_path(tmp_path):
    # Long chain: 10 nodes, all unmerged. Critical path = 10 * avg_runtime.
    # No matter the parallelism, ETA can't drop below that.
    nodes = [_node("R-0001")]
    for i in range(2, 11):
        nodes.append(_node(f"R-{i:04d}", [f"R-{i - 1:04d}"]))
    p = _write_dag(tmp_path, nodes)
    d = DAG.load(p)
    store = Store(tmp_path / "qk.db")
    # Seed 5 merged at the start of an *unrelated* prefix would skew the chain;
    # instead seed dummy "X-*" merged tasks elsewhere — but the rolling avg
    # only looks at R-*, so just seed the front of our chain to "merge" them
    # and shift the unmerged tail.
    for i in range(1, 6):
        _seed_merged_task(store, f"R-{i:04d}", runtime_s=200, cost_usd=1.0)
    s = compute_headline_stats(d, store, rolling_window=5, max_parallel_choices=(1, 3, 5))
    # 5 unmerged left in a *chain* — critical path = 5 * 200s = 1000s.
    # With N=5, parallel floor = 5 * 200 / 5 = 200s. ETA = max(1000, 200) = 1000.
    assert 1 in s.eta_parallel_s
    assert s.eta_parallel_s[5] == 1000


def test_projected_total_uses_avg_cost_times_unmerged(tmp_path):
    p = _write_dag(
        tmp_path,
        [_node(f"R-{i:04d}") for i in range(1, 11)],
    )
    d = DAG.load(p)
    store = Store(tmp_path / "qk.db")
    for i in range(1, 6):
        _seed_merged_task(store, f"R-{i:04d}", runtime_s=300, cost_usd=2.0)
    s = compute_headline_stats(d, store, rolling_window=5)
    # Cost so far = 5 * 2.0 = 10. Unmerged = 5. Projected = 10 + 5*2 = 20.
    assert s.cost_so_far_usd is not None
    assert abs(s.cost_so_far_usd - 10.0) < 0.01
    assert s.projected_total_usd is not None
    assert abs(s.projected_total_usd - 20.0) < 0.5
