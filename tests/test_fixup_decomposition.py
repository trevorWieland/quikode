"""v3 fixup decomposition: when final-check or CI fails, the worker invokes
the fixup planner to break the fix into per-subtask slices instead of one
monolithic doer attempt. Each slice runs through the same per-subtask
doer/checker/triage loop as the original spec subtasks, with its own
per-subtask commit. This is the structural fix for the v0 1-2h whole-spec
fixup-doer that lost session context and converged unreliably.

The full integration path (real planner agent + real subtasks running in a
container) is exercised by the fixture E2E. These unit tests cover the
worker-level wiring and the schema layer.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quikode.config import Config
from quikode.dag import DAG
from quikode.state import State, Store, SubtaskState
from quikode.subtask_schema import (
    FixupPlan,
    Plan,
    PlanValidationError,
    Subtask,
    parse_fixup_planner_output,
)
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


def _build_worker(tmp_path: Path, *, fixup_max_rounds: int = 3) -> TaskWorker:
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        prompts_dir=tmp_path / "missing-prompts",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
        fixup_max_rounds=fixup_max_rounds,
        subtask_transient_max_retries=3,
        subtask_hard_max_attempts=3,
        subtask_progress_check_after=10,  # don't fire under cap
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
                id="S-01",
                title="domain",
                depends_on=(),
                files_to_touch=("a.rs",),
                boundary="",
                acceptance=("compiles",),
                notes="",
            ),
        ),
        final_acceptance=("just ci",),
    )
    worker.plan_text = "stub plan"
    # Mark the spec subtask DONE so fixup-round simulation starts at final-check.
    store.upsert_subtasks(
        "R-001",
        [
            {
                "subtask_id": "S-01",
                "title": "domain",
                "acceptance": ["compiles"],
                "files_to_touch": ["a.rs"],
            }
        ],
    )
    store.update_subtask("R-001", "S-01", state=SubtaskState.DONE.value)
    return worker


# ----- schema -----


def test_subtasks_schema_has_kind_column(tmp_path):
    """Fresh DB should have the v3 fixup `kind` column (default 'spec')."""
    db = tmp_path / "fresh.db"
    Store(db).conn.close()
    conn = sqlite3.connect(db)
    cols = {r[1]: r[4] for r in conn.execute("PRAGMA table_info(subtasks)")}
    conn.close()
    assert "kind" in cols
    assert "'spec'" in str(cols["kind"]).lower() or cols["kind"] == "'spec'"


def test_migration_adds_kind_column_to_old_db(tmp_path):
    """An older subtasks table without the `kind` column auto-migrates."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("""
        CREATE TABLE subtasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL, subtask_id TEXT NOT NULL,
            state TEXT NOT NULL, created_at REAL, updated_at REAL,
            UNIQUE(task_id, subtask_id)
        )
    """)
    conn.execute(
        "INSERT INTO subtasks (task_id, subtask_id, state, created_at, updated_at) "
        "VALUES ('R-OLD','S-1','pending',1.0,1.0)"
    )
    conn.close()
    Store(db).conn.close()  # migrate
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(subtasks)")}
    assert "kind" in cols
    # Pre-existing row preserved with default kind='spec'.
    kind = conn.execute("SELECT kind FROM subtasks WHERE subtask_id='S-1'").fetchone()[0]
    assert kind == "spec"
    conn.close()


def test_append_subtasks_does_not_clobber_existing(tmp_path):
    """append_subtasks adds new rows without deleting existing — used by the
    fixup planner to layer fixup slices on top of original spec subtasks."""
    Store(tmp_path / ".quikode" / "quikode.db").conn.close()  # bootstrap dir
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.upsert_subtasks(
        "R-001",
        [{"subtask_id": "S-01", "title": "spec slice", "acceptance": ["x"]}],
    )
    store.append_subtasks(
        "R-001",
        [
            {
                "subtask_id": "F-1-1-line-budget",
                "title": "fixup slice",
                "acceptance": ["y"],
                "kind": "fixup-final",
            }
        ],
    )
    rows = store.list_subtasks("R-001")
    by_id = {r["subtask_id"]: r for r in rows}
    assert "S-01" in by_id
    assert "F-1-1-line-budget" in by_id
    assert by_id["S-01"].get("kind") == "spec"
    assert by_id["F-1-1-line-budget"].get("kind") == "fixup-final"
    store.conn.close()


def test_append_subtasks_skips_duplicate_id(tmp_path):
    """Append must not error on a planner repeat — second insert of same id is skipped."""
    Store(tmp_path / ".quikode" / "quikode.db").conn.close()
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.upsert_subtasks(
        "R-001",
        [{"subtask_id": "S-01", "title": "first", "acceptance": ["x"]}],
    )
    store.append_subtasks(
        "R-001",
        [
            # Duplicates the existing S-01 + adds a fresh F-1.
            {"subtask_id": "S-01", "title": "WRONG", "acceptance": ["wrong"]},
            {"subtask_id": "F-1", "title": "fixup", "acceptance": ["y"]},
        ],
    )
    rows = store.list_subtasks("R-001")
    by_id = {r["subtask_id"]: r["title"] for r in rows}
    # S-01 was NOT overwritten by the duplicate insert.
    assert by_id["S-01"] == "first"
    assert "F-1" in by_id
    store.conn.close()


# ----- FixupPlan parsing -----


def test_parse_fixup_planner_output_valid():
    text = """Here is the fixup plan:
