"""v3 Phase A: per-subtask commit + push gate.

After the checker emits Verdict.PASS, the worker runs `commit_subtask`
(in worktree.py) which `git add`-s the planner-declared files,
commits, and pushes. We verify three paths from the worker side:

1. clean PASS: subtask DONE, commit_sha set on the row.
2. transient push failure (network blip): transient_retries++,
   real `retries` does NOT bump, subtask stays in TRIAGING/DOING and the
   loop iterates again.
3. real failure (non-network): synthesized as a checker FAIL → triage
   runs, `retries`++.

`commit_subtask` itself is unit-tested separately (its `exec_in` calls
are stubbed). Here we operate on `_handle_subtask_pass` and the loop.
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
)
from quikode.worktree import CommitResult


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


def _build_worker(tmp_path: Path, plan: Plan, *, pre_commit_runner: str = "none") -> TaskWorker:
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        prompts_dir=tmp_path / "missing-prompts",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
        # v3 Phase A: small hard ceiling so tests block fast; progress
        # check disabled (after > hard_max) so tests don't need to stub it.
        subtask_hard_max_attempts=2,
        subtask_progress_check_after=10,
        subtask_progress_check_every=10,
        pre_commit_runner=pre_commit_runner,  # type: ignore[arg-type]
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    dag = _build_dag(tmp_path)
    store = Store(cfg.state_dir / "quikode.db")
    store.upsert_pending("R-001")
    store.set_field("R-001", branch="quikode/r-001-abc123")
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


def test_pass_with_clean_commit_marks_done_and_records_sha(tmp_path):
    plan = _build_plan(["S-01"])
    worker = _build_worker(tmp_path, plan)

    def fake_commit_subtask(
        handle, subtask, message, *, branch, remote, push, log_path, timeout=300, lane_review_fn=None
    ):
        return CommitResult(success=True, commit_sha="deadbeef" * 5, transient=False, output="ok")

    with (
        patch.object(worker, "_do_subtask", side_effect=lambda s, a, t: None),
        patch.object(
            worker,
            "_check_subtask",
            return_value=_CheckerOutcome(
                verdict=Verdict.PASS, checker_text="VERDICT: PASS", transient=False, rc=0, stderr=""
            ),
        ),
        patch.object(worker, "_pre_commit_gate", return_value=(True, "skipped")),
        patch("quikode.worker.worktree.commit_subtask", side_effect=fake_commit_subtask),
    ):
        outcome = worker._subtask_loop()

    assert outcome is None  # all settled, fall through to final_check
    s1 = worker.store.get_subtask("R-001", "S-01")
    assert s1["state"] == SubtaskState.DONE.value
    assert s1["commit_sha"] == "deadbeef" * 5
    assert (s1["pre_commit_failures"] or 0) == 0
    assert (s1["transient_retries"] or 0) == 0
    assert (s1["retries"] or 0) == 0
    worker.store.conn.close()


def test_pass_with_transient_push_failure_retries_without_burning_budget(tmp_path):
    """Network blip on push → transient_retries++ but NOT retries++. Loop
    re-attempts the doer/checker; the second attempt's commit succeeds."""
    plan = _build_plan(["S-01"])
    worker = _build_worker(tmp_path, plan)

    call_count = {"n": 0}

    def fake_commit(
        handle, subtask, message, *, branch, remote, push, log_path, timeout=300, lane_review_fn=None
    ):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return CommitResult(
                success=False,
                commit_sha=None,
                transient=True,
                output="git push failed (rc=128):\nfatal: unable to access 'https://github.com/...': Could not resolve host: github.com\n",
            )
        return CommitResult(success=True, commit_sha="cafef00d" * 5, transient=False, output="ok")

    with (
        patch.object(worker, "_do_subtask", side_effect=lambda s, a, t: None),
        patch.object(
            worker,
            "_check_subtask",
            return_value=_CheckerOutcome(
                verdict=Verdict.PASS, checker_text="VERDICT: PASS", transient=False, rc=0, stderr=""
            ),
        ),
        patch.object(worker, "_pre_commit_gate", return_value=(True, "skipped")),
        patch.object(worker, "_triage_subtask", return_value="should not be called"),
        patch("quikode.worker.worktree.commit_subtask", side_effect=fake_commit),
    ):
        outcome = worker._subtask_loop()

    assert outcome is None  # second attempt settled
    s1 = worker.store.get_subtask("R-001", "S-01")
    assert s1["state"] == SubtaskState.DONE.value
    assert (s1["transient_retries"] or 0) == 1
    # critical: real retry counter must NOT bump for transient retries
    assert (s1["retries"] or 0) == 0
    assert (s1["pre_commit_failures"] or 0) == 0
    worker.store.conn.close()


