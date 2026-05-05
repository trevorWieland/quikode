"""v3 Phase A: progress-check agent + retry overhaul.

The progress-check agent is invoked periodically inside `_subtask_loop`
when the doer/checker pair has been retrying without converging. Three
gating fields:
- `subtask_hard_max_attempts` (absolute ceiling, default 30)
- `subtask_progress_check_after` (first check, default 4)
- `subtask_progress_check_every` (re-check cadence, default 3)
- `subtask_flatline_block_count` (consecutive flatlines = block, default 2)

These tests cover:
1. ProgressAgent.check happy path — JSON output parsed correctly.
2. Parse-failure → uncertain.
3. Agent transient (raised exception) → uncertain (advisory, not crashing).
4. Loop blocks at the right attempt count when progress is FLATLINED.
5. Loop runs to hard_max when progress is always PROGRESSING.
6. flatline_count resets on progressing.
7. Transient retries don't bump the attempt counter.
8. progress_checks audit rows are written.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from quikode.agents.progress import (
    ProgressAgent,
    ProgressAttempt,
    ProgressVerdict,
    _parse_progress_output,
)
from quikode.config import Config
from quikode.dag import DAG
from quikode.state import State, Store, SubtaskState
from quikode.subtask_schema import Plan, Subtask
from quikode.types import AgentResult, Verdict
from quikode.worker import (
    TaskWorker,
    _CheckerOutcome,
)
from quikode.worktree import CommitResult

# ----- helpers shared with other tests -----


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
    hard_max: int = 8,
    progress_after: int = 3,
    progress_every: int = 2,
    flatline_block: int = 2,
) -> TaskWorker:
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        prompts_dir=tmp_path / "missing-prompts",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
        subtask_hard_max_attempts=hard_max,
        subtask_progress_check_after=progress_after,
        subtask_progress_check_every=progress_every,
        subtask_flatline_block_count=flatline_block,
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    dag = _build_dag(tmp_path)
    store = Store(cfg.state_dir / "quikode.db")
    store.upsert_pending("R-001")
    store.set_field("R-001", branch="quikode/r-001-abc")
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


# ----- _parse_progress_output -----


def test_parse_clean_json():
    raw = json.dumps({"verdict": "progressing", "rationale": "root cause shifted"})
    v = _parse_progress_output(raw)
    assert v.verdict == "progressing"
    assert "shifted" in v.rationale


def test_parse_flatlined():
    raw = json.dumps({"verdict": "flatlined", "rationale": "same error 3x"})
    v = _parse_progress_output(raw)
    assert v.verdict == "flatlined"


def test_parse_uncertain():
    raw = json.dumps({"verdict": "uncertain", "rationale": "too few attempts"})
    v = _parse_progress_output(raw)
    assert v.verdict == "uncertain"


def test_parse_extracts_object_from_preamble():
    """Agent occasionally includes prose preamble despite the prompt."""
    raw = (
        "Sure, here is the verdict:\n\n"
        + json.dumps({"verdict": "flatlined", "rationale": "X"})
        + "\n\nLet me know if you want more."
    )
    v = _parse_progress_output(raw)
    assert v.verdict == "flatlined"


def test_parse_garbage_falls_back_to_uncertain():
    v = _parse_progress_output("this is not json at all")
    assert v.verdict == "uncertain"
    assert "failed to parse" in v.rationale


def test_parse_empty_falls_back_to_uncertain():
    v = _parse_progress_output("")
    assert v.verdict == "uncertain"


def test_parse_unknown_verdict_value_falls_back():
    raw = json.dumps({"verdict": "weird-state", "rationale": "x"})
    v = _parse_progress_output(raw)
    assert v.verdict == "uncertain"
    assert "unknown verdict" in v.rationale


# ----- ProgressAgent.check -----


def _stub_handle():
    h = MagicMock()
    h.container_name = "qk-stub"
    return h


def test_progress_agent_happy_path(tmp_path):
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

    fake_result = AgentResult(
        rc=0,
        stdout=json.dumps({"verdict": "progressing", "rationale": "narrowed area"}),
        stderr="",
    )
    fake_agent = MagicMock()
    fake_agent.run.return_value = fake_result

    subtask = Subtask(id="S-01", title="x", acceptance=("a",), files_to_touch=("foo.rs",))
    pa = ProgressAgent(cfg)
    with patch("quikode.agents.progress.build_agent", return_value=fake_agent):
        outcome = pa.check(
            subtask=subtask,
            attempts=[ProgressAttempt(attempt_no=1, checker_root_cause="a", triage_notes="b")],
            acceptance=("a",),
            handle=_stub_handle(),
        )
    assert outcome.verdict == "progressing"
    assert "narrowed" in outcome.rationale


def test_progress_agent_parse_failure_returns_uncertain(tmp_path):
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

    fake_result = AgentResult(rc=0, stdout="not json at all sorry", stderr="")
    fake_agent = MagicMock()
    fake_agent.run.return_value = fake_result

    subtask = Subtask(id="S-01", title="x", acceptance=("a",), files_to_touch=("foo.rs",))
    pa = ProgressAgent(cfg)
    with patch("quikode.agents.progress.build_agent", return_value=fake_agent):
        outcome = pa.check(
            subtask=subtask,
            attempts=[ProgressAttempt(attempt_no=1, checker_root_cause="x", triage_notes="y")],
            acceptance=("a",),
            handle=_stub_handle(),
        )
    assert outcome.verdict == "uncertain"
    assert "failed to parse" in outcome.rationale


def test_progress_agent_invocation_raises_returns_uncertain(tmp_path):
    """Agent timeout/transient — exception caught, verdict=uncertain.
    The progress check is advisory; a crash here must not crash the
    worker's subtask loop."""
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

    fake_agent = MagicMock()
    fake_agent.run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=10)

    subtask = Subtask(id="S-01", title="x", acceptance=("a",), files_to_touch=("foo.rs",))
    pa = ProgressAgent(cfg)
    with patch("quikode.agents.progress.build_agent", return_value=fake_agent):
        outcome = pa.check(
            subtask=subtask,
            attempts=[ProgressAttempt(attempt_no=1, checker_root_cause="x", triage_notes="y")],
            acceptance=("a",),
            handle=_stub_handle(),
        )
    assert outcome.verdict == "uncertain"
    assert "transient" in outcome.rationale.lower()


