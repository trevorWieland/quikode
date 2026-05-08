"""Plan 32 — merge-node worker behavior.

PR-A 3/3 covered: id determinism, octopus / sequential merge dispatch.
PR-B extends: merge-planner doer-subloop on sequential conflict, audit
gauntlet wiring (`merge_node_mode=True`).

These tests stub `git`/`exec_in` rather than spinning a real container.
The merge-node worker's contract is "drive the FSM through deterministic
git steps + (on conflict) plan-driven integration subtasks + audit
gauntlet, then fire MERGE_NODE_BUILT". Real-git e2e coverage lives in
`test_stacking_e2e_git.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from quikode import fsm_runtime, merge_node, pre_pr_audit, prompts
from quikode.config import Config
from quikode.dag import DAG, Node
from quikode.evaluation_contract import build_for
from quikode.fsm import State
from quikode.state import Store
from quikode.subtask_schema import Plan, Subtask
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


def _node_for_merge(mn_id: str, parent_ids: list[str]) -> Node:
    return Node(
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


class _StubbedWorker(MergeNodeWorker):
    """Test double that bypasses container provisioning + git network IO."""

    def __init__(self, cfg, dag, store, node, *, mn_id, octopus_succeeds, sequential_succeeds, git_calls):
        super().__init__(cfg, dag, store, node)
        self._test_mn_id = mn_id
        self._test_octopus_succeeds = octopus_succeeds
        self._test_sequential_succeeds = sequential_succeeds
        self._test_git_calls = git_calls

    def _provision_merge_node_worktree(self) -> None:
        if fsm_runtime.current_state(self.store, self._test_mn_id) is State.PENDING:
            fsm_runtime.start_task(self.store, self._test_mn_id, note="test stub")

    def _provision_container(self, wt_path) -> None:
        del wt_path

    def _teardown(self) -> None:
        return None

    def _git_in_workspace(self, args: list[str]) -> tuple[int, str]:
        self._test_git_calls.append(list(args))
        if "merge" in args and "--no-ff" in args and "--abort" not in args:
            remote_refs = [a for a in args if a.startswith("origin/quikode/")]
            if len(remote_refs) >= 2:
                return (0 if self._test_octopus_succeeds else 1, "octopus merge attempt")
            if len(remote_refs) == 1:
                return (0 if self._test_sequential_succeeds else 1, "sequential merge")
        if args[:1] == ["diff"]:
            return (0, "diff --git a/x b/x\n+changed line\n")
        return (0, "ok")


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
    audit_outcome=None,
) -> _StubbedWorker:
    """Construct a stubbed MergeNodeWorker. The audit gauntlet is
    short-circuited via monkeypatching `_run_pre_pr_pipeline`."""
    dag = _empty_dag(tmp_path)
    parent_ids = store.get_parent_task_ids(mn_id)
    node = _node_for_merge(mn_id, parent_ids)
    git_calls = git_calls if git_calls is not None else []

    worker = _StubbedWorker(
        cfg,
        dag,
        store,
        node,
        mn_id=mn_id,
        octopus_succeeds=octopus_succeeds,
        sequential_succeeds=sequential_succeeds,
        git_calls=git_calls,
    )
    store.set_field(mn_id, worktree_path=str(tmp_path / "wt"), container_id="fake")

    class FakeHandle:
        def __init__(self) -> None:
            self.unit_id = "fake"
            self.metadata: dict[str, Any] = {"container_id": "fake"}

    worker.handle = FakeHandle()

    def fake_exec_in(handle, cmd, log_path=None, timeout=None):
        return (0, "fetched", "")

    monkeypatch.setattr(mnw_mod, "exec_in", fake_exec_in)

    def fake_audit(self, *, merge_node_mode):
        del merge_node_mode
        # Real pipeline leaves the row in PRE_PR_AUDITING on pass; stub
        # mimics that so the merge_node_built event has a valid source.
        if audit_outcome is None:
            fsm_runtime.enter_pre_pr_auditing(self.store, self.node.id, note="stub: pipeline pass")
        return audit_outcome

    monkeypatch.setattr(MergeNodeWorker, "_run_pre_pr_pipeline", fake_audit, raising=False)
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
    """Trivial octopus merge → audit gauntlet pass → MERGE_NODE_READY."""
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
        octopus_succeeds=True,
        audit_outcome=None,
    )

    outcome = worker.run()

    assert outcome.final_state == State.MERGE_NODE_READY
    assert fsm_runtime.current_state(store, mn_id) is State.MERGE_NODE_READY
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
        audit_outcome=None,
    )

    outcome = worker.run()

    assert outcome.final_state == State.MERGE_NODE_READY
    sequential_calls = [
        c
        for c in git_calls
        if "merge" in c
        and "--no-ff" in c
        and "--abort" not in c
        and len([a for a in c if a.startswith("origin/quikode/")]) == 1
    ]
    assert len(sequential_calls) == 2, f"expected 2 sequential merges; got {sequential_calls}"


# ----- worker lifecycle: sequential conflict triggers merge-planner subloop -----


def _stub_merge_planner_subloop_with_plan(monkeypatch, mn_id: str):
    """Helper: replace `_invoke_merge_planner` with a stub that returns a
    canned 2-subtask plan, and replace `_run_subtask_set` with a no-op
    that records its argument."""
    captured: dict[str, Any] = {}

    canned_plan = Plan(
        node_id=mn_id,
        summary="resolve cross-parent conflict",
        subtasks=(
            Subtask(
                id="S-01-resolve-foo",
                title="Resolve src/foo.py",
                depends_on=(),
                files_to_touch=("src/foo.py",),
                boundary="src/foo.py only",
                acceptance=("no <<<<<< markers in src/foo.py",),
                notes="",
                kind="merge-integration",
            ),
            Subtask(
                id="S-99-verify",
                title="Verify both parents still pass",
                depends_on=("S-01-resolve-foo",),
                files_to_touch=(),
                boundary="no production code edits",
                acceptance=("just ci passes",),
                notes="",
                kind="merge-integration",
            ),
        ),
        final_acceptance=("just ci passes",),
    )

    def fake_invoke(self, parent_ids, parent_branches):
        captured["parent_ids"] = list(parent_ids)
        captured["parent_branches"] = list(parent_branches)
        return canned_plan

    def fake_run_subtasks(self, subtasks):
        captured["subtasks_run"] = [s.id for s in subtasks]
        # Mimic per-subtask loop's terminal trail: the last pass leaves
        # the row in PUSHING (DOING_SUBTASK → CHECKING_SUBTASK → COMMITTING → PUSHING).
        fsm_runtime.enter_checking_subtask(self.store, self.node.id, note="stub")
        fsm_runtime.enter_committing(self.store, self.node.id, note="stub")
        fsm_runtime.enter_pushing(self.store, self.node.id, note="stub")

    monkeypatch.setattr(MergeNodeWorker, "_invoke_merge_planner", fake_invoke, raising=False)
    monkeypatch.setattr(MergeNodeWorker, "_run_subtask_set", fake_run_subtasks, raising=False)
    return captured, canned_plan


def test_sequential_conflict_invokes_merge_planner_subloop(tmp_path, monkeypatch):
    """Octopus fails AND sequential fails → merge-planner emits integration
    subtasks → doer subloop runs → audit passes → MERGE_NODE_READY."""
    store, mn_id, _ = _seed_merge_node(tmp_path, ["R-001", "R-002"])
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json", local_ci_command="just ci")
    captured, canned_plan = _stub_merge_planner_subloop_with_plan(monkeypatch, mn_id)
    worker = _build_worker(
        tmp_path,
        cfg,
        store,
        mn_id,
        monkeypatch,
        octopus_succeeds=False,
        sequential_succeeds=False,
        audit_outcome=None,
    )

    outcome = worker.run()

    assert outcome.final_state == State.MERGE_NODE_READY
    assert captured["parent_ids"] == sorted(["R-001", "R-002"])
    assert captured["subtasks_run"] == [s.id for s in canned_plan.topo_order()]
    rows = store.list_subtasks(mn_id)
    kinds = {r["subtask_id"]: r.get("kind") for r in rows}
    assert kinds["S-01-resolve-foo"] == "merge-integration"
    assert kinds["S-99-verify"] == "merge-integration"


def test_merge_planner_failure_blocks(tmp_path, monkeypatch):
    """Merge-planner agent returns no parseable plan → BLOCK with note."""
    store, mn_id, _ = _seed_merge_node(tmp_path, ["R-001", "R-002"])
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json", local_ci_command="just ci")

    def fake_invoke(self, parent_ids, parent_branches):
        del parent_ids, parent_branches

    monkeypatch.setattr(MergeNodeWorker, "_invoke_merge_planner", fake_invoke, raising=False)
    worker = _build_worker(
        tmp_path,
        cfg,
        store,
        mn_id,
        monkeypatch,
        octopus_succeeds=False,
        sequential_succeeds=False,
        audit_outcome=None,
    )

    outcome = worker.run()

    assert outcome.final_state == State.BLOCKED
    last_error = (store.get(mn_id) or {}).get("last_error") or ""
    assert "merge-planner" in last_error.lower()


# ----- audit gauntlet wiring -----


def test_audit_gauntlet_invoked_with_merge_node_mode(tmp_path, monkeypatch):
    """The merge-node worker calls `_run_pre_pr_pipeline(merge_node_mode=True)`."""
    store, mn_id, _ = _seed_merge_node(tmp_path, ["R-001", "R-002"])
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json", local_ci_command="just ci")
    captured: dict[str, Any] = {}

    def fake_audit(self, *, merge_node_mode):
        captured["merge_node_mode"] = merge_node_mode
        fsm_runtime.enter_pre_pr_auditing(self.store, self.node.id, note="stub")

    monkeypatch.setattr(MergeNodeWorker, "_run_pre_pr_pipeline", fake_audit, raising=False)
    git_calls: list[list[str]] = []
    dag = _empty_dag(tmp_path)
    node = _node_for_merge(mn_id, store.get_parent_task_ids(mn_id))
    worker = _StubbedWorker(
        cfg,
        dag,
        store,
        node,
        mn_id=mn_id,
        octopus_succeeds=True,
        sequential_succeeds=True,
        git_calls=git_calls,
    )
    store.set_field(mn_id, worktree_path=str(tmp_path / "wt"), container_id="fake")

    class FakeHandle:
        def __init__(self) -> None:
            self.unit_id = "fake"
            self.metadata: dict[str, Any] = {"container_id": "fake"}

    worker.handle = FakeHandle()
    monkeypatch.setattr(mnw_mod, "exec_in", lambda *a, **k: (0, "", ""))

    outcome = worker.run()

    assert outcome.final_state == State.MERGE_NODE_READY
    assert captured.get("merge_node_mode") is True


# ----- audit gauntlet `merge_node_mode` skip / re-enable -----


class _StageStub:
    def __init__(self, name: str, passed: bool = True) -> None:
        self.name = name
        self.passed = passed
        self.summary = f"{name} stub"
        self.raw_output = ""
        self.findings: list[dict] = []


def _build_pipeline_worker(tmp_path, store, mn_id, parent_ev_lists, monkeypatch):
    """Build a worker with `_execute_audit_stages` reachable; stub each
    pre_pr_audit stage to passing-stage stubs and capture which stages
    actually run."""
    parent_ids = store.get_parent_task_ids(mn_id)
    dag_nodes: dict[str, Node] = {}
    for pid, evlist in zip(parent_ids, parent_ev_lists, strict=True):
        dag_nodes[pid] = Node(
            id=pid,
            kind="behavior",
            milestone="M-1",
            title=f"parent {pid}",
            scope=f"scope of {pid}",
            depends_on=(),
            completes_behaviors=(),
            supports_behaviors=(),
            boundary_with_neighbors="",
            expected_evidence=tuple(evlist),
            playbook=(),
            rationale="",
            risks=(),
            raw={},
        )
    dag = DAG(nodes=dag_nodes, milestones={"M-1": {"id": "M-1", "title": "x"}}, raw={"nodes": []})
    node = _node_for_merge(mn_id, parent_ids)
    worker = MergeNodeWorker.__new__(MergeNodeWorker)
    worker.cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        local_ci_command="just ci",
    )
    worker.dag = dag
    worker.store = store
    worker.node = node
    worker.handle = type("FH", (), {"unit_id": "fake", "metadata": {"container_id": "fake"}})()
    worker.log_path = tmp_path / "log"
    worker.plan = None
    worker.plan_text = ""
    # Plan 33: per-task EvaluationContract is built/loaded by `_evaluation_contract`.
    # In unit tests the worker is constructed via __new__ so the cache slot
    # must be primed; the audit-stage stubs already replace `collect_standards_text`
    # so the contract isn't actually consulted here.
    worker._contract = None
    worker._last_witness_results = {}
    captured: dict[str, list[str]] = {"stages_run": []}

    def fake_local_ci(**kwargs):
        captured["stages_run"].append("local_ci")
        return _StageStub("local_ci")

    def fake_rubric(**kwargs):
        captured["stages_run"].append("rubric")
        return _StageStub("rubric")

    def fake_standards(**kwargs):
        captured["stages_run"].append("standards")
        return _StageStub("standards")

    captured_evidence: dict[str, list[dict]] = {}

    def fake_behavior(**kwargs):
        captured["stages_run"].append("behavior")
        captured_evidence["expected_evidence"] = list(kwargs.get("expected_evidence") or [])
        return _StageStub("behavior")

    monkeypatch.setattr(pre_pr_audit, "run_local_ci_gate", fake_local_ci)
    monkeypatch.setattr(pre_pr_audit, "run_rubric_audit", fake_rubric)
    monkeypatch.setattr(pre_pr_audit, "run_standards_audit", fake_standards)
    monkeypatch.setattr(pre_pr_audit, "run_behavior_audit", fake_behavior)
    monkeypatch.setattr(pre_pr_audit, "collect_standards_text", lambda cfg, **kw: "STANDARDS")
    # The audit-stage helper transitions to PRE_PR_AUDITING per stage as
    # a TUI-progress signal; the store is in PENDING in unit tests so we
    # stub the transition to a no-op.
    monkeypatch.setattr(fsm_runtime, "enter_pre_pr_auditing", lambda *a, **k: None)
    # The store also receives `update_pre_pr_audit_stage` calls. The
    # default implementation is fine, but it requires a live audit-cycle
    # row; for unit tests we no-op it.
    monkeypatch.setattr(store, "update_pre_pr_audit_stage", lambda *a, **k: None, raising=False)
    return worker, captured, captured_evidence


def test_merge_node_mode_skips_rubric_and_standards_on_trivial_cycle(tmp_path, monkeypatch):
    """No `kind=merge-integration` subtasks → rubric+standards skipped."""
    store, mn_id, _ = _seed_merge_node(tmp_path, ["R-001", "R-002"])
    worker, captured, _ = _build_pipeline_worker(tmp_path, store, mn_id, [[], []], monkeypatch)

    stages = worker._execute_audit_stages(
        cycle=1,
        diff_excerpt="diff",
        plan_text="plan",
        merge_node_mode=True,
    )

    assert captured["stages_run"] == ["local_ci", "behavior"]
    assert [s.name for s in stages] == ["local_ci", "behavior"]


def test_merge_node_mode_re_enables_rubric_when_integration_subtasks_present(tmp_path, monkeypatch):
    """`kind=merge-integration` subtasks → rubric+standards re-enabled."""
    store, mn_id, _ = _seed_merge_node(tmp_path, ["R-001", "R-002"])
    store.upsert_subtasks(
        mn_id,
        [
            {
                "subtask_id": "S-01-resolve",
                "title": "Resolve",
                "depends_on": [],
                "files_to_touch": [],
                "boundary": "",
                "acceptance": ["x"],
                "notes": "",
                "kind": "merge-integration",
            }
        ],
    )
    worker, captured, _ = _build_pipeline_worker(tmp_path, store, mn_id, [[], []], monkeypatch)

    stages = worker._execute_audit_stages(
        cycle=1,
        diff_excerpt="diff",
        plan_text="plan",
        merge_node_mode=True,
    )

    assert captured["stages_run"] == ["local_ci", "rubric", "standards", "behavior"]
    assert [s.name for s in stages] == ["local_ci", "rubric", "standards", "behavior"]


def test_spec_mode_runs_all_four_stages(tmp_path, monkeypatch):
    """`merge_node_mode=False` → standard 4-stage pipeline regardless of subtasks."""
    store, mn_id, _ = _seed_merge_node(tmp_path, ["R-001", "R-002"])
    worker, captured, _ = _build_pipeline_worker(tmp_path, store, mn_id, [[], []], monkeypatch)

    worker._execute_audit_stages(
        cycle=1,
        diff_excerpt="diff",
        plan_text="plan",
        merge_node_mode=False,
    )

    assert captured["stages_run"] == ["local_ci", "rubric", "standards", "behavior"]


def test_behavior_audit_uses_union_of_parent_expected_evidence(tmp_path, monkeypatch):
    """Behavior audit's `expected_evidence` is the union of source parents'."""
    store, mn_id, _ = _seed_merge_node(tmp_path, ["R-001", "R-002"])
    a_ev = [{"kind": "test", "behavior_id": "B-100", "description": "a"}]
    b_ev = [
        {"kind": "test", "behavior_id": "B-200", "description": "b"},
        {"kind": "test", "behavior_id": "B-100", "description": "a"},  # duplicate of a_ev[0]
    ]
    worker, _, captured_evidence = _build_pipeline_worker(tmp_path, store, mn_id, [a_ev, b_ev], monkeypatch)

    worker._execute_audit_stages(
        cycle=1,
        diff_excerpt="diff",
        plan_text="plan",
        merge_node_mode=True,
    )

    seen = captured_evidence["expected_evidence"]
    assert len(seen) == 2  # deduped
    bids = {ev["behavior_id"] for ev in seen}
    assert bids == {"B-100", "B-200"}


def test_spec_mode_uses_node_expected_evidence_only(tmp_path, monkeypatch):
    """Spec mode keeps the node's `expected_evidence` (no parent union)."""
    store, mn_id, _ = _seed_merge_node(tmp_path, ["R-001", "R-002"])
    worker, _, captured_evidence = _build_pipeline_worker(
        tmp_path,
        store,
        mn_id,
        [[{"kind": "test", "behavior_id": "B-XXX"}], [{"kind": "test", "behavior_id": "B-YYY"}]],
        monkeypatch,
    )
    # Override worker.node with one that carries its own expected_evidence.
    worker.node = Node(
        id=mn_id,
        kind="spec",
        milestone="M-1",
        title="t",
        scope="s",
        depends_on=(),
        completes_behaviors=(),
        supports_behaviors=(),
        boundary_with_neighbors="",
        expected_evidence=({"kind": "test", "behavior_id": "B-OWN"},),
        playbook=(),
        rationale="",
        risks=(),
        raw={},
    )

    worker._execute_audit_stages(
        cycle=1,
        diff_excerpt="diff",
        plan_text="plan",
        merge_node_mode=False,
    )

    seen = captured_evidence["expected_evidence"]
    assert seen == [{"kind": "test", "behavior_id": "B-OWN"}]


