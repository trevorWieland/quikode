"""v3 Phase B: TaskWorker.run_review_response.

Mocks the agent calls + commit/push/resolve_thread surfaces and walks the
worker through a review-response cycle, asserting state transitions, thread
resolution, review_round increment, and the pre-commit/checker fail behavior.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from quikode.config import Config
from quikode.dag import DAG
from quikode.github_graphql import ReviewThread
from quikode.state import State, Store
from quikode.types import Verdict
from quikode.worker import TaskWorker
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


def _build_worker(tmp_path: Path) -> TaskWorker:
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
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    cfg.worktree_root.mkdir(parents=True, exist_ok=True)
    dag = _build_dag(tmp_path)
    store = Store(cfg.state_dir / "q.db")
    store.upsert_pending("R-001")
    # Seed a worktree path on the row — required for re-provisioning.
    wt_path = cfg.worktree_root / "r-001"
    wt_path.mkdir(parents=True, exist_ok=True)
    store.transition(
        "R-001",
        State.AWAITING_MERGE,
        branch="quikode/r-001-abc123",
        worktree_path=str(wt_path),
        pr_number=42,
        pr_url="https://github.com/owner/repo/pull/42",
        plan_text="(stub plan text)",
    )
    worker = TaskWorker(cfg, dag, store, dag.nodes["R-001"])
    # Dummy handle so _h works in branches that don't tear down.
    worker.handle = MagicMock()
    worker.handle.container_name = "qk-stub"
    return worker


def _make_thread(thread_id: str = "PRRT_1") -> ReviewThread:
    return ReviewThread(
        thread_id=thread_id,
        is_resolved=False,
        is_outdated=False,
        path="src/foo.py",
        line=10,
        last_comment_id="PRC_1",
        last_comment_author="alice",
        last_comment_body="Please rename x to y",
        last_comment_created_at=1000.0,
        last_comment_is_bot=False,
    )


def _ok_commit() -> CommitResult:
    return CommitResult(success=True, commit_sha="deadbeef", transient=False, output="ok")


def _failed_commit() -> CommitResult:
    return CommitResult(success=False, commit_sha=None, transient=False, output="hook rejected")


# ----- happy path -----


def test_run_review_response_happy_path(tmp_path):
    worker = _build_worker(tmp_path)
    threads = [_make_thread("PRRT_1"), _make_thread("PRRT_2")]

    resolve_calls: list[str] = []

    def fake_resolve(thread_id):
        resolve_calls.append(thread_id)
        return True

    with (
        patch.object(worker, "_provision", return_value=None),
        patch.object(worker, "_triage", return_value="triage notes"),
        patch.object(worker, "_do", return_value=None),
        patch.object(
            worker,
            "_check",
            return_value=(Verdict.PASS, "pass", None, "VERDICT: PASS", False),
        ),
        patch.object(worker, "_commit_and_push_response", return_value=_ok_commit()),
        patch("quikode.worker.github_graphql.resolve_thread", side_effect=fake_resolve),
        patch("quikode.worker.docker_env.teardown"),
    ):
        outcome = worker.run_review_response(threads)

    assert outcome.final_state == State.AWAITING_MERGE
    # All threads resolved.
    assert sorted(resolve_calls) == ["PRRT_1", "PRRT_2"]
    # review_round incremented.
    row = worker.store.get("R-001")
    assert row["review_round"] == 1
    # Each thread marked addressed in the table.
    for tid in ["PRRT_1", "PRRT_2"]:
        stored = worker.store.get_review_thread("R-001", tid)
        assert stored["addressed_in_commit_sha"] == "deadbeef"
    # Final state: AWAITING_MERGE.
    assert row["state"] == State.AWAITING_MERGE.value
    worker.store.conn.close()


def test_run_review_response_transitions_through_responding(tmp_path):
    """Verify the state log shows AWAITING_MERGE → PROVISIONING → RESPONDING_TO_REVIEW
    → AWAITING_MERGE."""
    worker = _build_worker(tmp_path)
    threads = [_make_thread()]

    with (
        patch.object(worker, "_provision", return_value=None),
        patch.object(worker, "_triage", return_value="notes"),
        patch.object(worker, "_do", return_value=None),
        patch.object(worker, "_check", return_value=(Verdict.PASS, "pass", None, "VERDICT: PASS", False)),
        patch.object(worker, "_commit_and_push_response", return_value=_ok_commit()),
        patch("quikode.worker.github_graphql.resolve_thread", return_value=True),
        patch("quikode.worker.docker_env.teardown"),
    ):
        worker.run_review_response(threads)

    log_states = [
        r["to_state"]
        for r in worker.store.conn.execute(
            "SELECT to_state FROM state_log WHERE task_id = ? ORDER BY ts", ("R-001",)
        ).fetchall()
    ]
    # Must include RESPONDING_TO_REVIEW between AWAITING_MERGE entries.
    assert State.RESPONDING_TO_REVIEW.value in log_states
    # Last entry is AWAITING_MERGE.
    assert log_states[-1] == State.AWAITING_MERGE.value
    worker.store.conn.close()


# ----- failure paths -----


def test_run_review_response_empty_threads_noop(tmp_path):
    worker = _build_worker(tmp_path)
    with patch.object(worker, "_provision") as prov:
        outcome = worker.run_review_response([])
    assert outcome.final_state == State.AWAITING_MERGE
    prov.assert_not_called()
    worker.store.conn.close()


def test_run_review_response_commit_fails_then_redo_then_succeeds(tmp_path):
    """Pre-commit-style failure on first commit attempt → re-do once → second
    commit succeeds. Verifies _do is called twice."""
    worker = _build_worker(tmp_path)
    threads = [_make_thread()]
    do_call_count = [0]

    def fake_do(attempt):
        do_call_count[0] += 1

    commits = [_failed_commit(), _ok_commit()]

    def fake_commit():
        return commits.pop(0)

    with (
        patch.object(worker, "_provision", return_value=None),
        patch.object(worker, "_triage", return_value="notes"),
        patch.object(worker, "_do", side_effect=fake_do),
        patch.object(worker, "_check", return_value=(Verdict.PASS, "pass", None, "VERDICT: PASS", False)),
        patch.object(worker, "_commit_and_push_response", side_effect=fake_commit),
        patch("quikode.worker.github_graphql.resolve_thread", return_value=True),
        patch("quikode.worker.docker_env.teardown"),
    ):
        outcome = worker.run_review_response(threads)

    assert do_call_count[0] == 2
    assert outcome.final_state == State.AWAITING_MERGE
    # Round still incremented since commit eventually succeeded.
    assert worker.store.get("R-001")["review_round"] == 1
    worker.store.conn.close()


def test_run_review_response_commit_fails_twice_returns_awaiting_merge(tmp_path):
    """Both commit attempts fail → don't crash, return to AWAITING_MERGE,
    don't increment review_round (no work landed)."""
    worker = _build_worker(tmp_path)
    threads = [_make_thread()]

    with (
        patch.object(worker, "_provision", return_value=None),
        patch.object(worker, "_triage", return_value="notes"),
        patch.object(worker, "_do", return_value=None),
        patch.object(worker, "_check", return_value=(Verdict.PASS, "pass", None, "VERDICT: PASS", False)),
        patch.object(worker, "_commit_and_push_response", side_effect=[_failed_commit(), _failed_commit()]),
        patch("quikode.worker.github_graphql.resolve_thread") as resolver,
        patch("quikode.worker.docker_env.teardown"),
    ):
        outcome = worker.run_review_response(threads)

    assert outcome.final_state == State.AWAITING_MERGE
    # No threads resolved (we didn't push anything).
    resolver.assert_not_called()
    # Round NOT incremented.
    row = worker.store.get("R-001")
    assert (row["review_round"] or 0) == 0
    worker.store.conn.close()


