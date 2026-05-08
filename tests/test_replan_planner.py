"""Plan 38 PR-B.7: post-PR replan planner runs on the JsonAgent layer.

The replan planner emits a `PlannerOutput` (same wire schema as the
spec planner). The worker's `_replan_and_resume` translates wire →
runtime via `_wire_to_runtime_plan`, persists subtasks via
`store.upsert_subtasks`, and resumes the subtask loop.

These tests stub `make_agent("replan_planner", cfg)` and verify:
1. A valid `PlannerOutput` is translated and persisted; subtask
   ids carry over from prior DONE subtasks (preserves work).
2. A `parse_errors`-populated result BLOCKs.
3. A transport failure (`rc != 0`) BLOCKs.
4. The `replan_planner` role exists in `ROLES` and can be constructed
   with `make_agent`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from quikode.agent_registry import ROLES, make_agent
from quikode.agent_schemas import (
    PlannerOutput,
    RubricTargetSchema,
    StandardsRefSchema,
    SubtaskSpec,
)
from quikode.agents.json_protocol import JsonOutputAgent
from quikode.config import Config
from quikode.dag import DAG
from quikode.state import State, Store, SubtaskState
from quikode.worker import TaskWorker


@dataclass
class _StubAgentResult:
    structured: Any = None
    rc: int = 0
    transient: bool = False
    duration_s: float = 1.0
    tokens_input: int | None = None
    tokens_output: int | None = None
    cost_usd: float | None = None
    parse_errors: tuple[str, ...] = ()
    raw_text: str | None = None
    stderr_excerpt: str = ""


class _StubAgent:
    def __init__(self, result: _StubAgentResult):
        self.result = result
        self.last_prompt: str | None = None

    def invoke(self, prompt: str, **kwargs: Any) -> _StubAgentResult:
        self.last_prompt = prompt
        return self.result


# ---------- registry-level guards ----------


def test_replan_planner_role_registered() -> None:
    assert "replan_planner" in ROLES
    spec = ROLES["replan_planner"]
    assert spec.output_schema is PlannerOutput
    assert spec.writes_files is False
    assert spec.timeout_s_field == "replan_planner_timeout_s"


def test_make_agent_replan_planner_returns_json_output_agent() -> None:
    cfg = Config(repo_path=Path("/tmp/repo"), dag_path=Path("/tmp/dag"))
    agent = make_agent("replan_planner", cfg)
    assert isinstance(agent, JsonOutputAgent)
    assert agent.output_schema is PlannerOutput


# ---------- worker-level translation + persist ----------


def _build_dag(tmp_path: Path) -> DAG:
    raw = {
        "schema": "test",
        "milestones": [{"id": "M-1", "title": "x", "goal": "x", "status": "planned"}],
        "nodes": [
            {
                "id": "R-001",
                "kind": "behavior",
                "milestone": "M-1",
                "title": "test node",
                "scope": "x",
                "depends_on": [],
                "completes_behaviors": ["B-100"],
                "supports_behaviors": [],
                "boundary_with_neighbors": "",
                "expected_evidence": [
                    {
                        "behavior_id": "B-100",
                        "kind": "test",
                        "interfaces": ["api"],
                        "witnesses": ["positive"],
                        "command": "just test",
                        "description": "x",
                    }
                ],
                "playbook": [],
                "rationale": "",
                "risks": [],
            }
        ],
    }
    p = tmp_path / "dag.json"
    p.write_text(json.dumps(raw))
    return DAG.load(p)


def _build_worker(tmp_path: Path) -> TaskWorker:
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        prompts_dir=tmp_path / "missing-prompts",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
        local_ci_command="just ci",
        intent_max_replans=2,
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    dag = _build_dag(tmp_path)
    store = Store(cfg.state_dir / "quikode.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.PENDING_CI)
    store.set_field("R-001", branch="quikode/r-001-abc", base_ref_sha="aaa")
    # Pre-existing subtasks: S-01 already DONE
    store.upsert_subtasks(
        "R-001",
        [
            {
                "subtask_id": "S-01",
                "title": "old subtask 1",
                "depends_on": [],
                "files_to_touch": ["foo.rs"],
                "boundary": "",
                "acceptance": ["a"],
                "notes": "",
            },
            {
                "subtask_id": "S-02",
                "title": "old subtask 2",
                "depends_on": ["S-01"],
                "files_to_touch": ["bar.rs"],
                "boundary": "",
                "acceptance": ["b"],
                "notes": "",
            },
        ],
    )
    store.update_subtask("R-001", "S-01", state=SubtaskState.DONE.value)
    worker = TaskWorker(cfg, dag, store, dag.nodes["R-001"])
    worker.handle = MagicMock()
    worker.handle.container_name = "qk-stub"
    return worker


def _make_planner_output(node_id: str = "R-001") -> PlannerOutput:
    """Build a minimal valid `PlannerOutput` with one subtask carrying
    a rubric target, a standards ref, and the behavior evidence id from
    the test DAG node above so the runtime validators pass."""
    return PlannerOutput(
        node_id=node_id,
        summary="replanned plan",
        gauntlet_strategy=(
            "S-01 grounds the rubric Security category at apps/foo.rs:42; "
            "S-02 covers Performance under bar.rs:99 with a regression bench."
        ),
        subtasks=[
            SubtaskSpec(
                id="S-01",
                title="replanned subtask 1",
                depends_on=[],
                files_to_touch=["foo.rs"],
                boundary="",
                acceptance=["compiles cleanly"],
                rubric_targets=[RubricTargetSchema(category="security", predicted_score=8)],
                standards_referenced=[StandardsRefSchema(doc_path="docs/sec.md", section="X")],
                architecture_referenced=[],
                behavior_evidence_advanced=["B-100"],
            ),
            SubtaskSpec(
                id="S-NEW",
                title="newly added subtask",
                depends_on=["S-01"],
                files_to_touch=["new.rs"],
                boundary="",
                acceptance=["new behavior verified"],
                rubric_targets=[RubricTargetSchema(category="performance", predicted_score=7)],
                standards_referenced=[StandardsRefSchema(doc_path="docs/perf.md", section="Y")],
                architecture_referenced=[],
                behavior_evidence_advanced=[],
            ),
        ],
        final_acceptance=["just ci passes", "all rubric categories at >= 8"],
    )


def test_replan_happy_path_translates_and_persists(tmp_path) -> None:
    worker = _build_worker(tmp_path)
    plan_out = _make_planner_output()
    stub = _StubAgent(_StubAgentResult(structured=plan_out, rc=0))
    with (
        patch("quikode.workers.pr_lifecycle.make_agent", return_value=stub),
        patch.object(worker, "_subtask_loop", return_value=None),
    ):
        outcome = worker._replan_and_resume(
            prior_explanation="main shifted under us",
            affected="src/foo.rs",
        )
    # _subtask_loop returned None → fell through to PENDING_CI re-entry
    assert outcome is None
    rows = worker.store.list_subtasks("R-001")
    ids = {r["subtask_id"] for r in rows}
    assert "S-01" in ids
    assert "S-NEW" in ids
    # S-01 carried over its DONE state from prior plan
    s01 = next(r for r in rows if r["subtask_id"] == "S-01")
    assert s01["state"] == SubtaskState.DONE.value
    # Plan field stored
    assert worker.plan is not None
    assert "S-NEW" in {s.id for s in worker.plan.subtasks}


def _last_state_log_note(worker: TaskWorker) -> str:
    with worker.store.tx() as c:
        rows = list(
            c.execute(
                "SELECT note FROM state_log WHERE task_id = ? ORDER BY ts DESC LIMIT 1",
                ("R-001",),
            )
        )
    return rows[0][0] if rows else ""


def test_replan_parse_errors_block(tmp_path) -> None:
    worker = _build_worker(tmp_path)
    stub = _StubAgent(
        _StubAgentResult(
            structured=None,
            rc=0,
            parse_errors=("subtasks: at least 1 required",),
        )
    )
    with patch("quikode.workers.pr_lifecycle.make_agent", return_value=stub):
        outcome = worker._replan_and_resume(
            prior_explanation="x",
            affected="y",
        )
    assert outcome is not None
    assert outcome.final_state == State.BLOCKED
    note = _last_state_log_note(worker)
    assert "at least 1 required" in note


def test_replan_transport_failure_blocks(tmp_path) -> None:
    worker = _build_worker(tmp_path)
    stub = _StubAgent(_StubAgentResult(structured=None, rc=124, transient=True))
    with patch("quikode.workers.pr_lifecycle.make_agent", return_value=stub):
        outcome = worker._replan_and_resume(
            prior_explanation="x",
            affected="y",
        )
    assert outcome is not None
    assert outcome.final_state == State.BLOCKED
    note = _last_state_log_note(worker)
    assert "rc=124" in note