def test_progress_agent_nonzero_rc_returns_uncertain(tmp_path):
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

    fake_result = AgentResult(rc=2, stdout="", stderr="boom")
    fake_agent = MagicMock()
    fake_agent.run.return_value = fake_result

    subtask = Subtask(id="S-01", title="x", acceptance=("a",), files_to_touch=("foo.rs",))
    pa = ProgressAgent(cfg)
    with patch("quikode.agents.progress.build_agent", return_value=fake_agent):
        outcome = pa.check(
            subtask=subtask,
            attempts=[],
            acceptance=("a",),
            handle=_stub_handle(),
        )
    assert outcome.verdict == "uncertain"
    assert "rc=2" in outcome.rationale


# ----- progress check cadence seeded from retries (regression #24) -----


def test_progress_check_cadence_seeds_attempt_from_retries_after_resume(tmp_path):
    """Regression for #24: the local attempt counter must seed from the
    DB row's `retries` so cadence (fires at 6, 9, 12, ...) keeps firing
    across daemon restarts. Without this, a long-running stuck subtask
    survives multiple restart cycles, each restarting `attempt=0` and
    only ever firing the progress check at the first cadence point."""
    plan = _build_plan(["S-01"])
    worker = _build_worker(tmp_path, plan, progress_after=6, progress_every=3, hard_max=20)
    # Persist a subtask row simulating a resume mid-stuck-subtask: retries=12.
    worker.store.update_subtask("R-001", "S-01", retries=12)

    progress_called_with: list[int] = []

    def fake_progress(subtask, attempt):
        progress_called_with.append(attempt)
        return ProgressVerdict(verdict="progressing", rationale="x")

    do_count = {"n": 0}

    def fake_do(subtask, attempt, triage_notes):
        do_count["n"] += 1
        if do_count["n"] > 3:
            raise RuntimeError("test-stop")

    with (
        patch.object(worker, "_do_subtask", side_effect=fake_do),
        patch.object(
            worker,
            "_check_subtask",
            return_value=_CheckerOutcome(
                verdict=Verdict.FAIL, checker_text="VERDICT: FAIL", transient=False, rc=0, stderr=""
            ),
        ),
        patch.object(worker, "_triage_subtask", return_value="x"),
        patch.object(worker, "_run_progress_check", side_effect=fake_progress),
        patch.object(worker, "_handle_parent_rebase_if_needed", return_value=None),
        patch.object(worker, "_handle_branch_divergence_if_needed", return_value=None),
        patch("quikode.worker.time.sleep"),
    ):
        try:
            worker._run_subtask_set([plan.subtasks[0]])
        except RuntimeError as e:
            assert "test-stop" in str(e)

    # Cadence with after=6, every=3 fires at attempts 6, 9, 12, 15, 18.
    # Without the fix, post-restart the local attempt would have been
    # 1, 2, 3 → no progress check ever fires. With the fix, retries=12
    # seeds attempt to 12, so the first iteration is attempt=13, second 14,
    # third 15 — and 15 hits the cadence.
    assert any(a >= 13 for a in progress_called_with), (
        f"progress_check should have fired at attempt >= 13 (seeded from "
        f"retries=12); got attempts={progress_called_with}"
    )


