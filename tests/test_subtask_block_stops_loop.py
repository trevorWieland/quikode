"""Regression: a BLOCKED subtask must immediately fail the whole task.

Critical contract bug discovered during R-0001's 2026-05-02 run: the
v2 _subtask_loop continued past a BLOCKED subtask and tried to run later
subtasks (which always depend on earlier ones for compile/correctness
reasons). Result: misleadingly-further-along tasks that couldn't actually
pass + wasted token budget on doomed downstream work.

Fix: subtask BLOCKED → return WorkerOutcome(BLOCKED) immediately, mark
remaining subtasks as SKIPPED so the user can see which slices never
ran. The user resumes after fixing the cause via `quikode resume <id>`.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from quikode.config import Config
from quikode.dag import DAG
from quikode.state import State, Store, SubtaskState
from quikode.subtask_schema import Plan, Subtask
from quikode.types import Verdict
from quikode.worker import (
    TaskWorker,
    _CheckerOutcome,
    _SubtaskPassOutcome,
)


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


def _build_plan(subtask_ids: list[str]) -> Plan:
    return Plan(
        node_id="R-001",
        summary="test plan",
        subtasks=tuple(
            Subtask(
                id=sid,
                title=sid,
                depends_on=(),
                files_to_touch=(f"{sid}.rs",),
                boundary="",
                acceptance=("compiles",),
                notes="",
            )
            for sid in subtask_ids
        ),
        final_acceptance=("just ci passes",),
    )


def _build_worker(tmp_path: Path, plan: Plan) -> TaskWorker:
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        prompts_dir=tmp_path / "missing-prompts",  # bundled prompts fallback
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
        # v3 Phase A: block fast — small hard ceiling, no progress checks
        # under the cap so tests don't have to stub the agent.
        subtask_hard_max_attempts=2,
        subtask_progress_check_after=10,
        subtask_progress_check_every=10,
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    dag = _build_dag(tmp_path)
    store = Store(cfg.state_dir / "quikode.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.PLANNING)
    store.upsert_subtasks(
        "R-001",
        [
            {
                "subtask_id": s.id,
                "title": s.title,
                "depends_on": list(s.depends_on),
                "files_to_touch": list(s.files_to_touch),
                "boundary": s.boundary,
                "acceptance": list(s.acceptance),
                "notes": s.notes,
            }
            for s in plan.subtasks
        ],
    )
    worker = TaskWorker(cfg, dag, store, dag.nodes["R-001"])
    worker.plan = plan
    worker.handle = MagicMock()
    worker.handle.container_name = "qk-stub"
    return worker


def test_block_on_first_subtask_skips_remaining_and_returns_blocked(tmp_path):
    """The whole loop must short-circuit on first BLOCKED subtask."""
    plan = _build_plan(["S-01", "S-02", "S-03"])
    worker = _build_worker(tmp_path, plan)

    do_calls: list[str] = []
    check_calls: list[str] = []

    def fake_do(subtask, attempt, triage_notes):
        do_calls.append(subtask.id)
        worker.store.update_subtask("R-001", subtask.id, state=SubtaskState.DOING.value)

    def fake_check(subtask):
        check_calls.append(subtask.id)
        return _CheckerOutcome(
            verdict=Verdict.FAIL,
            checker_text="VERDICT: FAIL\nROOT_CAUSE: nope",
            transient=False,
            rc=0,
            stderr="",
        )

    def fake_triage(subtask, attempt, budget, checker_text):
        return "fix it"

    with (
        patch.object(worker, "_do_subtask", side_effect=fake_do),
        patch.object(worker, "_check_subtask", side_effect=fake_check),
        patch.object(worker, "_triage_subtask", side_effect=fake_triage),
    ):
        outcome = worker._subtask_loop()

    # 1. Loop returned BLOCKED, didn't fall through to None (final_check).
    assert outcome is not None
    assert outcome.final_state is State.BLOCKED
    assert "S-01" in outcome.note

    # 2. Only S-01 was attempted (the doer ran budget=2 times for S-01 only).
    assert do_calls == ["S-01", "S-01"]
    assert check_calls == ["S-01", "S-01"]

    # 3. S-01 is in BLOCKED state.
    s1 = worker.store.get_subtask("R-001", "S-01")
    assert s1["state"] == SubtaskState.BLOCKED.value

    # 4. S-02 and S-03 are SKIPPED (visible in store).
    s2 = worker.store.get_subtask("R-001", "S-02")
    s3 = worker.store.get_subtask("R-001", "S-03")
    assert s2["state"] == SubtaskState.SKIPPED.value
    assert s3["state"] == SubtaskState.SKIPPED.value

    # 5. Task itself transitioned to BLOCKED.
    task = worker.store.get("R-001")
    assert task["state"] == State.BLOCKED.value

    worker.store.conn.close()


def test_block_on_middle_subtask_skips_only_later(tmp_path):
    """When S-02 of [S-01, S-02, S-03] blocks, S-01 stays DONE, S-03 is SKIPPED."""
    plan = _build_plan(["S-01", "S-02", "S-03"])
    worker = _build_worker(tmp_path, plan)

    def fake_do(subtask, attempt, triage_notes):
        worker.store.update_subtask("R-001", subtask.id, state=SubtaskState.DOING.value)

    def fake_check(subtask):
        # S-01 passes, S-02 always fails
        if subtask.id == "S-01":
            return _CheckerOutcome(
                verdict=Verdict.PASS, checker_text="VERDICT: PASS", transient=False, rc=0, stderr=""
            )
        return _CheckerOutcome(
            verdict=Verdict.FAIL, checker_text="VERDICT: FAIL", transient=False, rc=0, stderr=""
        )

    def fake_pass(subtask):
        # Stub the v3 commit gate: PASS branch always settles cleanly.
        worker.store.update_subtask("R-001", subtask.id, state=SubtaskState.DONE.value)
        return _SubtaskPassOutcome(kind="settled")

    with (
        patch.object(worker, "_do_subtask", side_effect=fake_do),
        patch.object(worker, "_check_subtask", side_effect=fake_check),
        patch.object(worker, "_triage_subtask", return_value="fix it"),
        patch.object(worker, "_handle_subtask_pass", side_effect=fake_pass),
    ):
        outcome = worker._subtask_loop()

    assert outcome is not None
    assert outcome.final_state is State.BLOCKED
    assert "S-02" in outcome.note

    s1 = worker.store.get_subtask("R-001", "S-01")
    s2 = worker.store.get_subtask("R-001", "S-02")
    s3 = worker.store.get_subtask("R-001", "S-03")
    assert s1["state"] == SubtaskState.DONE.value
    assert s2["state"] == SubtaskState.BLOCKED.value
    assert s3["state"] == SubtaskState.SKIPPED.value
    worker.store.conn.close()


def test_all_pass_returns_none_so_final_check_runs(tmp_path):
    """Sanity: when every subtask PASSes, loop returns None and run() falls
    through to final_check."""
    plan = _build_plan(["S-01", "S-02"])
    worker = _build_worker(tmp_path, plan)

    def fake_do(subtask, attempt, triage_notes):
        worker.store.update_subtask("R-001", subtask.id, state=SubtaskState.DOING.value)

    def fake_pass(subtask):
        worker.store.update_subtask("R-001", subtask.id, state=SubtaskState.DONE.value)
        return _SubtaskPassOutcome(kind="settled")

    with (
        patch.object(worker, "_do_subtask", side_effect=fake_do),
        patch.object(
            worker,
            "_check_subtask",
            return_value=_CheckerOutcome(
                verdict=Verdict.PASS, checker_text="VERDICT: PASS", transient=False, rc=0, stderr=""
            ),
        ),
        patch.object(worker, "_handle_subtask_pass", side_effect=fake_pass),
    ):
        outcome = worker._subtask_loop()

    assert outcome is None  # fall through to final_check
    assert worker.store.get_subtask("R-001", "S-01")["state"] == SubtaskState.DONE.value
    assert worker.store.get_subtask("R-001", "S-02")["state"] == SubtaskState.DONE.value
    worker.store.conn.close()


def test_transient_checker_failures_capped_not_treated_as_real_attempts(tmp_path, monkeypatch):
    """Regression for the 2026-05-03 R-0002 runaway: when the docker container
    is gone (e.g. another orchestrator's cleanup_all_quikode killed it), the
    checker fast-fails with rc=1, no VERDICT in stdout, transient=True. Without
    a transient cap in the subtask loop, the worker treated each as a real
    FAIL and looped 50 times in seconds, burning the hard ceiling. The fix
    free-retries (without bumping attempt) and BLOCKs once the consecutive
    transient cap (`subtask_transient_max_retries`) is exceeded."""
    plan = _build_plan(["S-01"])
    worker = _build_worker(tmp_path, plan)
    # Tighten config so the test runs fast.
    worker.cfg.subtask_transient_max_retries = 2
    worker.cfg.subtask_hard_max_attempts = 50  # MUST not be exhausted; the cap
    # below should fire first.

    do_calls: list[str] = []
    check_calls: list[str] = []

    def fake_do(subtask, attempt, triage_notes):
        do_calls.append(subtask.id)

    def fake_check(subtask):
        check_calls.append(subtask.id)
        # Always return transient — simulate vanished container.
        return _CheckerOutcome(verdict=Verdict.FAIL, checker_text="", transient=True, rc=0, stderr="")

    triage_was_called = False

    def fake_triage(*a, **kw):
        nonlocal triage_was_called
        triage_was_called = True
        return "no"

    # Speed up time.sleep so the loop's 15s backoff per transient doesn't
    # actually delay the test. (worker.py imports `time` at module level.)
    monkeypatch.setattr("quikode.worker.time.sleep", lambda _s: None)

    with (
        patch.object(worker, "_do_subtask", side_effect=fake_do),
        patch.object(worker, "_check_subtask", side_effect=fake_check),
        patch.object(worker, "_triage_subtask", side_effect=fake_triage),
    ):
        outcome = worker._subtask_loop()

    assert outcome is not None
    assert outcome.final_state is State.BLOCKED
    # Transient cap message in the BLOCK reason.
    assert "transient" in outcome.note.lower()
    # Triage must NOT have been called — transient checker failures don't
    # warrant a triage round (the input is empty and would just confuse the
    # triage agent).
    assert not triage_was_called
    # Doer was called exactly transient_max + 1 times (free retries until cap).
    assert len(do_calls) == 3  # 2 retries + 1 that exceeds the cap
    worker.store.conn.close()


def test_pre_existing_skipped_subtask_returns_blocked(tmp_path):
    """If a resume comes in with a SKIPPED subtask in its plan, that's a
    sign of a prior partial run we can't safely continue past — return
    BLOCKED rather than re-attempting (which might cascade further damage)."""
    plan = _build_plan(["S-01", "S-02"])
    worker = _build_worker(tmp_path, plan)
    worker.store.update_subtask("R-001", "S-01", state=SubtaskState.SKIPPED.value)

    outcome = worker._subtask_loop()
    assert outcome is not None
    assert outcome.final_state is State.BLOCKED
    assert "SKIPPED" in outcome.note
    worker.store.conn.close()
