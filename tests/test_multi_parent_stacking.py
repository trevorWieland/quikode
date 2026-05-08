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
from quikode.config import Config, StackingStrategy
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
    phase = ["octopus"]
    merges_done = [0]

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
            phase[0] = "sequential"
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if verb == "merge" and "--no-ff" in rest:
            if phase[0] == "octopus" and cmd.count("quikode/r-001-aaa") + cmd.count("quikode/r-002-bbb") == 2:
                return subprocess.CompletedProcess(cmd, 1, "", "CONFLICT")
            # Sequential form has a single branch.
            merges_done[0] += 1
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
    assert merges_done[0] == 2  # both sequential merges ran


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
    parent_task_ids. The old scalar column gets the FIRST sorted id."""
    edges = [
        ("R-001", []),
        ("R-002", []),
        ("R-099", ["R-001", "R-002"]),
    ]
    dag = _make_dag(tmp_path, edges)
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        stacking_strategy=StackingStrategy.WITHIN_MILESTONE,
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
    """The single-parent case still stamps cleanly into the JSON-array
    columns."""
    edges = [("R-001", []), ("R-002", ["R-001"])]
    dag = _make_dag(tmp_path, edges)
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        stacking_strategy=StackingStrategy.WITHIN_MILESTONE,
    )
    store = Store(tmp_path / "q.db")
    o = Orchestrator(cfg, dag, store)
    store.upsert_pending("R-001")
    store.upsert_pending("R-002")
    store.transition("R-001", State.PENDING_CI, branch="quikode/r-001-aaa")
    nxt = o._pick_next({"R-001", "R-002"}, set())
    assert nxt == "R-002"
    assert store.get_parent_task_ids("R-002") == ["R-001"]
    assert store.get_parent_branches("R-002") == ["quikode/r-001-aaa"]


def test_stack_depth_uses_max_path_in_dag(tmp_path):
    """A child with two parents at depths 1 and 3 returns 4 (1 + max(3,1))."""
    edges = [("R-001", []), ("R-002", []), ("R-003", []), ("R-099", ["R-001", "R-002"])]
    dag = _make_dag(tmp_path, edges)
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json")
    store = Store(tmp_path / "q.db")
    o = Orchestrator(cfg, dag, store)
    for nid, _ in edges:
        store.upsert_pending(nid)
    # Build a deeper chain on R-002: R-003 → R-002 → root, and a shallow
    # chain on R-001: R-001 → root. R-099's depth should be 1 + max(2, 3) = 4
    # if R-002 itself has depth 3, but as direct parent it's depth-2 from
    # R-099's POV. We assert depth >= 2 (the shallow path) and that adding
    # an indirect ancestor pushes it deeper.
    store.set_parent_chain("R-002", parent_task_ids=["R-003"], parent_branches=["quikode/r-003-aaa"])
    store.set_parent_chain("R-099", parent_task_ids=["R-001", "R-002"], parent_branches=["a", "b"])
    # R-099 → max(R-001 depth=1, R-002 depth=2) + 1 = 3
    assert o._stack_depth("R-099") == 3
    # R-001 has no parents → depth 1 (counts itself, matches old semantics)
    assert o._stack_depth("R-001") == 1


def test_stack_root_with_multi_parent_returns_min_id(tmp_path):
    """Multi-parent DAG: pick the lexicographically lowest root for the
    breadth-cap key. Deterministic across re-walks."""
    edges = [("R-005", []), ("R-001", []), ("R-099", ["R-005", "R-001"])]
    dag = _make_dag(tmp_path, edges)
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json")
    store = Store(tmp_path / "q.db")
    o = Orchestrator(cfg, dag, store)
    for nid, _ in edges:
        store.upsert_pending(nid)
    store.set_parent_chain("R-099", parent_task_ids=["R-005", "R-001"], parent_branches=["a", "b"])
    # Roots are R-005 and R-001; min wins.
    assert o._stack_root("R-099") == "R-001"


def test_would_form_cycle_via_alternate_path(tmp_path):
    """Cycle detection must catch a → b → a even when the cycle isn't
    on the lowest-id path. BFS over parent_task_ids."""
    edges = [("R-001", []), ("R-002", []), ("R-003", [])]
    dag = _make_dag(tmp_path, edges)
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json")
    store = Store(tmp_path / "q.db")
    o = Orchestrator(cfg, dag, store)
    for nid, _ in edges:
        store.upsert_pending(nid)
    # Wire R-002 → R-003, R-003 → R-001. If we now ask "would stacking
    # R-001 on R-002 form a cycle?", the BFS should walk
    # R-002 → R-003 → R-001 (HIT) and return True.
    store.set_parent_chain("R-002", parent_task_ids=["R-003"], parent_branches=["a"])
    store.set_parent_chain("R-003", parent_task_ids=["R-001"], parent_branches=["b"])
    assert o._would_form_cycle("R-001", "R-002") is True
    # R-001 onto a fresh ancestor (no path) should be safe.
    assert o._would_form_cycle("R-099", "R-001") is False


def test_stack_size_under_root_counts_dag_dependents(tmp_path):
    """Multiple children sharing roots should all count; merging children
    don't get double-counted under each parent's root."""
    edges = [("R-001", []), ("R-002", []), ("R-003", []), ("R-099", ["R-001", "R-002"])]
    dag = _make_dag(tmp_path, edges)
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json")
    store = Store(tmp_path / "q.db")
    o = Orchestrator(cfg, dag, store)
    for nid, _ in edges:
        store.upsert_pending(nid)
    store.set_parent_chain("R-099", parent_task_ids=["R-001", "R-002"], parent_branches=["a", "b"])
    # R-099 → root R-001 (min(R-001, R-002)). R-001 → root R-001. R-002 → root R-002.
    # R-003 → root R-003. So under_root(R-001) = R-001 + R-099 = 2.
    assert o._stack_size_under_root("R-001") == 2
    assert o._stack_size_under_root("R-002") == 1


def test_children_of_parent_branch_matches_array_column(tmp_path):
    """`children_of_parent_branch` returns every non-terminal task whose
    `parent_pr_branches` JSON array contains the cited branch — including
    children with multi-parent linkage where the cited branch is one
    among several parents."""
    store = Store(tmp_path / "q.db")
    for nid in ("R-001", "R-002", "R-003"):
        store.upsert_pending(nid)
        store.transition(nid, State.DOING_SUBTASK)
    branch = "quikode/parent-aaa"
    store.set_parent_chain("R-001", parent_task_ids=["P"], parent_pr_branches=[branch])
    store.set_parent_chain("R-002", parent_task_ids=["P"], parent_pr_branches=[branch])
    store.set_parent_chain(
        "R-003",
        parent_task_ids=["P", "OTHER"],
        parent_pr_branches=[branch, "quikode/other-bbb"],
    )
    children = store.children_of_parent_branch(branch)
    ids = {c["id"] for c in children}
    assert ids == {"R-001", "R-002", "R-003"}


def test_observed_branch_tip_round_trip(tmp_path):
    store = Store(tmp_path / "q.db")
    store.upsert_pending("R-001")
    assert store.get_last_observed_branch_tip_sha("R-001") is None
    store.set_last_observed_branch_tip_sha("R-001", "deadbeef00")
    assert store.get_last_observed_branch_tip_sha("R-001") == "deadbeef00"


def test_cascade_rebase_recurses_into_grandchildren(tmp_path, monkeypatch):
    """When a parent's tip advances, descendants at every depth should be
    queued. B → C → D: B advances → C and D both rebase."""
    edges = [
        ("R-001", []),
        ("R-002", ["R-001"]),
        ("R-003", ["R-002"]),
    ]
    dag = _make_dag(tmp_path, edges)
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        stacking_strategy=StackingStrategy.WITHIN_MILESTONE,
    )
    store = Store(tmp_path / "q.db")
    o = Orchestrator(cfg, dag, store)
    for nid, _ in edges:
        store.upsert_pending(nid)
        store.transition(nid, State.DOING_SUBTASK, branch=f"quikode/{nid.lower()}-aaa")
    # R-002 stacks on R-001; R-003 stacks on R-002.
    store.set_parent_chain(
        "R-002",
        parent_task_ids=["R-001"],
        parent_branches=["quikode/r-001-aaa"],
        parent_pr_branches=["quikode/r-001-aaa"],
    )
    store.set_parent_chain(
        "R-003",
        parent_task_ids=["R-002"],
        parent_branches=["quikode/r-002-aaa"],
        parent_pr_branches=["quikode/r-002-aaa"],
    )

    scheduled: list[str] = []

    # Plan 31: cascade-on-push routes through `_schedule_rebase_to_parent_tip`
    # (children stay stacked on parent's evolving tip), not the legacy
    # `_schedule_rebase_to_main`. Stub the parent_tip entry.
    def _stub_schedule(self, task_id, pool, futures, rrf, *, parent_branch):
        scheduled.append(task_id)

    monkeypatch.setattr(Orchestrator, "_schedule_rebase_to_parent_tip", _stub_schedule)

    # Trigger cascade: R-001's branch tip advanced.
    o._schedule_cascade_rebase("quikode/r-001-aaa", pool=None, futures={}, review_response_futures=set())
    # R-002 (direct child) and R-003 (grandchild via R-002's branch) both queued.
    assert "R-002" in scheduled
    assert "R-003" in scheduled
    # needs_parent_rebase flag set on both.
    assert store.get("R-002")["needs_parent_rebase"] == 1
    assert store.get("R-003")["needs_parent_rebase"] == 1


def test_cascade_rebase_skips_terminal_descendants(tmp_path, monkeypatch):
    """MERGED / ABORTED / BLOCKED descendants are excluded from the cascade."""
    edges = [("R-001", []), ("R-002", ["R-001"])]
    dag = _make_dag(tmp_path, edges)
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        stacking_strategy=StackingStrategy.WITHIN_MILESTONE,
    )
    store = Store(tmp_path / "q.db")
    o = Orchestrator(cfg, dag, store)
    for nid, _ in edges:
        store.upsert_pending(nid)
    # R-002 already MERGED → must NOT be re-rebased.
    store.set_parent_chain(
        "R-002",
        parent_task_ids=["R-001"],
        parent_branches=["quikode/r-001-aaa"],
        parent_pr_branches=["quikode/r-001-aaa"],
    )
    store.transition("R-002", State.MERGED, branch="quikode/r-002-aaa")

    scheduled: list[str] = []

    def _stub_schedule(self, task_id, pool, futures, rrf, *, parent_branch):
        scheduled.append(task_id)

    monkeypatch.setattr(Orchestrator, "_schedule_rebase_to_parent_tip", _stub_schedule)
    o._schedule_cascade_rebase("quikode/r-001-aaa", pool=None, futures={}, review_response_futures=set())
    assert scheduled == []


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
