"""v3 Phase B: TaskWorker.run_review_response.

After v3 fixup decomposition (this session), review responses go through
the fixup planner to break the threads into per-thread mini-subtasks
instead of a monolithic doer call. These tests mock `_run_fixup_round`
directly to assert the surrounding control flow: state transitions, thread
resolution + addressed_in_commit_sha bookkeeping, review_round increment,
empty-thread no-op, fixup-blocked → PENDING_CI fallback, and crash
→ PENDING_CI recovery.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from quikode.config import Config
from quikode.dag import DAG
from quikode.github_graphql import ReviewThread
from quikode.state import State, Store
from quikode.worker import TaskWorker, WorkerOutcome


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
    wt_path = cfg.worktree_root / "r-001"
    wt_path.mkdir(parents=True, exist_ok=True)
    store.transition(
        "R-001",
        State.PENDING_CI,
        branch="quikode/r-001-abc123",
        worktree_path=str(wt_path),
        pr_number=42,
        pr_url="https://github.com/owner/repo/pull/42",
        plan_text="(stub plan text)",
    )
    worker = TaskWorker(cfg, dag, store, dag.nodes["R-001"])
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


# ----- happy path -----


def test_run_review_response_happy_path(tmp_path):
    """All fixup subtasks settle → threads resolved, addressed-sha stamped,
    review_round incremented, task back to PENDING_CI."""
    worker = _build_worker(tmp_path)
    threads = [_make_thread("PRRT_1"), _make_thread("PRRT_2")]
    resolve_calls: list[str] = []

    def fake_resolve(thread_id):
        resolve_calls.append(thread_id)
        return True

    with (
        patch.object(worker, "_provision", return_value=None),
        # Fixup round succeeds — returns None per _run_fixup_round contract.
        patch.object(worker, "_run_fixup_round", return_value=None),
        patch.object(worker, "_latest_commit_sha_on_branch", return_value="deadbeef"),
        patch("quikode.worker.github_graphql.resolve_thread", side_effect=fake_resolve),
        patch("quikode.worker.docker_env.teardown"),
    ):
        outcome = worker.run_review_response(threads)

    assert outcome.final_state == State.PENDING_CI
    assert sorted(resolve_calls) == ["PRRT_1", "PRRT_2"]
    row = worker.store.get("R-001")
    assert row["review_round"] == 1
    for tid in ["PRRT_1", "PRRT_2"]:
        stored = worker.store.get_review_thread("R-001", tid)
        assert stored["addressed_in_commit_sha"] == "deadbeef"
    assert row["state"] == State.PENDING_CI.value
    worker.store.conn.close()


def test_run_review_response_transitions_through_responding(tmp_path):
    """State log shows PENDING_CI → PROVISIONING → ADDRESSING_FEEDBACK
    → PENDING_CI (via fixup-decomposition path)."""
    worker = _build_worker(tmp_path)
    threads = [_make_thread()]
    with (
        patch.object(worker, "_provision", return_value=None),
        patch.object(worker, "_run_fixup_round", return_value=None),
        patch.object(worker, "_latest_commit_sha_on_branch", return_value="deadbeef"),
        patch("quikode.worker.github_graphql.resolve_thread", return_value=True),
        patch("quikode.worker.docker_env.teardown"),
    ):
        worker.run_review_response(threads)

    log_states = [
        r["to_state"]
        for r in worker.store.conn.execute(
            "SELECT to_state FROM state_log WHERE task_id = ? ORDER BY ts",
            ("R-001",),
        ).fetchall()
    ]
    assert State.ADDRESSING_FEEDBACK.value in log_states
    assert log_states[-1] == State.PENDING_CI.value
    worker.store.conn.close()


# ----- failure paths -----


def test_run_review_response_empty_threads_noop(tmp_path):
    worker = _build_worker(tmp_path)
    with patch.object(worker, "_provision") as prov:
        outcome = worker.run_review_response([])
    assert outcome.final_state == State.PENDING_CI
    prov.assert_not_called()
    worker.store.conn.close()


def test_run_review_response_fixup_blocked_returns_to_pending_ci(tmp_path):
    """When the fixup round itself blocks (e.g. a fixup subtask exhausts its
    hard-max attempts), the worker logs the partial progress and returns to
    PENDING_CI — review responses are human-driven, so the operator
    will re-trigger via a fresh thread or a manual retry."""
    worker = _build_worker(tmp_path)
    threads = [_make_thread()]
    blocked_outcome = WorkerOutcome(State.BLOCKED, "fixup subtask blocked: too many attempts")
    with (
        patch.object(worker, "_provision", return_value=None),
        patch.object(worker, "_run_fixup_round", return_value=blocked_outcome),
        patch("quikode.worker.github_graphql.resolve_thread") as resolver,
        patch("quikode.worker.docker_env.teardown"),
    ):
        outcome = worker.run_review_response(threads)

    assert outcome.final_state == State.PENDING_CI
    assert "fixup blocked" in outcome.note.lower()
    # No threads resolved — work didn't complete.
    resolver.assert_not_called()
    # Round NOT incremented.
    row = worker.store.get("R-001")
    assert (row["review_round"] or 0) == 0
    worker.store.conn.close()


def test_run_review_response_resolve_failure_still_marks_addressed(tmp_path):
    """When resolve_thread returns False (or raises), we still record
    addressed_in_commit_sha — the commits DID land; the resolve mutation
    itself failing doesn't invalidate the cycle."""
    worker = _build_worker(tmp_path)
    threads = [_make_thread()]
    with (
        patch.object(worker, "_provision", return_value=None),
        patch.object(worker, "_run_fixup_round", return_value=None),
        patch.object(worker, "_latest_commit_sha_on_branch", return_value="deadbeef"),
        patch("quikode.worker.github_graphql.resolve_thread", return_value=False),
        patch("quikode.worker.docker_env.teardown"),
    ):
        outcome = worker.run_review_response(threads)

    assert outcome.final_state == State.PENDING_CI
    stored = worker.store.get_review_thread("R-001", "PRRT_1")
    assert stored["addressed_in_commit_sha"] == "deadbeef"
    worker.store.conn.close()


def test_run_review_response_crash_returns_to_pending_ci(tmp_path):
    """Any unexpected exception is caught and the task returns to
    PENDING_CI rather than FAILED — humans can re-comment to retry."""
    worker = _build_worker(tmp_path)
    threads = [_make_thread()]
    with (
        patch.object(worker, "_provision", return_value=None),
        patch.object(worker, "_run_fixup_round", side_effect=RuntimeError("oops agent died")),
        patch("quikode.worker.docker_env.teardown"),
    ):
        outcome = worker.run_review_response(threads)

    assert outcome.final_state == State.PENDING_CI
    assert "crashed" in outcome.note
    worker.store.conn.close()


def test_run_review_response_tears_down_container(tmp_path):
    """Container teardown runs even on the success path; worktree is
    preserved (no remove_worktree call)."""
    worker = _build_worker(tmp_path)
    threads = [_make_thread()]
    with (
        patch.object(worker, "_provision", return_value=None),
        patch.object(worker, "_run_fixup_round", return_value=None),
        patch.object(worker, "_latest_commit_sha_on_branch", return_value="deadbeef"),
        patch("quikode.worker.github_graphql.resolve_thread", return_value=True),
        patch("quikode.worker.docker_env.teardown") as td,
        patch("quikode.worker.worktree.remove_worktree") as rm,
    ):
        worker.run_review_response(threads)

    td.assert_called_once()
    rm.assert_not_called()
    worker.store.conn.close()
