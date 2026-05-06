"""DAG loading + ready-task scheduling.

Schema follows tanren's docs/roadmap/dag.json. Each node has:
  id, kind, milestone, title, depends_on[], scope,
  completes_behaviors[], expected_evidence[], playbook[], ...
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Node:
    id: str
    kind: str
    milestone: str
    title: str
    scope: str
    depends_on: tuple[str, ...]
    completes_behaviors: tuple[str, ...]
    supports_behaviors: tuple[str, ...]
    boundary_with_neighbors: str
    expected_evidence: tuple[dict, ...]
    playbook: tuple[str, ...]
    rationale: str
    risks: tuple[str, ...]
    raw: dict = field(repr=False, compare=False)


@dataclass
class DAG:
    nodes: dict[str, Node]
    milestones: dict[str, dict]
    raw: dict = field(repr=False, compare=False)

    @classmethod
    def load(cls, path: Path) -> DAG:
        raw = json.loads(Path(path).read_text())
        nodes: dict[str, Node] = {}
        for n in raw.get("nodes", []):
            nodes[n["id"]] = Node(
                id=n["id"],
                kind=n.get("kind", "behavior"),
                milestone=n["milestone"],
                title=n.get("title", ""),
                scope=n.get("scope", ""),
                depends_on=tuple(n.get("depends_on", []) or []),
                completes_behaviors=tuple(n.get("completes_behaviors", []) or []),
                supports_behaviors=tuple(n.get("supports_behaviors", []) or []),
                boundary_with_neighbors=n.get("boundary_with_neighbors", ""),
                expected_evidence=tuple(n.get("expected_evidence", []) or []),
                playbook=tuple(n.get("playbook", []) or []),
                rationale=n.get("rationale", ""),
                risks=tuple(n.get("risks", []) or []),
                raw=n,
            )
        milestones = {m["id"]: m for m in raw.get("milestones", [])}
        return cls(nodes=nodes, milestones=milestones, raw=raw)

    def topo_layers(self) -> list[list[str]]:
        """Return nodes grouped by dependency depth (layer 0 = no deps)."""
        depth: dict[str, int] = {}
        for nid in self._topo_order():
            n = self.nodes[nid]
            depth[nid] = 0 if not n.depends_on else 1 + max(depth[d] for d in n.depends_on if d in depth)
        layers: dict[int, list[str]] = defaultdict(list)
        for nid, d in depth.items():
            layers[d].append(nid)
        return [layers[d] for d in sorted(layers)]

    def _topo_order(self) -> list[str]:
        indeg = dict.fromkeys(self.nodes, 0)
        rev: dict[str, list[str]] = defaultdict(list)
        for nid, n in self.nodes.items():
            for dep in n.depends_on:
                if dep in self.nodes:
                    indeg[nid] += 1
                    rev[dep].append(nid)
        ready = [nid for nid, d in indeg.items() if d == 0]
        out: list[str] = []
        while ready:
            ready.sort()  # deterministic
            cur = ready.pop(0)
            out.append(cur)
            for nxt in rev[cur]:
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    ready.append(nxt)
        if len(out) != len(self.nodes):
            raise ValueError("cycle in DAG")
        return out

    def ready_nodes(
        self, completed_ids: set[str], in_progress_ids: set[str], skip_kinds: set[str] | None = None
    ) -> list[Node]:
        """Nodes whose deps are all in `completed_ids` and not yet started."""
        skip_kinds = skip_kinds or set()
        ready: list[Node] = []
        for nid, n in self.nodes.items():
            if nid in completed_ids or nid in in_progress_ids:
                continue
            if n.kind in skip_kinds:
                continue
            if all(d in completed_ids for d in n.depends_on):
                ready.append(n)
        ready.sort(key=lambda x: x.id)
        return ready

    def filter(self, ids: Iterable[str] | None = None, milestone: str | None = None) -> set[str]:
        """Return the closure of node IDs to run, including transitive deps."""
        if ids is None and milestone is None:
            return set(self.nodes)
        seed: set[str] = set()
        if ids:
            seed |= {i for i in ids if i in self.nodes}
        if milestone:
            seed |= {nid for nid, n in self.nodes.items() if n.milestone == milestone}
        # transitive closure of deps
        out = set()
        stack = list(seed)
        while stack:
            cur = stack.pop()
            if cur in out:
                continue
            out.add(cur)
            n = self.nodes.get(cur)
            if n:
                stack.extend(n.depends_on)
        return out

    def descendants_of(self, node_id: str) -> set[str]:
        """All node IDs that transitively depend on `node_id`."""
        rev: dict[str, list[str]] = defaultdict(list)
        for nid, n in self.nodes.items():
            for d in n.depends_on:
                rev[d].append(nid)
        out: set[str] = set()
        stack = [node_id]
        while stack:
            cur = stack.pop()
            for child in rev.get(cur, []):
                if child not in out:
                    out.add(child)
                    stack.append(child)
        return out

    def ancestors_of(self, node_id: str) -> set[str]:
        """All node IDs that `node_id` transitively depends on."""
        n = self.nodes.get(node_id)
        if n is None:
            return set()
        out: set[str] = set()
        stack = list(n.depends_on)
        while stack:
            cur = stack.pop()
            if cur in out:
                continue
            out.add(cur)
            dn = self.nodes.get(cur)
            if dn:
                stack.extend(dn.depends_on)
        return out

    def stats(self) -> dict[str, Any]:
        layers = self.topo_layers()
        return {
            "node_count": len(self.nodes),
            "milestone_count": len(self.milestones),
            "depth": len(layers),
            "max_layer_width": max((len(layer) for layer in layers), default=0),
        }
