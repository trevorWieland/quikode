"""Plan 38 PR-B.7: intent reviewer runs on the JsonAgent layer.

Replaces the prior `_parse_intent_verdict` regex with the structured
`IntentReviewVerdict` schema. Closed-enum `verdict` field —
`no_drift` | `minor_drift` | `intent_conflict` — drives the worker
branch instead of free-text `VERDICT:` lines.

These tests stub `make_agent("intent_reviewer", cfg)` and verify each
verdict routes to the right branch:
- `no_drift`   → `enter_pending_ci`, return None
- `minor_drift` → calls `_rebase_or_resolve`
- `intent_conflict` → calls `_replan_and_resume` (when budget
  remaining) or BLOCKs (when exhausted)
- transport failure / parse_errors → safe default to `no_drift`
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from quikode.agent_schemas import IntentReviewVerdict
from quikode.config import Config
from quikode.dag import DAG
from quikode.state import State, Store
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
                "completes_behaviors": [],
                "supports_behaviors": [],
                "boundary_with_neighbors": "",
                "expected_evidence": [],
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
        intent_max_replans=2,
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    dag = _build_dag(tmp_path)
    store = Store(cfg.state_dir / "quikode.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.PENDING_CI)
    store.set_field(
        "R-001",
        branch="quikode/r-001-abc",
        base_ref_sha="aaa",
        last_synced_main_sha="aaa",
    )
    worker = TaskWorker(cfg, dag, store, dag.nodes["R-001"])
    worker.handle = MagicMock()
    worker.handle.container_name = "qk-stub"
    return worker


_VERDICT_VALUES: dict[str, IntentReviewVerdict] = {
    "no_drift": IntentReviewVerdict(
        verdict="no_drift",
        affected_areas=["src/lib.rs"],
        explanation="something happened on main",
        next_actions=[],
    ),
    "minor_drift": IntentReviewVerdict(
        verdict="minor_drift",
        affected_areas=["src/lib.rs"],
        explanation="something happened on main",
        next_actions=[],
    ),
    "intent_conflict": IntentReviewVerdict(
        verdict="intent_conflict",
        affected_areas=["src/lib.rs"],
        explanation="something happened on main",
        next_actions=[],
    ),
}


def _stub_agent(verdict_value: str, *, rc: int = 0, parse_errors: tuple[str, ...] = ()) -> _StubAgent:
    env: IntentReviewVerdict | None = _VERDICT_VALUES.get(verdict_value)
    return _StubAgent(_StubAgentResult(structured=env, rc=rc, parse_errors=parse_errors))


# ---------- closed-enum branching ----------


def test_intent_reviewer_no_drift_returns_none(tmp_path) -> None:
    worker = _build_worker(tmp_path)
    stub = _stub_agent("no_drift")
    with patch("quikode.workers.pr_lifecycle.make_agent", return_value=stub):
        verdict = worker._run_intent_reviewer(
            base="aaa",
            current_main="bbb",
            task_diff="diff",
            main_log="log",
            main_diff="diff",
        )
    assert verdict is not None
    assert verdict.verdict == "no_drift"
    outcome = worker._handle_intent_review_outcome(verdict)
    assert outcome is None  # no-op continuation


def test_intent_reviewer_minor_drift_triggers_rebase(tmp_path) -> None:
    worker = _build_worker(tmp_path)
    stub = _stub_agent("minor_drift")
    sentinel = object()
    with (
        patch("quikode.workers.pr_lifecycle.make_agent", return_value=stub),
        patch.object(worker, "_rebase_or_resolve", return_value=sentinel),
    ):
        verdict = worker._run_intent_reviewer(
            base="aaa",
            current_main="bbb",
            task_diff="diff",
            main_log="log",
            main_diff="diff",
        )
        outcome = worker._handle_intent_review_outcome(verdict)
    assert outcome is sentinel


def test_intent_reviewer_intent_conflict_replans(tmp_path) -> None:
    worker = _build_worker(tmp_path)
    stub = _stub_agent("intent_conflict")
    sentinel = object()
    with (
        patch("quikode.workers.pr_lifecycle.make_agent", return_value=stub),
        patch.object(worker, "_replan_and_resume", return_value=sentinel) as mock_replan,
    ):
        verdict = worker._run_intent_reviewer(
            base="aaa",
            current_main="bbb",
            task_diff="diff",
            main_log="log",
            main_diff="diff",
        )
        outcome = worker._handle_intent_review_outcome(verdict)
    assert outcome is sentinel
    mock_replan.assert_called_once()
    # affected_areas was list[str] in the schema; it's joined when handed
    # to `_replan_and_resume` (positional arg #2).
    call_args = mock_replan.call_args
    assert "src/lib.rs" in call_args.args[1]


def test_intent_reviewer_intent_conflict_blocks_when_replan_budget_exhausted(tmp_path) -> None:
    worker = _build_worker(tmp_path)
    # Push replan_count above the budget
    worker.store.set_field("R-001", replan_count=worker.cfg.intent_max_replans)
    # Move to ADDRESSING_FEEDBACK so the BLOCK_TASK transition is valid
    # (the prior call site lived in an active state when this branch fired).
    worker.store.transition("R-001", State.AUDIT_LOCAL_CI)
    stub = _stub_agent("intent_conflict")
    with patch("quikode.workers.pr_lifecycle.make_agent", return_value=stub):
        verdict = worker._run_intent_reviewer(
            base="aaa",
            current_main="bbb",
            task_diff="diff",
            main_log="log",
            main_diff="diff",
        )
        outcome = worker._handle_intent_review_outcome(verdict)
    assert outcome is not None
    assert outcome.final_state == State.BLOCKED


def test_intent_reviewer_parse_failure_defaults_to_no_drift(tmp_path) -> None:
    """Schema validation failure → safe-no-op `no_drift` synthesis. The
    reviewer is advisory; a parse error must not BLOCK the task."""
    worker = _build_worker(tmp_path)
    stub = _stub_agent("", parse_errors=("verdict: invalid value 'maybe'",))
    with patch("quikode.workers.pr_lifecycle.make_agent", return_value=stub):
        verdict = worker._run_intent_reviewer(
            base="aaa",
            current_main="bbb",
            task_diff="diff",
            main_log="log",
            main_diff="diff",
        )
    assert verdict is not None
    assert verdict.verdict == "no_drift"
    assert "intent reviewer call failed" in verdict.explanation


def test_intent_reviewer_transport_failure_defaults_to_no_drift(tmp_path) -> None:
    worker = _build_worker(tmp_path)
    stub = _stub_agent("", rc=124)
    with patch("quikode.workers.pr_lifecycle.make_agent", return_value=stub):
        verdict = worker._run_intent_reviewer(
            base="aaa",
            current_main="bbb",
            task_diff="diff",
            main_log="log",
            main_diff="diff",
        )
    assert verdict is not None
    assert verdict.verdict == "no_drift"


def test_intent_review_record_uses_structured_verdict(tmp_path) -> None:
    """Plan 38 PR-B.7: the `verdict` field persisted to `intent_reviews`
    is the closed-enum string (not the prior uppercase form). This test
    locks in the wire format so older briefing readers don't blow up."""
    worker = _build_worker(tmp_path)
    stub = _stub_agent("intent_conflict")
    with patch("quikode.workers.pr_lifecycle.make_agent", return_value=stub):
        worker._run_intent_reviewer(
            base="aaa",
            current_main="bbb",
            task_diff="diff",
            main_log="log",
            main_diff="diff",
        )
    with worker.store.tx() as c:
        rows = list(
            c.execute(
                "SELECT verdict, affected_areas FROM intent_reviews WHERE task_id = ?",
                ("R-001",),
            )
        )
    assert rows
    assert rows[0][0] == "intent_conflict"
    assert "src/lib.rs" in rows[0][1]