def test_run_review_response_checker_fail_does_not_block(tmp_path):
    """Checker FAIL on the response cycle should be advisory only — commit
    + push still happens, threads are still resolved, round increments."""
    worker = _build_worker(tmp_path)
    threads = [_make_thread()]

    with (
        patch.object(worker, "_provision", return_value=None),
        patch.object(worker, "_triage", return_value="notes"),
        patch.object(worker, "_do", return_value=None),
        patch.object(
            worker,
            "_check",
            return_value=(Verdict.FAIL, "fail", "ci excerpt", "VERDICT: FAIL", False),
        ),
        patch.object(worker, "_commit_and_push_response", return_value=_ok_commit()),
        patch("quikode.worker.github_graphql.resolve_thread", return_value=True) as resolver,
        patch("quikode.worker.docker_env.teardown"),
    ):
        outcome = worker.run_review_response(threads)

    assert outcome.final_state == State.AWAITING_MERGE
    # Thread still resolved (checker fail is advisory).
    resolver.assert_called_once_with("PRRT_1")
    # Round still bumped.
    assert worker.store.get("R-001")["review_round"] == 1
    worker.store.conn.close()


def test_run_review_response_resolve_failure_still_marks_addressed(tmp_path):
    """When resolve_thread returns False (or raises), we still record
    addressed_in_commit_sha — the commit DID land; the resolve mutation
    itself can fail without invalidating the cycle."""
    worker = _build_worker(tmp_path)
    threads = [_make_thread()]

    with (
        patch.object(worker, "_provision", return_value=None),
        patch.object(worker, "_triage", return_value="notes"),
        patch.object(worker, "_do", return_value=None),
        patch.object(worker, "_check", return_value=(Verdict.PASS, "pass", None, "VERDICT: PASS", False)),
        patch.object(worker, "_commit_and_push_response", return_value=_ok_commit()),
        patch("quikode.worker.github_graphql.resolve_thread", return_value=False),
        patch("quikode.worker.docker_env.teardown"),
    ):
        outcome = worker.run_review_response(threads)

    assert outcome.final_state == State.AWAITING_MERGE
    stored = worker.store.get_review_thread("R-001", "PRRT_1")
    assert stored["addressed_in_commit_sha"] == "deadbeef"
    worker.store.conn.close()


