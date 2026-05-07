"""Fresh workspace services."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .dag import DAG, Node
from .state import Store

_NODE_SUBJECT_RE = re.compile(r"^(?P<node_id>[A-Za-z0-9][A-Za-z0-9_.-]*):")


@dataclass(frozen=True)
class SeedResult:
    merged: dict[str, str]
    pending: list[str]


def seed_from_base(
    cfg: Config,
    store: Store,
    *,
    merged_nodes_file: Path | None = None,
) -> SeedResult:
    """Seed a fresh store with DAG nodes deterministically known to be on the base branch."""

    dag = DAG.load(cfg.dag_path)
    evidence = _collect_seed_evidence(cfg, dag, merged_nodes_file=merged_nodes_file)
    for node_id, detail in sorted(evidence.items()):
        if node_id in dag.nodes:
            source, _, value = detail.partition(":")
            store.seed_merged_node(node_id, source=source, evidence=value or detail)

    merged = {node_id: evidence[node_id] for node_id in sorted(evidence) if node_id in dag.nodes}
    pending = [node_id for node_id in sorted(dag.nodes) if node_id not in merged]
    return SeedResult(merged=merged, pending=pending)


def seed_from_main(
    cfg: Config,
    store: Store,
    *,
    merged_nodes_file: Path | None = None,
) -> SeedResult:
    """Compatibility alias for older callers; uses cfg.base_branch."""
    return seed_from_base(cfg, store, merged_nodes_file=merged_nodes_file)


def _collect_seed_evidence(
    cfg: Config,
    dag: DAG,
    *,
    merged_nodes_file: Path | None,
) -> dict[str, str]:
    evidence: dict[str, str] = {}
    for node in dag.nodes.values():
        dag_evidence = _dag_node_evidence(node)
        if dag_evidence:
            evidence[node.id] = dag_evidence
    for node_id, detail in _git_subject_evidence(cfg.repo_path, cfg.pr_remote, cfg.base_branch).items():
        evidence.setdefault(node_id, detail)
    if merged_nodes_file is not None:
        evidence.update(_explicit_file_evidence(merged_nodes_file))
    return evidence


def _dag_node_evidence(node: Node) -> str | None:
    if node.raw.get("merged_in_main") is True:
        return "dag:merged_in_main=true"
    if node.raw.get("status") == "merged":
        return 'dag:status="merged"'
    return None


def _git_subject_evidence(repo_path: Path, remote: str, base_branch: str) -> dict[str, str]:
    result = subprocess.run(
        ["git", "log", "--format=%s", f"{remote}/{base_branch}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return {}
    evidence: dict[str, str] = {}
    for subject in result.stdout.splitlines():
        match = _NODE_SUBJECT_RE.match(subject)
        if match:
            node_id = match.group("node_id")
            evidence.setdefault(node_id, f"git-subject:{subject}")
    return evidence


def _explicit_file_evidence(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        return {str(node_id): _stringify_evidence(detail) for node_id, detail in data.items()}
    if isinstance(data, list):
        out: dict[str, str] = {}
        for item in data:
            if isinstance(item, str):
                out[item] = "explicit-file:list-entry"
            elif isinstance(item, dict) and "id" in item:
                out[str(item["id"])] = _stringify_evidence(item.get("evidence") or item)
        return out
    raise ValueError("merged nodes file must be a JSON object or list")


def _stringify_evidence(value: Any) -> str:
    if isinstance(value, str):
        return f"explicit-file:{value}"
    return "explicit-file:" + json.dumps(value, sort_keys=True, default=str)