# ----- _should_run_progress_check cadence -----


def test_should_run_progress_check_cadence(tmp_path):
    plan = _build_plan(["S-01"])
    worker = _build_worker(tmp_path, plan, progress_after=4, progress_every=3)
    # before the first window
    assert not worker._should_run_progress_check(1)
    assert not worker._should_run_progress_check(3)
    # first window
    assert worker._should_run_progress_check(4)
    # not until +every
    assert not worker._should_run_progress_check(5)
    assert not worker._should_run_progress_check(6)
    assert worker._should_run_progress_check(7)
    assert worker._should_run_progress_check(10)
    worker.store.conn.close()


# ----- _subtask_loop with stubbed progress agent -----


def test_loop_blocks_when_progress_flatlines(tmp_path):
    """Stub: checker always FAILs, progress always returns flatlined.
    With progress_after=2, every=2, flatline_block=2:
    - attempt 1: FAIL
    - attempt 2: FAIL → progress check #1: flatlined (count=1)
    - attempt 3: FAIL
    - attempt 4: FAIL → progress check #2: flatlined (count=2) → BLOCK
    """
    plan = _build_plan(["S-01"])
    worker = _build_worker(tmp_path, plan, hard_max=20, progress_after=2, progress_every=2, flatline_block=2)

    do_calls: list[int] = []

    def fake_do(subtask, attempt, triage_notes):
        do_calls.append(attempt)

    fake_progress = MagicMock()
    fake_progress.check.return_value = ProgressVerdict(verdict="flatlined", rationale="same error")

    with (
        patch.object(worker, "_do_subtask", side_effect=fake_do),
        patch.object(
            worker,
            "_check_subtask",
            return_value=_CheckerOutcome(
                verdict=Verdict.FAIL, checker_text="VERDICT: FAIL", transient=False, rc=0, stderr=""
            ),
        ),
        patch.object(worker, "_triage_subtask", return_value="fix it"),
        patch("quikode.worker.build_progress_agent", return_value=fake_progress),
    ):
        outcome = worker._subtask_loop()

    assert outcome is not None
    assert outcome.final_state is State.BLOCKED
    assert "flatlined" in outcome.note
    # 4 doer attempts before block
    assert do_calls == [1, 2, 3, 4]
    s1 = worker.store.get_subtask("R-001", "S-01")
    assert s1["state"] == SubtaskState.BLOCKED.value
    assert (s1["flatline_count"] or 0) == 2
    assert (s1["progress_check_count"] or 0) == 2
    # 2 progress check audit rows
    rows = worker.store.get_recent_progress_checks("R-001", "S-01")
    assert len(rows) == 2
    assert all(r["verdict"] == "flatlined" for r in rows)
    worker.store.conn.close()