def test_run_review_response_crash_returns_to_awaiting_merge(tmp_path):
    """Any unexpected exception is caught and the task returns to
    AWAITING_MERGE rather than FAILED — humans can re-comment to retry."""
    worker = _build_worker(tmp_path)
    threads = [_make_thread()]

    with (
        patch.object(worker, "_provision", return_value=None),
        patch.object(worker, "_triage", side_effect=RuntimeError("oops agent died")),
        patch("quikode.worker.docker_env.teardown"),
    ):
        outcome = worker.run_review_response(threads)

    assert outcome.final_state == State.AWAITING_MERGE
    assert "crashed" in outcome.note
    worker.store.conn.close()


def test_run_review_response_tears_down_container(tmp_path):
    """Container teardown runs even on the success path; worktree is
    preserved (no remove_worktree call)."""
    worker = _build_worker(tmp_path)
    threads = [_make_thread()]

    with (
        patch.object(worker, "_provision", return_value=None),
        patch.object(worker, "_triage", return_value="notes"),
        patch.object(worker, "_do", return_value=None),
        patch.object(worker, "_check", return_value=(Verdict.PASS, "pass", None, "VERDICT: PASS", False)),
        patch.object(worker, "_commit_and_push_response", return_value=_ok_commit()),
        patch("quikode.worker.github_graphql.resolve_thread", return_value=True),
        patch("quikode.worker.docker_env.teardown") as td,
        patch("quikode.worker.worktree.remove_worktree") as rm,
    ):
        worker.run_review_response(threads)

    td.assert_called_once()
    rm.assert_not_called()
    worker.store.conn.close()