```json
{
  "summary": "split big files",
  "subtasks": [
    {
      "id": "F-1-1-line-budget",
      "title": "Split big.rs",
      "depends_on": [],
      "files_to_touch": ["crates/foo/src/big.rs"],
      "boundary": "refactor only",
      "acceptance": ["no file > 500 lines"],
      "interfaces": [],
      "notes": "",
      "kind": "fixup-final"
    }
  ]
}
```"""
    plan = parse_fixup_planner_output(text)
    assert isinstance(plan, FixupPlan)
    assert len(plan.subtasks) == 1
    assert plan.subtasks[0].id == "F-1-1-line-budget"
    assert plan.subtasks[0].kind == "fixup-final"


def test_parse_fixup_planner_output_rejects_empty_subtasks():
    text = """```json
{"summary": "x", "subtasks": []}
```"""
    with pytest.raises(PlanValidationError):
        parse_fixup_planner_output(text)


# ----- worker fixup-round flow -----


def test_final_check_loop_invokes_fixup_planner_on_fail(tmp_path):
    """When _check returns FAIL/fail, the loop calls _run_fixup_round (which
    calls _invoke_fixup_planner). Successful planner output → fixup subtasks
    appended + run; re-check happens after."""
    worker = _build_worker(tmp_path, fixup_max_rounds=2)
    # First _check: FAIL. Second _check (after fixup round 1): PASS.
    seq = [
        (Verdict.FAIL, "fail", "ci log here", "VERDICT: FAIL\nROOT_CAUSE: line budget", False),
        (Verdict.PASS, "pass", None, "VERDICT: PASS", False),
    ]
    call_count = {"n": 0}

    def fake_check():
        i = call_count["n"]
        call_count["n"] += 1
        return seq[min(i, len(seq) - 1)]

    fixup_plan = FixupPlan(
        summary="x",
        subtasks=(
            Subtask(
                id="F-1-line-budget",
                title="x",
                depends_on=(),
                files_to_touch=("a.rs",),
                boundary="",
                acceptance=("ok",),
                notes="",
                kind="fixup-final",
            ),
        ),
    )

    def fake_run_subtask_set(subtasks):
        # Pretend the fixup subtask landed cleanly.
        for s in subtasks:
            worker.store.update_subtask("R-001", s.id, state=SubtaskState.DONE.value)

    with (
        patch.object(worker, "_check", side_effect=fake_check),
        patch.object(worker, "_invoke_fixup_planner", return_value=fixup_plan),
        patch.object(worker, "_run_subtask_set", side_effect=fake_run_subtask_set),
        patch.object(worker, "_handle_parent_rebase_if_needed", return_value=None),
    ):
        outcome = worker._final_check_loop()

    assert outcome is None  # success
    # _check called twice: once before fixup, once after.
    assert call_count["n"] == 2
    # Fixup subtask was persisted.
    rows = worker.store.list_subtasks("R-001")
    by_id = {r["subtask_id"]: r for r in rows}
    assert "F-1-line-budget" in by_id
    assert by_id["F-1-line-budget"].get("kind") == "fixup-final"
    worker.store.conn.close()


def test_final_check_loop_falls_back_to_monolithic_doer_when_planner_fails(tmp_path):
    """If the fixup planner returns None (parse error, agent rc!=0), the worker
    falls back to the legacy `_do(attempt=200+round)` monolithic call so we
    never get stuck without ANY attempt at fixing."""
    worker = _build_worker(tmp_path, fixup_max_rounds=1)

    # First _check: FAIL. After fallback _do, fixup_round=1 increments; with
    # max_rounds=1 the loop exits BLOCKED on the next iteration since the
    # round-cap is exceeded only when a SECOND fail would have triggered.
    # Actually: round becomes 1 (== max_rounds), runs the fallback, comes back
    # and re-checks; second FAIL → round 2 > max_rounds=1 → BLOCKED.
    seq = [
        (Verdict.FAIL, "fail", "x", "VERDICT: FAIL", False),
        (Verdict.FAIL, "fail", "x", "VERDICT: FAIL", False),
    ]
    call_count = {"n": 0}

    def fake_check():
        i = call_count["n"]
        call_count["n"] += 1
        return seq[min(i, len(seq) - 1)]

    do_calls: list[int] = []

    def fake_do(attempt):
        do_calls.append(attempt)

    with (
        patch.object(worker, "_check", side_effect=fake_check),
        patch.object(worker, "_invoke_fixup_planner", return_value=None),
        patch.object(worker, "_do", side_effect=fake_do),
        patch.object(worker, "_handle_parent_rebase_if_needed", return_value=None),
    ):
        outcome = worker._final_check_loop()

    assert outcome is not None
    assert outcome.final_state is State.BLOCKED
    # Fallback _do called once with the legacy 200+1 = 201 attempt number.
    assert do_calls == [201]
    worker.store.conn.close()
