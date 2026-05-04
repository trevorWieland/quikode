"""DAG layout: rank assignment + column placement + edge routing.

The algorithm is a simplified Sugiyama:
  1. `ranks(dag)` — longest path from any root for each node id.
  2. `columns(dag, ranks)` — place each rank's nodes left-to-right; reduce
     crossings via barycenter sweeps over a few passes. Long edges get
     virtual same-rank "dummy" cells so the renderer doesn't draw through
     real nodes.
  3. `edges(dag, ranks, columns)` — return per-edge cell sequences (the
     intermediate (rank, col) cells along the route).

Pure: no UI / IO deps. Operates on `quikode.dag.DAG`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from quikode.dag import DAG


def ranks(dag: DAG) -> dict[str, int]:
    """Longest path from any root for each node id.

    Roots = nodes with no `depends_on`. Loops over the existing topo order
    so we can fold in `max(parent_rank) + 1` deterministically.
    """
    out: dict[str, int] = {}
    # Reuse DAG's topo sort — it raises on cycles, which we want here too.
    for nid in dag._topo_order():
        n = dag.nodes[nid]
        deps_in_dag = [d for d in n.depends_on if d in dag.nodes]
        if not deps_in_dag:
            out[nid] = 0
        else:
            out[nid] = 1 + max(out[d] for d in deps_in_dag)
    return out


def columns(dag: DAG, ranks_map: dict[str, int]) -> dict[str, int]:
    """Assign a column index per node id.

    Initial placement: sort each rank's nodes by id (deterministic). Then
    do a few barycenter sweeps — for each node, average the column of its
    parents (down sweep) or children (up sweep), and re-sort the rank by
    that. Finally compact column indices to be 0..k-1 per rank.
    """
    # Bucket ids by rank.
    by_rank: dict[int, list[str]] = defaultdict(list)
    for nid, r in ranks_map.items():
        by_rank[r].append(nid)
    for ids in by_rank.values():
        ids.sort()

    # Children index for the up-sweep.
    children: dict[str, list[str]] = defaultdict(list)
    for nid, n in dag.nodes.items():
        for d in n.depends_on:
            if d in dag.nodes:
                children[d].append(nid)

    # Initial column = position in the rank's id-sorted list.
    col: dict[str, int] = {nid: i for r in by_rank for i, nid in enumerate(by_rank[r])}

    # Barycenter sweeps. Three round-trips is enough for the tanren DAG;
    # this is "decent, not optimal" by design.
    max_rank = max(by_rank) if by_rank else 0
    for _ in range(3):
        # Down sweep: rank r's order influenced by rank r-1.
        for r in range(1, max_rank + 1):
            ids = by_rank.get(r, [])

            def _bary_down(nid: str) -> float:
                parents = [d for d in dag.nodes[nid].depends_on if d in col]
                if not parents:
                    return col[nid]
                return sum(col[p] for p in parents) / len(parents)

            ids.sort(key=lambda x: (_bary_down(x), x))
            col.update({nid: i for i, nid in enumerate(ids)})
            by_rank[r] = ids
        # Up sweep: rank r's order influenced by rank r+1.
        for r in range(max_rank - 1, -1, -1):
            ids = by_rank.get(r, [])

            def _bary_up(nid: str) -> float:
                kids = [c for c in children.get(nid, []) if c in col]
                if not kids:
                    return col[nid]
                return sum(col[c] for c in kids) / len(kids)

            ids.sort(key=lambda x: (_bary_up(x), x))
            col.update({nid: i for i, nid in enumerate(ids)})
            by_rank[r] = ids

    return col


@dataclass
class Edge:
    """One DAG edge plus the (rank, col) cells its route passes through.

    `cells` is the ordered list of cells from source to target. For a
    short edge (target_rank == source_rank + 1) cells will be empty.
    For long edges (target_rank > source_rank + 1) cells contains one
    intermediate cell per intervening rank, placed in the column of
    the source by default. The renderer paints these cells with edge
    glyphs.
    """

    source: str
    target: str
    cells: list[tuple[int, int]] = field(default_factory=list)


def edges(dag: DAG, ranks_map: dict[str, int], columns_map: dict[str, int]) -> list[Edge]:
    """Return all DAG edges with their intermediate routing cells."""
    out: list[Edge] = []
    for nid, n in dag.nodes.items():
        for dep in n.depends_on:
            if dep not in dag.nodes:
                continue
            sr = ranks_map[dep]
            tr = ranks_map[nid]
            sc = columns_map[dep]
            tc = columns_map[nid]
            cells: list[tuple[int, int]] = []
            # For each intervening rank between sr and tr, pick a column
            # by linear interpolation source -> target so the long edge
            # bends gradually rather than zig-zagging through node columns.
            if tr > sr + 1:
                span = tr - sr
                for k in range(1, span):
                    interp = sc + (tc - sc) * k / span
                    cells.append((sr + k, round(interp)))
            out.append(Edge(source=dep, target=nid, cells=cells))
    return out


def count_crossings(dag: DAG, ranks_map: dict[str, int], columns_map: dict[str, int]) -> int:
    """Count edge crossings between adjacent ranks. Used in tests.

    Two edges (a→b) and (c→d) on adjacent ranks cross iff
    (col(a) < col(c)) ^ (col(b) < col(d)). This is the standard
    Sugiyama crossing count for a single layer-pair.
    """
    by_rank: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for nid, n in dag.nodes.items():
        for dep in n.depends_on:
            if dep in dag.nodes and ranks_map[nid] == ranks_map[dep] + 1:
                by_rank[ranks_map[nid]].append((dep, nid))
    total = 0
    for pairs in by_rank.values():
        for i, (a, b) in enumerate(pairs):
            for c, d in pairs[i + 1 :]:
                ca, cc = columns_map[a], columns_map[c]
                cb, cd = columns_map[b], columns_map[d]
                if (ca < cc and cb > cd) or (ca > cc and cb < cd):
                    total += 1
    return total
