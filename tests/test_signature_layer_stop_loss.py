"""Plan 48: same-signature stop-loss at failure-layer granularity.

After plan 47 retired the doer envelope, every checker FAIL produced a
structurally identical signature and the plan-23 stop-loss fired after 5
attempts regardless of the underlying failure layer. Plan 48 layers the
structured `SubtaskTriageOutput.failure_layer` into the retry signature
so the stop-loss compares attempts at the layer granularity instead of
treating every FAIL as identical.
"""

from __future__ import annotations

from unittest.mock import patch

from quikode.state import State
from quikode.types import Verdict
from quikode.worker import _CheckerOutcome
from tests.test_progress_check import _build_plan, _build_worker


def test_layer_differing_attempts_do_not_trip_stop_loss(tmp_path):
    """Five consecutive checker FAILs with *different* failure layers
    produce five distinct signatures; the stop-loss must NOT fire and
    the loop runs to the hard_max ceiling instead."""
    plan = _build_plan(["S-01"])
    worker = _build_worker(
        tmp_path,
        plan,
        hard_max=5,
        progress_after=20,
        progress_every=20,
        flatline_block=10,
        same_signature_block=5,
    )
    do_calls: list[int] = []
    layers = ["local_ci", "rubric", "standards", "architecture", "behavior"]

    def fake_do(subtask, attempt, triage_notes):
        do_calls.append(attempt)

    def fake_triage(subtask, attempt, budget, checker_text):
        layer = layers[(attempt - 1) % len(layers)]
        return f"fix it: {layer}", layer

    with (
        patch.object(worker, "_do_subtask", side_effect=fake_do),
        patch.object(
            worker,
            "_check_subtask",
            return_value=_CheckerOutcome(
                verdict=Verdict.FAIL, checker_text="VERDICT: FAIL", transient=False, rc=0, stderr=""
            ),
        ),
        patch.object(worker, "_triage_subtask", side_effect=fake_triage),
    ):
        outcome = worker._subtask_loop()

    assert outcome is not None
    assert outcome.final_state is State.BLOCKED
    assert "same-signature stop-loss" not in outcome.note
    assert "hard ceiling" in outcome.note
    assert do_calls == [1, 2, 3, 4, 5]
    worker.store.conn.close()


def test_layer_constant_attempts_trip_stop_loss(tmp_path):
    """When the structured `failure_layer` is constant across N
    consecutive non-transient retries the signatures collapse and the
    stop-loss fires as expected at the new granularity."""
    plan = _build_plan(["S-01"])
    worker = _build_worker(
        tmp_path,
        plan,
        hard_max=20,
        progress_after=20,
        progress_every=20,
        flatline_block=10,
        same_signature_block=3,
    )
    do_calls: list[int] = []

    def fake_do(subtask, attempt, triage_notes):
        do_calls.append(attempt)

    def fake_triage(subtask, attempt, budget, checker_text):
        return "stuck on local_ci", "local_ci"

    with (
        patch.object(worker, "_do_subtask", side_effect=fake_do),
        patch.object(
            worker,
            "_check_subtask",
            return_value=_CheckerOutcome(
                verdict=Verdict.FAIL, checker_text="VERDICT: FAIL", transient=False, rc=0, stderr=""
            ),
        ),
        patch.object(worker, "_triage_subtask", side_effect=fake_triage),
    ):
        outcome = worker._subtask_loop()

    assert outcome is not None
    assert outcome.final_state is State.BLOCKED
    assert "same-signature stop-loss" in outcome.note
    assert do_calls == [1, 2, 3]
    worker.store.conn.close()
