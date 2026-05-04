"""v3.5 Phase 2: multi-parent stacking.

Schema + Store helpers + merge-base name derivation + picker side-effects.
The actual `git merge` is exercised against a real bare repo where
useful, otherwise asserted via subprocess fakes — `construct_merge_base`
is thin enough that pattern-checking the call sequence is sufficient.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from quikode import stacking
from quikode.config import Config
from quikode.dag import DAG
from quikode.orchestrator import Orchestrator
from quikode.state import State, Store

# ----- Schema + Store helpers -----


def test_store_round_trip_multi_parent(tmp_path):
    store = Store(tmp_path / "q.db")
    store.upsert_pending("R-001")
    store.upsert_pending("R-002")
    store.upsert_pending("R-099")
    store.set_parent_chain(
        "R-099",
        parent_task_ids=["R-001", "R-002"],
        parent_branches=["quikode/r-001-aaa", "quikode/r-002-bbb"],
        parent_pr_branches=["quikode/r-001-aaa", "quikode/r-002-bbb"],
    )
    assert store.get_parent_task_ids("R-099") == ["R-001", "R-002"]
    assert store.get_parent_branches("R-099") == [
        "quikode/r-001-aaa",
        "quikode/r-002-bbb",
    ]
    # Legacy scalar columns also stamped (first entry).
    row = store.get("R-099")
    assert row["parent_task_id"] == "R-001"
    assert row["parent_branch"] == "quikode/r-001-aaa"


def test_store_clear_multi_parent(tmp_path):
    store = Store(tmp_path / "q.db")
    store.upsert_pending("R-099")
    store.set_parent_chain(
        "R-099",
        parent_task_ids=["R-001"],
        parent_branches=["quikode/r-001-aaa"],
    )
    assert store.get_parent_task_ids("R-099") == ["R-001"]
    # Clear by passing empty list.
    store.set_parent_chain("R-099", parent_task_ids=[])
    assert store.get_parent_task_ids("R-099") == []


def test_store_legacy_scalar_falls_through(tmp_path):
    """An old DB with only scalar parent_task_id should still appear in the
    JSON-array getter — backfill migration runs at Store init."""
    store = Store(tmp_path / "q.db")
    store.upsert_pending("R-099")
    # Write only the scalar (legacy shape).
    store.conn.execute(
        "UPDATE tasks SET parent_task_id = ?, parent_branch = ? WHERE id = ?",
        ("R-001", "quikode/r-001-aaa", "R-099"),
    )
    store.conn.commit()
    # Re-open Store: backfill should populate parent_task_ids.
    store2 = Store(tmp_path / "q.db")
    assert store2.get_parent_task_ids("R-099") == ["R-001"]


# ----- Merge-base branch naming -----


def test_merge_base_branch_name_deterministic_for_same_parents():
    n1 = stacking.compute_merge_base_branch_name("R-099", ["a", "b", "c"])
    n2 = stacking.compute_merge_base_branch_name("R-099", ["c", "b", "a"])
    # Sort-canonical so order doesn't matter.
    assert n1 == n2
    assert "r-099-base-" in n1


def test_merge_base_branch_name_changes_with_parent_set():
    n_with_b = stacking.compute_merge_base_branch_name("R-099", ["a", "b"])
    n_with_b_c = stacking.compute_merge_base_branch_name("R-099", ["a", "b", "c"])
    assert n_with_b != n_with_b_c


def test_merge_base_branch_name_empty_raises():
    with pytest.raises(ValueError):
        stacking.compute_merge_base_branch_name("R-099", [])


# ----- construct_merge_base subprocess shape -----


def test_construct_merge_base_octopus_succeeds(tmp_path, monkeypatch):
    """Octopus merge succeeds → returns sha from rev-parse HEAD."""
    calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["git", "checkout", "-B"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["git", "merge"] and "--no-ff" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "Merge made", "")
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, "abc1234567\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "unexpected")

    monkeypatch.setattr(stacking.subprocess, "run", _fake_run)
    sha = stacking.construct_merge_base(
        repo_path=tmp_path,
        parent_branches=["quikode/r-001-aaa", "quikode/r-002-bbb"],
        branch_name="quikode/r-099-base-deadbe",
    )
    assert sha == "abc1234567"
    # First call should be the checkout, second the octopus merge.
    assert calls[0][:3] == ["git", "checkout", "-B"]
    assert calls[1][:2] == ["git", "merge"]
    assert "quikode/r-001-aaa" in calls[1] and "quikode/r-002-bbb" in calls[1]


def test_construct_merge_base_octopus_fails_falls_back_to_sequential(tmp_path, monkeypatch):
    """When octopus fails, the helper aborts + retries pairwise. Sequential
    success → returns sha."""
    state = {"phase": "octopus", "merges_done": 0}

    def _fake_run(cmd, **kwargs):
        # Strip the leading `git` plus any `-c key=value` options so we
        # can match on the actual git verb.
        verb_idx = 1
        while verb_idx < len(cmd) and cmd[verb_idx] == "-c":
            verb_idx += 2  # skip `-c` + its value
        verb = cmd[verb_idx] if verb_idx < len(cmd) else ""
        rest = cmd[verb_idx + 1 :]
        if verb == "checkout" and rest[:1] == ["-B"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if verb == "merge" and "--abort" in rest:
            state["phase"] = "sequential"
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if verb == "merge" and "--no-ff" in rest:
            if (
                state["phase"] == "octopus"
                and cmd.count("quikode/r-001-aaa") + cmd.count("quikode/r-002-bbb") == 2
            ):
                return subprocess.CompletedProcess(cmd, 1, "", "CONFLICT")
            # Sequential form has a single branch.
            state["merges_done"] += 1
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if verb == "rev-parse" and rest[:1] == ["HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, "deadbeef00\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "unexpected")

    monkeypatch.setattr(stacking.subprocess, "run", _fake_run)
    sha = stacking.construct_merge_base(
        repo_path=tmp_path,
        parent_branches=["quikode/r-001-aaa", "quikode/r-002-bbb"],
        branch_name="quikode/r-099-base-deadbe",
    )
    assert sha == "deadbeef00"
    assert state["merges_done"] == 2  # both sequential merges ran


def test_construct_merge_base_returns_none_on_unrecoverable_conflict(tmp_path, monkeypatch):
    """When sequential merges also conflict, the helper aborts + returns None."""

    def _fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "checkout", "-B"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["git", "merge"] and "--abort" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["git", "merge"] and "--no-ff" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "CONFLICT")
        return subprocess.CompletedProcess(cmd, 1, "", "unexpected")

    monkeypatch.setattr(stacking.subprocess, "run", _fake_run)
    sha = stacking.construct_merge_base(
        repo_path=tmp_path,
        parent_branches=["quikode/r-001-aaa", "quikode/r-002-bbb"],
        branch_name="quikode/r-099-base-cafe00",
    )
    assert sha is None


def test_construct_merge_base_empty_parents_returns_none(tmp_path):
    sha = stacking.construct_merge_base(
        repo_path=tmp_path,
        parent_branches=[],
        branch_name="quikode/x",
    )
    assert sha is None


# ----- Picker side-effects: multi-parent stamping -----


def _make_dag(tmp_path: Path, edges: list[tuple[str, list[str]]]) -> DAG:
    nodes = []
    for nid, deps in edges:
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


def test_picker_stamps_multi_parent_chain(tmp_path):
    """When a child has 2 stack-ready parents, the picker writes both to
    parent_task_ids. The legacy scalar column gets the FIRST sorted id."""
    edges = [
        ("R-001", []),
        ("R-002", []),
        ("R-099", ["R-001", "R-002"]),
    ]
    dag = _make_dag(tmp_path, edges)
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        stacking_strategy="within-milestone",
    )
    store = Store(tmp_path / "q.db")
    o = Orchestrator(cfg, dag, store)
    for nid, _ in edges:
        store.upsert_pending(nid)
    # Two parents both in PENDING_CI (the v3.5 "PR open" resting state).
    store.transition("R-001", State.PENDING_CI, branch="quikode/r-001-aaa")
    store.transition("R-002", State.PENDING_CI, branch="quikode/r-002-bbb")

    nxt = o._pick_next({"R-001", "R-002", "R-099"}, set())
    assert nxt == "R-099"
    # Multi-parent stamping landed.
    assert store.get_parent_task_ids("R-099") == ["R-001", "R-002"]
    branches = store.get_parent_branches("R-099")
    assert "quikode/r-001-aaa" in branches and "quikode/r-002-bbb" in branches
    # Legacy scalar = first entry, deterministic.
    row = store.get("R-099")
    assert row["parent_task_id"] == "R-001"


def test_picker_clears_parent_chain_on_fresh_root(tmp_path):
    """A fresh-root pick that has stale parent metadata from a prior round
    should clear it. Otherwise the worker's provisioning would try to fork
    off a defunct parent."""
    edges = [("R-005", [])]
    dag = _make_dag(tmp_path, edges)
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json")
    store = Store(tmp_path / "q.db")
    o = Orchestrator(cfg, dag, store)
    store.upsert_pending("R-005")
    # Stale stack metadata from a prior round.
    store.set_parent_chain(
        "R-005",
        parent_task_ids=["R-OLD"],
        parent_branches=["quikode/r-old-aaa"],
    )
    store.set_parent_merge_base("R-005", branch="quikode/r-005-base-stale", sha="badc0ffee0")

    nxt = o._pick_next({"R-005"}, set())
    assert nxt == "R-005"
    assert store.get_parent_task_ids("R-005") == []
    row = store.get("R-005")
    assert row["parent_merge_base_branch"] is None
    assert row["parent_merge_base_sha"] is None


def test_picker_single_parent_unchanged(tmp_path):
    """The single-parent case (legacy stacking) still stamps cleanly into
    the new array columns + the legacy scalars in lockstep."""
    edges = [("R-001", []), ("R-002", ["R-001"])]
    dag = _make_dag(tmp_path, edges)
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        stacking_strategy="within-milestone",
    )
    store = Store(tmp_path / "q.db")
    o = Orchestrator(cfg, dag, store)
    store.upsert_pending("R-001")
    store.upsert_pending("R-002")
    store.transition("R-001", State.PENDING_CI, branch="quikode/r-001-aaa")
    nxt = o._pick_next({"R-001", "R-002"}, set())
    assert nxt == "R-002"
    assert store.get_parent_task_ids("R-002") == ["R-001"]
    assert store.get("R-002")["parent_task_id"] == "R-001"
    assert store.get("R-002")["parent_branch"] == "quikode/r-001-aaa"


def test_set_parent_merge_base_round_trip(tmp_path):
    store = Store(tmp_path / "q.db")
    store.upsert_pending("R-099")
    store.set_parent_merge_base("R-099", branch="quikode/r-099-base-cafeee", sha="abc1234567")
    row = store.get("R-099")
    assert row["parent_merge_base_branch"] == "quikode/r-099-base-cafeee"
    assert row["parent_merge_base_sha"] == "abc1234567"
    # Clear path
    store.set_parent_merge_base("R-099", branch=None, sha=None)
    row2 = store.get("R-099")
    assert row2["parent_merge_base_branch"] is None
    assert row2["parent_merge_base_sha"] is None
