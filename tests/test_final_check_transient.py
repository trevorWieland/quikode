"""Regression for cleanup-1: transient checker failures don't burn the
final-check retry budget.

Live observed during E2E: when codex/auth/container fast-fails the checker
agent, three "attempts" can complete in 2 seconds and the task BLOCKs
immediately on infrastructure noise. The fix wires `_check()` to surface a
`transient` flag (set when AgentResult.transient is True OR when the
checker agent returned rc!=0 in <5s with no parseable VERDICT) and
`_final_check_loop` skips burning attempt-budget on transients.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from quikode.config import Config
from quikode.dag import DAG
from quikode.state import State, Store
from quikode.subtask_schema import Plan, Subtask
from quikode.types import Verdict
from quikode.worker import TaskWorker


def _build_dag(tmp_path: Path) -> DAG:
    raw = {
        "schema": "test",
        "milestones": [{"id": "M-1", "title": "x", "goal": "x", "status": "planned"}],
        "nodes": [
            {
                "id": "R-001",
                "kind": "behavior",
                "milestone": "M-1",
                "title": "x",
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
        # v3 fixup decomposition: budget for whole-spec retries is now
        # `fixup_max_rounds` (each round = a fixup planner call + its
        # decomposed subtasks). Set tight for fast tests.
        fixup_max_rounds=2,
        subtask_transient_max_retries=3,
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    dag = _build_dag(tmp_path)
    store = Store(cfg.state_dir / "quikode.db")
    store.upsert_pending("R-001")
    worker = TaskWorker(cfg, dag, store, dag.nodes["R-001"])
    worker.handle = MagicMock(container_name="qk-stub")
    worker.plan = Plan(
        node_id="R-001",
        summary="x",
        subtasks=(
            Subtask(
                id="S-1",
                title="x",
                depends_on=(),
                files_to_touch=("x.rs",),
                boundary="",
                acceptance=("compiles",),
                notes="",
            ),
        ),
        final_acceptance=("just ci",),
    )
    worker.plan_text = "stub plan"
    return worker


def test_transient_check_does_not_burn_budget(tmp_path):
    """When _check returns transient=True, the loop retries without bumping
    the fixup_round counter. After fixup_max_rounds real failures the loop
    exits with a BLOCKED outcome citing exhausted rounds."""
    worker = _build_worker(tmp_path)

    # Sequence: 2 transients → 1 real fail → 1 real fail → 1 real fail → exhaust.
    seq = [
        (Verdict.FAIL, "pass", None, "transient noise", True),
        (Verdict.FAIL, "pass", None, "transient noise", True),
        (Verdict.FAIL, "pass", None, "VERDICT: FAIL\nROOT_CAUSE: real", False),
        (Verdict.FAIL, "pass", None, "VERDICT: FAIL\nROOT_CAUSE: real", False),
        (Verdict.FAIL, "pass", None, "VERDICT: FAIL\nROOT_CAUSE: real", False),
    ]
    call_count = {"n": 0}

    def fake_check():
        i = call_count["n"]
        call_count["n"] += 1
        return seq[min(i, len(seq) - 1)]

    with (
        patch.object(worker, "_check", side_effect=fake_check),
        # The fixup planner returns None → caller falls back to _do (also patched).
        patch.object(worker, "_invoke_fixup_planner", return_value=None),
        patch.object(worker, "_do"),
        patch("quikode.worker.time.sleep"),  # don't actually sleep on transient backoff
    ):
        outcome = worker._final_check_loop()

    assert outcome is not None
    assert outcome.final_state is State.BLOCKED
    assert "exhausted fixup rounds" in outcome.note
    # _check was called: 2 transients (free) + fixup_max_rounds=2 real attempts
    # + 1 final attempt that triggers the round-cap break = 5 calls.
    assert call_count["n"] == 5


def test_transient_cap_blocks_after_too_many_in_a_row(tmp_path):
    """If transients keep firing past the cap, block instead of looping forever."""
    worker = _build_worker(tmp_path)
    # transient_max_retries=3, so the 4th transient should block.
    transient_response = (Verdict.FAIL, "pass", None, "noise", True)

    with (
        patch.object(worker, "_check", return_value=transient_response),
        patch.object(worker, "_triage"),
        patch.object(worker, "_do"),
        patch("quikode.worker.time.sleep"),
    ):
        outcome = worker._final_check_loop()

    assert outcome is not None
    assert outcome.final_state is State.BLOCKED
    assert "transient" in outcome.note.lower()


def test_transient_counter_resets_on_real_attempt(tmp_path):
    """After a real attempt, the transient counter resets — so a transient
    after several real failures still gets retried freely."""
    worker = _build_worker(tmp_path)
    # transient_max_retries=3, fixup_max_rounds=2. Sequence: T, T, real-fail,
    # T, T, T, real-fail, real-fail (round-cap exit). The middle real-fail
    # resets the transient counter; we should block at "exhausted fixup
    # rounds", not transient cap.
    seq = [
        (Verdict.FAIL, "pass", None, "noise", True),
        (Verdict.FAIL, "pass", None, "noise", True),
        (Verdict.FAIL, "pass", None, "VERDICT: FAIL", False),
        (Verdict.FAIL, "pass", None, "noise", True),
        (Verdict.FAIL, "pass", None, "noise", True),
        (Verdict.FAIL, "pass", None, "VERDICT: FAIL", False),
        (Verdict.FAIL, "pass", None, "VERDICT: FAIL", False),
    ]
    i = {"n": 0}

    def fake_check():
        idx = i["n"]
        i["n"] += 1
        return seq[min(idx, len(seq) - 1)]

    with (
        patch.object(worker, "_check", side_effect=fake_check),
        patch.object(worker, "_invoke_fixup_planner", return_value=None),
        patch.object(worker, "_do"),
        patch("quikode.worker.time.sleep"),
    ):
        outcome = worker._final_check_loop()

    assert outcome is not None
    assert outcome.final_state is State.BLOCKED
    assert "exhausted fixup rounds" in outcome.note


def test_resume_resets_budget_counters(tmp_path):
    """`quikode resume` must zero out do_check_retries/ci_triage_retries/
    review_triage_retries so the next final-check pass starts fresh."""
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        prompts_dir=tmp_path / "missing-prompts",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.state_dir / "quikode.db")
    store.upsert_pending("R-001")
    # Simulate a prior run that bumped retries.
    store.increment("R-001", "do_check_retries")
    store.increment("R-001", "do_check_retries")
    store.increment("R-001", "ci_triage_retries")
    store.increment("R-001", "review_triage_retries")
    row = store.get("R-001")
    assert row["do_check_retries"] == 2
    assert row["ci_triage_retries"] == 1
    assert row["review_triage_retries"] == 1

    # transition mirrors what cli.py:resume() does.
    store.transition(
        "R-001",
        State.PENDING,
        note="resume",
        do_check_retries=0,
        ci_triage_retries=0,
        review_triage_retries=0,
    )
    row = store.get("R-001")
    assert row["do_check_retries"] == 0
    assert row["ci_triage_retries"] == 0
    assert row["review_triage_retries"] == 0