def test_pass_with_real_commit_failure_falls_into_triage_and_bumps_retries(tmp_path):
    """Non-transient commit/push failure (e.g. nothing to commit, hook
    rejection) is synthesized as a checker FAIL — triage runs and the
    real `retries` counter increments."""
    plan = _build_plan(["S-01"])
    worker = _build_worker(tmp_path, plan)

    def fake_commit(
        handle, subtask, message, *, branch, remote, push, log_path, timeout=300, lane_review_fn=None
    ):
        return CommitResult(
            success=False,
            commit_sha=None,
            transient=False,
            output="git commit failed (rc=1):\nnothing to commit, working tree clean",
        )

    triage_calls: list[str] = []

    def fake_triage(subtask, attempt, budget, checker_text):
        triage_calls.append(checker_text[:200])
        return "fix it"

    with (
        patch.object(worker, "_do_subtask", side_effect=lambda s, a, t: None),
        patch.object(
            worker,
            "_check_subtask",
            return_value=_CheckerOutcome(
                verdict=Verdict.PASS, checker_text="VERDICT: PASS", transient=False, rc=0, stderr=""
            ),
        ),
        patch.object(worker, "_pre_commit_gate", return_value=(True, "skipped")),
        patch.object(worker, "_triage_subtask", side_effect=fake_triage),
        patch("quikode.worker.worktree.commit_subtask", side_effect=fake_commit),
    ):
        outcome = worker._subtask_loop()

    # subtask_hard_max_attempts=2 → after 2 failed attempts the task BLOCKs.
    assert outcome is not None
    assert outcome.final_state is State.BLOCKED
    s1 = worker.store.get_subtask("R-001", "S-01")
    assert s1["state"] == SubtaskState.BLOCKED.value
    assert (s1["retries"] or 0) == 2
    assert (s1["transient_retries"] or 0) == 0
    # triage was called both times and saw the synthesized checker FAIL.
    assert len(triage_calls) == 2
    assert all("commit/push failed" in t for t in triage_calls)
    worker.store.conn.close()


def test_pre_commit_gate_failure_synthesizes_checker_fail(tmp_path):
    """A pre-commit hook rejection should bump pre_commit_failures and
    funnel through the existing FAIL→triage path (so the doer sees the
    hook output as feedback)."""
    plan = _build_plan(["S-01"])
    worker = _build_worker(tmp_path, plan)

    def fake_gate(subtask):
        return False, "lefthook: rustfmt failed\n--- a/foo.rs\n+++ b/foo.rs\n@@\n-bad\n+good\n"

    triage_calls: list[str] = []

    def fake_triage(subtask, attempt, budget, checker_text):
        triage_calls.append(checker_text)
        return "fix the formatting"

    # Both attempts fail the gate → subtask BLOCKs.
    with (
        patch.object(worker, "_do_subtask", side_effect=lambda s, a, t: None),
        patch.object(
            worker,
            "_check_subtask",
            return_value=_CheckerOutcome(
                verdict=Verdict.PASS, checker_text="VERDICT: PASS", transient=False, rc=0, stderr=""
            ),
        ),
        patch.object(worker, "_pre_commit_gate", side_effect=fake_gate),
        patch.object(worker, "_triage_subtask", side_effect=fake_triage),
    ):
        outcome = worker._subtask_loop()

    assert outcome is not None
    assert outcome.final_state is State.BLOCKED
    s1 = worker.store.get_subtask("R-001", "S-01")
    assert (s1["pre_commit_failures"] or 0) == 2
    assert (s1["retries"] or 0) == 2
    # triage saw the gate output
    assert len(triage_calls) == 2
    for txt in triage_calls:
        assert "pre-commit hook failed" in txt
        assert "rustfmt failed" in txt
    worker.store.conn.close()
