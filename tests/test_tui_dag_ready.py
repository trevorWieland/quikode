"""Header surfaces "DAG-ready but not yet seeded" count, and /ready lists them."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from quikode.config_template import DEFAULT_CONFIG_TOML
from quikode.state import State, Store
from quikode.tui.app import QuikodeTUI
from quikode.tui.controllers.store_polls import StorePoller


def _bootstrap(tmp_path: Path) -> Path:
    qkdir = tmp_path / ".quikode"
    qkdir.mkdir()
    dag_path = tmp_path / "dag.json"
    dag = {
        "schema": "test",
        "milestones": [{"id": "M-1", "title": "x", "goal": "x", "status": "planned"}],
        "nodes": [
            {
                "id": "F-001",
                "kind": "foundation",
                "milestone": "M-1",
                "title": "Foundation",
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
                "id": "R-001",
                "kind": "behavior",
                "milestone": "M-1",
                "title": "Ready node A",
                "scope": "x",
                "depends_on": ["F-001"],
                "completes_behaviors": [],
                "supports_behaviors": [],
                "boundary_with_neighbors": "",
                "expected_evidence": [],
                "playbook": [],
                "rationale": "",
                "risks": [],
            },
            {
                "id": "R-002",
                "kind": "behavior",
                "milestone": "M-1",
                "title": "Ready node B (not yet seeded)",
                "scope": "x",
                "depends_on": ["F-001"],
                "completes_behaviors": [],
                "supports_behaviors": [],
                "boundary_with_neighbors": "",
                "expected_evidence": [],
                "playbook": [],
                "rationale": "",
                "risks": [],
            },
            {
                "id": "R-003",
                "kind": "behavior",
                "milestone": "M-1",
                "title": "Blocked node",
                "scope": "x",
                "depends_on": ["R-001"],
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
    dag_path.write_text(json.dumps(dag))
    (qkdir / "config.toml").write_text(
        DEFAULT_CONFIG_TOML.format(repo_path=str(tmp_path), dag_path=str(dag_path))
    )
    return tmp_path


def test_dag_ready_unseeded_counts_unseeded_descendants(tmp_path):
    _bootstrap(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    # Seed only F-001 (merged) and R-001 (pending). R-002 is in DAG but not seeded.
    store.upsert_pending("F-001")
    store.transition("F-001", State.MERGED)
    store.upsert_pending("R-001")  # pending, in store
    # R-002, R-003 not yet seeded.

    poller = StorePoller(workspace=tmp_path)
    snap = poller.poll()
    # R-002 is ready (its dep F-001 is merged) and not yet seeded → counts as 1.
    # R-003 depends on R-001 which is still pending, so it's NOT ready.
    # R-001 is ready but already seeded — not counted in the "unseeded" surface.
    assert snap.header.dag_ready_unseeded == 1


def test_dag_ready_unseeded_zero_when_all_ready_seeded(tmp_path):
    _bootstrap(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("F-001")
    store.transition("F-001", State.MERGED)
    store.upsert_pending("R-001")
    store.upsert_pending("R-002")  # now both ready nodes are seeded
    snap = StorePoller(workspace=tmp_path).poll()
    assert snap.header.dag_ready_unseeded == 0


def test_dag_total_in_scope_is_dag_node_count(tmp_path):
    """total_in_scope was the number of seeded tasks; now it's the DAG node
    count so the header's merged % reflects DAG progress."""
    _bootstrap(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("F-001")
    store.transition("F-001", State.MERGED)
    snap = StorePoller(workspace=tmp_path).poll()
    assert snap.header.total_in_scope == 4  # F-001, R-001, R-002, R-003


def test_dag_cache_invalidates_on_mtime_change(tmp_path):
    """Editing dag.json should be picked up on the next poll without restart."""
    _bootstrap(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("F-001")
    store.transition("F-001", State.MERGED)
    store.upsert_pending("R-001")

    poller = StorePoller(workspace=tmp_path)
    snap1 = poller.poll()
    assert snap1.header.total_in_scope == 4

    # Add a 5th node to the DAG
    dag_path = tmp_path / "dag.json"
    dag = json.loads(dag_path.read_text())
    dag["nodes"].append(
        {
            "id": "R-004",
            "kind": "behavior",
            "milestone": "M-1",
            "title": "fifth",
            "scope": "x",
            "depends_on": ["F-001"],
            "completes_behaviors": [],
            "supports_behaviors": [],
            "boundary_with_neighbors": "",
            "expected_evidence": [],
            "playbook": [],
            "rationale": "",
            "risks": [],
        }
    )
    # Bump mtime explicitly — same-second writes can otherwise tie.
    dag_path.write_text(json.dumps(dag))
    later = time.time() + 1
    os.utime(dag_path, (later, later))

    snap2 = poller.poll()
    assert snap2.header.total_in_scope == 5
    # R-004 also counts as a new ready-unseeded node (depends on merged F-001).
    assert snap2.header.dag_ready_unseeded >= 1


@pytest.mark.asyncio
async def test_slash_ready_lists_unseeded_nodes(tmp_path):
    _bootstrap(tmp_path)
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("F-001")
    store.transition("F-001", State.MERGED)

    app = QuikodeTUI(workspace=tmp_path, poll_interval_s=0.05)
    async with app.run_test() as pilot:
        await pilot.pause()
        # R-001 and R-002 are both ready (depend only on merged F-001),
        # neither is seeded. /ready should list them without crashing.
        app._dispatch_slash("/ready")
        await pilot.pause()
        assert app.is_running