def test_loop_runs_to_hard_max_when_always_progressing(tmp_path):
    """Progress agent says 'progressing' every check → loop must run
    through the full hard_max attempts before blocking on the ceiling."""
    plan = _build_plan(["S-01"])
    worker = _build_worker(tmp_path, plan, hard_max=6, progress_after=2, progress_every=2, flatline_block=2)

    do_calls: list[int] = []

    def fake_do(subtask, attempt, triage_notes):
        do_calls.append(attempt)

    fake_progress = MagicMock()
    fake_progress.check.return_value = ProgressVerdict(verdict="progressing", rationale="narrowed")

    with (
        patch.object(worker, "_do_subtask", side_effect=fake_do),
        patch.object(
            worker,
            "_check_subtask",
            return_value=_CheckerOutcome(
                verdict=Verdict.FAIL, checker_text="VERDICT: FAIL", transient=False, rc=0, stderr=""
            ),
        ),
        patch.object(worker, "_triage_subtask", return_value="fix it"),
        patch("quikode.worker.build_progress_agent", return_value=fake_progress),
    ):
        outcome = worker._subtask_loop()

    assert outcome is not None
    assert outcome.final_state is State.BLOCKED
    assert "hard ceiling of 6 attempts" in outcome.note
    assert do_calls == [1, 2, 3, 4, 5, 6]
    s1 = worker.store.get_subtask("R-001", "S-01")
    assert (s1["flatline_count"] or 0) == 0  # always reset on progressing
    # progress checks fire at attempts 2, 4, 6 — 3 audit rows.
    rows = worker.store.get_recent_progress_checks("R-001", "S-01")
    assert len(rows) == 3
    assert all(r["verdict"] == "progressing" for r in rows)
    worker.store.conn.close()


def test_progressing_resets_flatline_count(tmp_path):
    """flatlined → progressing → flatlined → progressing — never block,
    because progressing zeroes out the consecutive counter."""
    plan = _build_plan(["S-01"])
    worker = _build_worker(tmp_path, plan, hard_max=10, progress_after=2, progress_every=2, flatline_block=2)

    fake_progress = MagicMock()
    # Cadence: attempts 2, 4, 6, 8, 10. Pattern: flatlined, progressing, flatlined, progressing, ...
    fake_progress.check.side_effect = [
        ProgressVerdict(verdict="flatlined", rationale="x"),
        ProgressVerdict(verdict="progressing", rationale="y"),
        ProgressVerdict(verdict="flatlined", rationale="z"),
        ProgressVerdict(verdict="progressing", rationale="w"),
        ProgressVerdict(verdict="flatlined", rationale="v"),
    ]

    with (
        patch.object(worker, "_do_subtask", side_effect=lambda s, a, t: None),
        patch.object(
            worker,
            "_check_subtask",
            return_value=_CheckerOutcome(
                verdict=Verdict.FAIL, checker_text="VERDICT: FAIL", transient=False, rc=0, stderr=""
            ),
        ),
        patch.object(worker, "_triage_subtask", return_value="fix it"),
        patch("quikode.worker.build_progress_agent", return_value=fake_progress),
    ):
        outcome = worker._subtask_loop()

    # Either hits hard_max or blocks via never-2-in-a-row flatlines. With
    # this pattern, flatline_count never exceeds 1 — block reason should
    # be the hard ceiling, NOT flatline.
    assert outcome is not None
    assert outcome.final_state is State.BLOCKED
    assert "hard ceiling" in outcome.note
    s1 = worker.store.get_subtask("R-001", "S-01")
    # last check was flatlined (count=1, not 2)
    assert (s1["flatline_count"] or 0) == 1
    worker.store.conn.close()


def test_uncertain_does_not_bump_flatline_count(tmp_path):
    """uncertain verdicts should NOT count toward flatline. They reset
    the flatline counter (defensive: uncertainty isn't progress, but it's
    also not a confirmed flatline)."""
    plan = _build_plan(["S-01"])
    worker = _build_worker(tmp_path, plan, hard_max=8, progress_after=2, progress_every=2, flatline_block=2)

    fake_progress = MagicMock()
    fake_progress.check.side_effect = [
        ProgressVerdict(verdict="flatlined", rationale="x"),
        ProgressVerdict(verdict="uncertain", rationale="y"),
        ProgressVerdict(verdict="flatlined", rationale="z"),
        ProgressVerdict(verdict="uncertain", rationale="w"),
    ]

    with (
        patch.object(worker, "_do_subtask", side_effect=lambda s, a, t: None),
        patch.object(
            worker,
            "_check_subtask",
            return_value=_CheckerOutcome(
                verdict=Verdict.FAIL, checker_text="VERDICT: FAIL", transient=False, rc=0, stderr=""
            ),
        ),
        patch.object(worker, "_triage_subtask", return_value="fix it"),
        patch("quikode.worker.build_progress_agent", return_value=fake_progress),
    ):
        outcome = worker._subtask_loop()

    assert outcome is not None
    assert outcome.final_state is State.BLOCKED
    assert "hard ceiling" in outcome.note  # never reached 2 consecutive flatlines
    worker.store.conn.close()