# ----- conflict-resolver multi-parent context -----


def test_conflict_resolver_prompt_renders_merge_node_context(tmp_path):
    """`rebase_target_kind="merge_node"` + `parent_contexts` → prompt
    includes per-parent diff blocks."""
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json", base_branch="main")
    node = Node(
        id="M-deadbeef",
        kind="merge",
        milestone="",
        title="merge-node integrating R-A,R-B",
        scope="",
        depends_on=("R-A", "R-B"),
        completes_behaviors=(),
        supports_behaviors=(),
        boundary_with_neighbors="",
        expected_evidence=(),
        playbook=(),
        rationale="",
        risks=(),
        raw={},
    )
    parent_contexts = [
        {
            "task_id": "R-A",
            "branch": "quikode/r-a-aaa",
            "log": "abc Add foo",
            "diff": "diff --git a/foo b/foo\n+method foo()\n",
        },
        {
            "task_id": "R-B",
            "branch": "quikode/r-b-bbb",
            "log": "def Rename foo to bar",
            "diff": "diff --git a/foo b/foo\n-foo()\n+bar()\n",
        },
    ]
    rendered = prompts.conflict_resolver_prompt(
        cfg,
        node,
        task_diff_excerpt="",
        main_log_excerpt="",
        main_diff_excerpt="",
        conflicted_files=[{"path": "foo", "content": "<<<<<<< HEAD\nfoo()\n=======\nbar()\n>>>>>>>"}],
        rebase_target_kind="merge_node",
        parent_contexts=parent_contexts,
    )

    assert "merge-node worker" in rendered.lower()
    assert "R-A" in rendered
    assert "R-B" in rendered
    assert "Add foo" in rendered
    assert "Rename foo to bar" in rendered
    assert "method foo()" in rendered
    assert "+bar()" in rendered
    assert "GIVE_UP" in rendered


