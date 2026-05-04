"""Headline stats sidebar — pure functions over DAG + Store.

Computes project depth, remaining depth, merged/in-flight/ready counts,
rolling cost + runtime averages, and ETA estimates (serial + parallel).
The screen widget reads this and formats the right sidebar.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from quikode.dag import DAG
from quikode.state import State, Store

from .layout import ranks


@dataclass(frozen=True)
class HeadlineStats:
    """All headline numbers shown in the sidebar.

    Money fields are dollars; time fields are seconds. Optional fields
    are None when there isn't enough signal yet (e.g. no merged R-* nodes
    for the rolling-window averages).
    """

    project_depth: int
    remaining_depth: int
    total_nodes: int
    merged: int
    in_flight: int
    ready: int
    behaviors_total: int
    behaviors_completed: int
    cost_so_far_usd: float | None
    avg_cost_per_node_usd: float | None
    projected_total_usd: float | None
    avg_runtime_per_node_s: float | None
    eta_serial_s: float | None
    eta_parallel_s: dict[int, float]


_IN_FLIGHT = {
    State.PROVISIONING.value,
    State.PLANNING.value,
    State.DOING.value,
    State.CHECKING.value,
    State.TRIAGING.value,
    State.DOING_SUBTASK.value,
    State.CHECKING_SUBTASK.value,
    State.TRIAGING_SUBTASK.value,
    State.FINAL_CHECKING.value,
    State.COMMITTING.value,
    State.PUSHING.value,
    State.PR_OPENING.value,
    State.POLLING_CI.value,
    State.REBASING.value,
    State.CONFLICT_RESOLVING.value,
    State.INTENT_REVIEWING.value,
    State.REPLANNING.value,
    State.ADDRESSING_FEEDBACK.value,
    State.REBASING_TO_MAIN.value,
}


def _rolling_merged_r_tasks(
    store: Store, *, limit: int
) -> list[tuple[str, float, float | None, float | None]]:
    """Return (task_id, runtime_s, cost_usd, merged_at) for the last
    `limit` R-* nodes that reached MERGED, newest-first.

    Runtime = merged_at - first_provisioned_at (using state_log). Cost =
    SUM(cost_usd) over agent_calls for that task. Returns whatever it can
    find — short list when fewer than `limit` are merged.
    """
    rows = store.conn.execute(
        "SELECT t.id AS id, MAX(sl.ts) AS merged_at "
        "FROM tasks t JOIN state_log sl ON sl.task_id = t.id "
        "WHERE t.state = ? AND t.id LIKE 'R-%' AND sl.to_state = ? "
        "GROUP BY t.id "
        "ORDER BY merged_at DESC LIMIT ?",
        (State.MERGED.value, State.MERGED.value, limit),
    ).fetchall()
    out: list[tuple[str, float, float | None, float | None]] = []
    for r in rows:
        tid = r["id"]
        merged_at = float(r["merged_at"])
        # First provisioning ts (earliest one — `quikode retry` re-runs the
        # task; the *latest* attempt's start would be more accurate but the
        # design doc spec'd "merged_at - first_provisioned_at" so we honor
        # the simpler reading).
        prov_row = store.conn.execute(
            "SELECT MIN(ts) AS first_ts FROM state_log WHERE task_id = ? AND to_state = ?",
            (tid, State.PROVISIONING.value),
        ).fetchone()
        first_prov = float(prov_row["first_ts"]) if prov_row and prov_row["first_ts"] else None
        runtime_s = merged_at - first_prov if first_prov is not None else 0.0
        cost_row = store.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM agent_calls WHERE task_id = ?",
            (tid,),
        ).fetchone()
        cost = float(cost_row["c"]) if cost_row and cost_row["c"] else None
        out.append((tid, runtime_s, cost, merged_at))
    return out


def _critical_path_runtime_s(
    dag: DAG, ranks_map: dict[str, int], states: dict[str, str], avg_runtime_s: float
) -> float:
    """Longest unmerged dependency chain in node-runtime-seconds.

    Each unmerged node contributes `avg_runtime_s`. Topological-DP over
    the DAG: f(n) = avg_runtime + max(f(d) for d in deps if unmerged).
    """
    if avg_runtime_s <= 0:
        return 0.0
    # Visit in rank order so deps are already computed.
    by_rank: dict[int, list[str]] = defaultdict(list)
    for nid, r in ranks_map.items():
        by_rank[r].append(nid)
    f: dict[str, float] = {}
    best = 0.0
    for r in sorted(by_rank):
        for nid in by_rank[r]:
            n = dag.nodes[nid]
            if states.get(nid) == "merged":
                f[nid] = 0.0
                continue
            parent_max = max((f.get(d, 0.0) for d in n.depends_on), default=0.0)
            f[nid] = avg_runtime_s + parent_max
            best = max(best, f[nid])
    return best


def compute_headline_stats(
    dag: DAG,
    store: Store,
    *,
    rolling_window: int = 5,
    max_parallel_choices: tuple[int, ...] = (1, 3, 5),
) -> HeadlineStats:
    """Compute all sidebar numbers in one pass."""
    ranks_map = ranks(dag)
    project_depth = max(ranks_map.values(), default=-1) + 1
    states: dict[str, str] = {r["id"]: r["state"] for r in store.all_tasks()}

    merged_count = sum(1 for s in states.values() if s == State.MERGED.value)
    in_flight = sum(1 for s in states.values() if s in _IN_FLIGHT)
    merged_ids = {nid for nid, s in states.items() if s == State.MERGED.value}
    ready = sum(
        1
        for nid, n in dag.nodes.items()
        if nid not in merged_ids
        and all(d in merged_ids for d in n.depends_on)
        and states.get(nid) != State.MERGED.value
    )
    # Subtle: filter the count to *unmerged* ready nodes only (a merged node
    # whose deps are merged is "ready" by the predicate but uninteresting).
    # Already handled by the `nid not in merged_ids` clause.

    remaining_depth_set = {ranks_map[nid] for nid, s in states.items() if s != State.MERGED.value}
    # Include unseeded nodes in the remaining-depth calc (no row in the
    # store ⇒ effectively pending).
    for nid, r in ranks_map.items():
        if nid not in states:
            remaining_depth_set.add(r)
    remaining_depth = (max(remaining_depth_set) + 1) if remaining_depth_set else 0

    behaviors = {b for n in dag.nodes.values() for b in n.completes_behaviors}
    behaviors_completed = {
        b
        for nid, s in states.items()
        if s == State.MERGED.value
        for b in dag.nodes.get(nid).completes_behaviors  # type: ignore[union-attr]
        if nid in dag.nodes
    }

    # Cost-so-far: SUM(cost_usd) over all agent_calls.
    cost_row = store.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM agent_calls").fetchone()
    cost_so_far = float(cost_row["c"]) if cost_row and cost_row["c"] else None

    # Rolling averages.
    rolling = _rolling_merged_r_tasks(store, limit=rolling_window)
    avg_cost: float | None
    avg_runtime: float | None
    if len(rolling) >= rolling_window:
        cost_vals = [c for _, _, c, _ in rolling if c is not None]
        runtime_vals = [rt for _, rt, _, _ in rolling if rt > 0]
        avg_cost = sum(cost_vals) / len(cost_vals) if cost_vals else None
        avg_runtime = sum(runtime_vals) / len(runtime_vals) if runtime_vals else None
    else:
        avg_cost = None
        avg_runtime = None

    unmerged_count = len(dag.nodes) - merged_count
    projected_total = (cost_so_far or 0.0) + avg_cost * unmerged_count if avg_cost is not None else None

    eta_serial = avg_runtime * unmerged_count if avg_runtime is not None else None

    eta_parallel: dict[int, float] = {}
    if avg_runtime is not None and unmerged_count > 0:
        cp_runtime = _critical_path_runtime_s(dag, ranks_map, states, avg_runtime)
        total_work = avg_runtime * unmerged_count
        for n_par in max_parallel_choices:
            if n_par <= 0:
                continue
            parallel_floor = math.ceil(total_work / n_par)
            eta_parallel[n_par] = max(cp_runtime, parallel_floor)

    return HeadlineStats(
        project_depth=project_depth,
        remaining_depth=remaining_depth,
        total_nodes=len(dag.nodes),
        merged=merged_count,
        in_flight=in_flight,
        ready=ready,
        behaviors_total=len(behaviors),
        behaviors_completed=len(behaviors_completed),
        cost_so_far_usd=cost_so_far,
        avg_cost_per_node_usd=avg_cost,
        projected_total_usd=projected_total,
        avg_runtime_per_node_s=avg_runtime,
        eta_serial_s=eta_serial,
        eta_parallel_s=eta_parallel,
    )
