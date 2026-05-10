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
from typing import Literal
from unittest.mock import MagicMock, patch

from quikode.config import Config
from quikode.dag import DAG
from quikode.fsm import Event
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


def _build_worker(
    tmp_path: Path,
    plan: Plan,
    *,
    pre_commit_runner: Literal["auto", "lefthook", "pre-commit", "none"] = "none",
) -> TaskWorker:
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
        pre_commit_runner=pre_commit_runner,
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    dag = _build_dag(tmp_path)
    store = Store(cfg.state_dir / "quikode.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.PLANNING)
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

    def fake_commit_subtask(handle, subtask, message, *, branch, remote, push, log_path, timeout=300):
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

    def fake_commit(handle, subtask, message, *, branch, remote, push, log_path, timeout=300):
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
        patch.object(worker, "_triage_subtask", return_value=("should not be called", None)),
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

    def fake_commit(handle, subtask, message, *, branch, remote, push, log_path, timeout=300):
        return CommitResult(
            success=False,
            commit_sha=None,
            transient=False,
            output="git commit failed (rc=1):\nnothing to commit, working tree clean",
        )

    triage_calls: list[str] = []

    def fake_triage(subtask, attempt, budget, checker_text):
        triage_calls.append(checker_text[:200])
        return "fix it", None

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
        return "fix the formatting", None

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


def test_no_op_done_in_addressing_feedback_skips_fsm_events(tmp_path):
    """Plan 54: when the no-op-DONE path triggers while the parent task
    is in ADDRESSING_FEEDBACK (post-PR CI-fix loop / review-response
    loop), `_handle_passed_subtask` must skip the per-subtask-loop
    `enter_committing` / `enter_pushing` FSM events. Those events fire
    `SUBTASK_PASSED` / `COMMIT_CREATED`, which the FSM only registers
    from `CHECKING_SUBTASK` / `COMMITTING`; firing them from
    `ADDRESSING_FEEDBACK` would raise `InvalidTransition` and crash the
    worker. The subtask DB row must still be marked DONE."""
    plan = _build_plan(["F-CI-1"])
    worker = _build_worker(tmp_path, plan)

    # Force the parent task into ADDRESSING_FEEDBACK before the call,
    # mirroring the CI-fix flow where the daemon already entered that
    # state before invoking the worker.
    worker.store.transition("R-001", State.ADDRESSING_FEEDBACK)

    # The no-op-DONE branch in `_handle_subtask_pass` triggers when the
    # checker_text starts with `_NO_OP_DONE_CHECKER_PREFIX`. The pass
    # path skips the commit/push call entirely (no `commit_subtask`
    # patch needed) and returns kind="settled".
    no_op_text = "VERDICT: PASS\nROOT_CAUSE: subtask no-op DONE; gates green on empty diff"
    settled, retry, checker_text = worker._handle_passed_subtask(plan.subtasks[0], checker_text=no_op_text)

    assert settled is True
    assert retry is False
    assert checker_text == ""
    # subtask DB row marked DONE without crashing on the FSM event
    s1 = worker.store.get_subtask("R-001", "F-CI-1")
    assert s1["state"] == SubtaskState.DONE.value
    # parent task state unchanged — feedback caller advances it later
    parent = worker.store.get("R-001")
    assert parent["state"] == State.ADDRESSING_FEEDBACK.value
    worker.store.conn.close()


def test_settled_in_per_subtask_loop_state_fires_fsm_events(tmp_path):
    """Plan 54 regression guard: when the parent task IS in a per-
    subtask-loop state (`CHECKING_SUBTASK` here), `_handle_passed_subtask`
    must still fire the `enter_committing` / `enter_pushing` FSM events
    so the per-subtask-loop progresses normally."""
    plan = _build_plan(["S-01"])
    worker = _build_worker(tmp_path, plan)

    # Reach CHECKING_SUBTASK via the canonical FSM path so apply_event
    # transitions are registered. PLANNING → DOING_SUBTASK (PLAN_VALID)
    # → CHECKING_SUBTASK (DOER_DONE).
    worker.store.apply_event("R-001", Event.PLAN_VALID)
    worker.store.apply_event("R-001", Event.DOER_DONE)
    assert worker.store.get("R-001")["state"] == State.CHECKING_SUBTASK.value

    def fake_commit_subtask(handle, subtask, message, *, branch, remote, push, log_path, timeout=300):
        return CommitResult(success=True, commit_sha="ab" * 20, transient=False, output="ok")

    with (
        patch.object(worker, "_pre_commit_gate", return_value=(True, "skipped")),
        patch("quikode.worker.worktree.commit_subtask", side_effect=fake_commit_subtask),
    ):
        settled, retry, checker_text = worker._handle_passed_subtask(
            plan.subtasks[0], checker_text="VERDICT: PASS"
        )

    assert settled is True
    assert retry is False
    assert checker_text == ""
    # FSM advanced through COMMITTING → PUSHING
    assert worker.store.get("R-001")["state"] == State.PUSHING.value
    worker.store.conn.close()


def test_subtask_loop_picks_up_leftover_fixup_subtasks_on_resume(tmp_path):
    """Resume mid-fixup: the daemon was running fixup subtasks (added by the
    audit-driven fixup planner) when it died. Orphan recovery sends the task
    back to PENDING with a resume marker. On the next worker run, _plan()
    skips planning, _subtask_loop iterates self.plan.topo_order() — which
    only includes the *original* spec subtasks, not the fixups — so all
    spec subtasks short-circuit as DONE and the loop returns None. Without
    the leftover-fixup pickup, _commit_push then fast-forwards to
    LOCAL_CI_CHECKING with half-applied audit findings."""
    plan = _build_plan(["S-01"])
    worker = _build_worker(tmp_path, plan)

    # Mark S-01 done — already committed before the simulated restart.
    worker.store.update_subtask("R-001", "S-01", state=SubtaskState.DONE.value)

    # Append two fixup subtasks (audit-driven) to the store. One is still
    # in DOING (the doer was running when daemon died); one is fully
    # PENDING. Neither is in self.plan.topo_order().
    worker.store.append_subtasks(
        "R-001",
        [
            {
                "subtask_id": "F-1-1-fix-formatting",
                "title": "Fix formatting issues found by rubric audit",
                "depends_on": [],
                "files_to_touch": ["src/foo.rs"],
                "boundary": "",
                "acceptance": ["cargo fmt --check passes"],
                "notes": "",
                "kind": "fixup-pre-pr-audit",
            },
            {
                "subtask_id": "F-1-2-fix-types",
                "title": "Fix type errors found by behavior audit",
                "depends_on": [],
                "files_to_touch": ["src/bar.rs"],
                "boundary": "",
                "acceptance": ["cargo check passes"],
                "notes": "",
                "kind": "fixup-pre-pr-audit",
            },
        ],
    )
    # Simulate the doer was mid-run on F-1-1 when the daemon died.
    worker.store.update_subtask("R-001", "F-1-1-fix-formatting", state=SubtaskState.DOING.value)

    fixup_ids_run: list[str] = []

    def fake_commit(handle, subtask, message, *, branch, remote, push, log_path, timeout=300):
        fixup_ids_run.append(subtask.id)
        return CommitResult(success=True, commit_sha="cafe" * 10, transient=False, output="ok")

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
        patch("quikode.worker.worktree.commit_subtask", side_effect=fake_commit),
    ):
        outcome = worker._subtask_loop()

    assert outcome is None, "all subtasks settled (spec + leftover fixups)"
    assert "F-1-1-fix-formatting" in fixup_ids_run
    assert "F-1-2-fix-types" in fixup_ids_run
    # Both fixups should now be DONE.
    f1 = worker.store.get_subtask("R-001", "F-1-1-fix-formatting")
    f2 = worker.store.get_subtask("R-001", "F-1-2-fix-types")
    assert f1["state"] == SubtaskState.DONE.value
    assert f2["state"] == SubtaskState.DONE.value
    worker.store.conn.close()