def test_transient_retries_do_not_count_toward_attempts(tmp_path):
    """Doer succeeds and PASSes; commit gate transient-fails 3 times then
    succeeds. retries should stay 0; transient_retries should be 3.
    Subtask should converge well under hard_max attempts."""
    plan = _build_plan(["S-01"])
    worker = _build_worker(tmp_path, plan, hard_max=4, progress_after=10, progress_every=10, flatline_block=2)

    call_count = {"n": 0}

    def fake_commit(handle, subtask, message, *, branch, remote, push, log_path, timeout=300):
        call_count["n"] += 1
        if call_count["n"] <= 3:
            return CommitResult(success=False, commit_sha=None, transient=True, output="network blip")
        return CommitResult(success=True, commit_sha="abc" * 10, transient=False, output="ok")

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

    # Loop converged — fall through to None (final_check).
    assert outcome is None
    s1 = worker.store.get_subtask("R-001", "S-01")
    assert s1["state"] == SubtaskState.DONE.value
    assert (s1["transient_retries"] or 0) == 3
    assert (s1["retries"] or 0) == 0
    worker.store.conn.close()


def test_progress_check_audit_rows_written(tmp_path):
    """Every progress-check call writes an audit row with attempts_at_check
    and verdict. Sanity check the schema is wired."""
    plan = _build_plan(["S-01"])
    # flatline_block=10 (the max) so all 3 progress checks fire — none of
    # the patterns we feed reach 10 consecutive flatlines.
    worker = _build_worker(tmp_path, plan, hard_max=6, progress_after=2, progress_every=2, flatline_block=10)

    fake_progress = MagicMock()
    fake_progress.check.side_effect = [
        ProgressVerdict(verdict="progressing", rationale="r1"),
        ProgressVerdict(verdict="uncertain", rationale="r2"),
        ProgressVerdict(verdict="flatlined", rationale="r3"),
    ]

    with (
        patch.object(worker, "_do_subtask", side_effect=lambda s, a, t: None),
        patch.object(
            worker,
            "_check_subtask",
            return_value=_CheckerOutcome(
                verdict=Verdict.FAIL, checker_text="VERDICT: FAIL", transient=False, rc=0, stderr=""
            ),
        ),
        patch.object(worker, "_triage_subtask", return_value="fix it"),
        patch("quikode.worker.build_progress_agent", return_value=fake_progress),
    ):
        worker._subtask_loop()

    rows = worker.store.get_recent_progress_checks("R-001", "S-01", limit=10)
    assert len(rows) == 3
    verdicts_in_order = [r["verdict"] for r in reversed(rows)]
    assert verdicts_in_order == ["progressing", "uncertain", "flatlined"]
    rationales_in_order = [r["rationale"] for r in reversed(rows)]
    assert rationales_in_order == ["r1", "r2", "r3"]
    # attempts_at_check should be 2, 4, 6
    attempts_in_order = [r["attempts_at_check"] for r in reversed(rows)]
    assert attempts_in_order == [2, 4, 6]
    worker.store.conn.close()


def test_loop_blocks_immediately_when_first_check_already_flatlines_at_block_count_one(tmp_path):
    """flatline_block_count=1 → first flatlined verdict blocks immediately."""
    plan = _build_plan(["S-01"])
    worker = _build_worker(tmp_path, plan, hard_max=20, progress_after=2, progress_every=2, flatline_block=1)

    fake_progress = MagicMock()
    fake_progress.check.return_value = ProgressVerdict(verdict="flatlined", rationale="boom")

    do_calls: list[int] = []

    def fake_do(subtask, attempt, triage_notes):
        do_calls.append(attempt)

    with (
        patch.object(worker, "_do_subtask", side_effect=fake_do),
        patch.object(
            worker,
            "_check_subtask",
            return_value=_CheckerOutcome(
                verdict=Verdict.FAIL, checker_text="VERDICT: FAIL", transient=False, rc=0, stderr=""
            ),
        ),
        patch.object(worker, "_triage_subtask", return_value="fix it"),
        patch("quikode.worker.build_progress_agent", return_value=fake_progress),
    ):
        outcome = worker._subtask_loop()

    assert outcome is not None
    assert outcome.final_state is State.BLOCKED
    assert "flatlined 1 consecutive times" in outcome.note
    assert do_calls == [1, 2]
    worker.store.conn.close()