def test_conflict_resolver_prompt_main_target_unchanged(tmp_path):
    """`rebase_target_kind="main"` (default) renders the legacy framing,
    no parent_contexts surfaced."""
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json", base_branch="main")
    node = Node(
        id="R-A",
        kind="behavior",
        milestone="",
        title="t",
        scope="s",
        depends_on=(),
        completes_behaviors=(),
        supports_behaviors=(),
        boundary_with_neighbors="",
        expected_evidence=(),
        playbook=(),
        rationale="",
        risks=(),
        raw={},
    )
    rendered = prompts.conflict_resolver_prompt(
        cfg,
        node,
        task_diff_excerpt="diff x",
        main_log_excerpt="abc",
        main_diff_excerpt="diff main",
        conflicted_files=[],
    )

    assert "merge-node worker" not in rendered.lower()
    assert "main" in rendered.lower()


# ----- merge-planner prompt -----


def test_merge_planner_prompt_renders_with_parent_diffs(tmp_path):
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json", base_branch="main")
    parent_contexts = [
        {
            "task_id": "R-A",
            "branch": "quikode/r-a",
            "title": "Add foo",
            "summary": "introduces foo()",
            "diff_excerpt": "diff --git a/foo b/foo\n+def foo(): ...",
        },
        {
            "task_id": "R-B",
            "branch": "quikode/r-b",
            "title": "Rename foo",
            "summary": "renames foo to bar",
            "diff_excerpt": "diff --git a/foo b/foo\n-foo\n+bar",
        },
    ]
    # Plan 33: merge_planner_prompt now takes a Node and EvaluationContract.
    merge_node_obj = Node(
        id="M-abcdef12",
        kind="merge",
        milestone="",
        title="merge-node",
        scope="",
        depends_on=("R-A", "R-B"),
        completes_behaviors=(),
        supports_behaviors=(),
        boundary_with_neighbors="",
        expected_evidence=(),
        playbook=(),
        rationale="",
        risks=(),
        raw={},
    )
    contract = build_for(merge_node_obj, cfg)
    rendered = prompts.merge_planner_prompt(cfg, merge_node_obj, parent_contexts, contract)

    assert "M-abcdef12" in rendered
    assert "R-A" in rendered and "R-B" in rendered
    assert "Add foo" in rendered and "Rename foo" in rendered
    assert "introduces foo" in rendered
    assert "renames foo to bar" in rendered
    assert "main" in rendered  # base_branch reference
