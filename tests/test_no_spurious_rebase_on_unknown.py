"""Bug 3 from validation-2026-05-03 findings: spurious rebase from
mergeable=UNKNOWN.

When github reports a freshly-opened PR's mergeable status as UNKNOWN
(while it computes mergeability), neither the orchestrator's review
watcher nor the worker's _poll_pr_loop should treat that as
CONFLICTING. The predicate must be strict equality `== "CONFLICTING"`,
NOT `!= "MERGEABLE"`.

These tests pin the strictness so a regression to the looser predicate
fails fast.
"""

from __future__ import annotations

import json
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock, patch

from quikode.config import Config
from quikode.dag import DAG, Node
from quikode.github import PRStatus
from quikode.orchestrator import Orchestrator
from quikode.state import State, Store
from quikode.worker import TaskWorker, WorkerOutcome

# -------- shared fixtures --------


def _make_dag(tmp_path: Path) -> DAG:
    raw = {
        "schema": "test",
        "milestones": [{"id": "M-1", "title": "x", "goal": "x", "status": "planned"}],
        "nodes": [
            {
                "id": "T-001",
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
            },
        ],
    }
    p = tmp_path / "dag.json"
    p.write_text(json.dumps(raw))
    return DAG.load(p)


def _orch(tmp_path: Path) -> Orchestrator:
    dag = _make_dag(tmp_path)
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.state_dir / "q.db")
    return Orchestrator(cfg, dag, store)


def _make_pool() -> MagicMock:
    pool = MagicMock()

    def _submit(fn, *args, **kwargs):
        f: Future = Future()
        f.set_result(None)
        return f

    pool.submit.side_effect = _submit
    return pool


def _seed_awaiting_merge(o: Orchestrator, pr_number: int = 33) -> None:
    o.store.upsert_pending("T-001")
    o.store.transition(
        "T-001",
        State.PENDING_CI,
        branch="quikode/t-001-abc",
        pr_number=pr_number,
        pr_url=f"https://github.com/owner/repo/pull/{pr_number}",
    )
    # Last poll is old enough to be re-polled this tick.
    o.store.set_field("T-001", last_review_poll_ts=0)


def _pr(state: str, mergeable: str, pr_number: int = 33) -> PRStatus:
    return PRStatus(
        number=pr_number,
        url=f"https://github.com/owner/repo/pull/{pr_number}",
        state=state,
        mergeable=mergeable,
        checks_status="success",
        failed_checks=[],
    )


# -------- orchestrator: _poll_review_threads --------


def test_orchestrator_unknown_mergeable_does_not_schedule_rebase(tmp_path):
    """Github reports mergeable=UNKNOWN (computing) → no rebase scheduled.

    UNKNOWN is the standard transient state for a freshly-opened PR.
    Treating it as CONFLICTING was the Run-1 bug that ate T-001's work.
    """
    o = _orch(tmp_path)
    _seed_awaiting_merge(o)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=_pr("OPEN", "UNKNOWN")),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[]),
        patch.object(o, "_repo_identifier", return_value="owner/repo"),
    ):
        o._poll_review_threads(pool, futures, rrf)

    row = o.store.get("T-001")
    # Still AWAITING_MERGE — no rebase fired.
    assert row["state"] == State.PENDING_CI.value
    assert row["pre_rebase_state"] is None
    o.store.conn.close()


def test_orchestrator_mergeable_does_not_schedule_rebase(tmp_path):
    """mergeable=MERGEABLE → no rebase scheduled."""
    o = _orch(tmp_path)
    _seed_awaiting_merge(o)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=_pr("OPEN", "MERGEABLE")),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[]),
        patch.object(o, "_repo_identifier", return_value="owner/repo"),
    ):
        o._poll_review_threads(pool, futures, rrf)

    row = o.store.get("T-001")
    assert row["state"] == State.PENDING_CI.value
    o.store.conn.close()


def test_orchestrator_conflicting_does_schedule_rebase(tmp_path):
    """Positive case: mergeable=CONFLICTING → rebase IS scheduled."""
    o = _orch(tmp_path)
    _seed_awaiting_merge(o)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=_pr("OPEN", "CONFLICTING")),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[]),
        patch.object(o, "_repo_identifier", return_value="owner/repo"),
    ):
        o._poll_review_threads(pool, futures, rrf)

    row = o.store.get("T-001")
    assert row["state"] == State.REBASING_TO_MAIN.value
    o.store.conn.close()


# -------- worker: _poll_pr_loop predicate --------


def _node(task_id: str = "T-001") -> Node:
    return Node(
        id=task_id,
        kind="behavior",
        milestone="M-1",
        title="x",
        scope="x",
        depends_on=(),
        completes_behaviors=(),
        supports_behaviors=(),
        boundary_with_neighbors="",
        expected_evidence=(),
        playbook=(),
        rationale="",
        risks=(),
        raw={},
    )


def _worker(tmp_path) -> TaskWorker:
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path)
    store = Store(tmp_path / "q.db")

    class _DAG:
        def __init__(self):
            self.nodes = {"T-001": _node()}

    return TaskWorker(cfg, _DAG(), store, _node())


def _worker_with_pr(tmp_path) -> TaskWorker:
    w = _worker(tmp_path)
    w.store.upsert_pending("T-001")
    w.store.transition(
        "T-001",
        State.POLLING_CI,
        branch="quikode/t-001-abc",
        pr_number=33,
        pr_url="https://github.com/owner/repo/pull/33",
    )
    w.handle = MagicMock(container_name="qk-stub")
    return w


def test_worker_poll_pr_loop_does_not_rebase_on_unknown(tmp_path, monkeypatch):
    """Worker `_poll_pr_loop`: github reports mergeable=UNKNOWN → no
    `_rebase_or_resolve()` call. The loop must wait for github's stable
    verdict instead.
    """
    w = _worker_with_pr(tmp_path)

    # Capture whether _rebase_or_resolve was called. Make poll_pr cycle:
    # first call returns UNKNOWN (the spurious case), second call returns
    # MERGEABLE (so the loop exits to AWAITING_MERGE and we don't hang).
    poll_calls = [
        PRStatus(33, "url", "OPEN", "UNKNOWN", "success", []),
        PRStatus(33, "url", "OPEN", "MERGEABLE", "success", []),
    ]
    poll_idx = {"i": 0}

    def fake_poll(repo, pr):
        i = poll_idx["i"]
        poll_idx["i"] = min(i + 1, len(poll_calls) - 1)
        return poll_calls[i]

    rebase_called = {"hit": False}

    def fail_if_called(self):
        rebase_called["hit"] = True

    monkeypatch.setattr("quikode.worker.github.poll_pr", fake_poll)
    monkeypatch.setattr(TaskWorker, "_rebase_or_resolve", fail_if_called)
    monkeypatch.setattr("quikode.worker.time.sleep", lambda s: None)
    # Make the parent-rebase / intent-review checkpoints noop.
    monkeypatch.setattr(TaskWorker, "_handle_parent_rebase_if_needed", lambda self: None)
    monkeypatch.setattr(TaskWorker, "_run_intent_review", lambda self: None)

    outcome = w._poll_pr_loop()
    assert outcome.final_state == State.PENDING_CI
    assert rebase_called["hit"] is False, "UNKNOWN must NOT trigger _rebase_or_resolve"


def test_worker_poll_pr_loop_does_not_rebase_on_mergeable(tmp_path, monkeypatch):
    """Worker `_poll_pr_loop`: mergeable=MERGEABLE → exits to
    AWAITING_MERGE with no rebase."""
    w = _worker_with_pr(tmp_path)

    rebase_called = {"hit": False}

    def fail_if_called(self):
        rebase_called["hit"] = True

    monkeypatch.setattr(
        "quikode.worker.github.poll_pr",
        lambda repo, pr: PRStatus(33, "url", "OPEN", "MERGEABLE", "success", []),
    )
    monkeypatch.setattr(TaskWorker, "_rebase_or_resolve", fail_if_called)
    monkeypatch.setattr("quikode.worker.time.sleep", lambda s: None)
    monkeypatch.setattr(TaskWorker, "_handle_parent_rebase_if_needed", lambda self: None)
    monkeypatch.setattr(TaskWorker, "_run_intent_review", lambda self: None)

    outcome = w._poll_pr_loop()
    assert outcome.final_state == State.PENDING_CI
    assert rebase_called["hit"] is False


def test_worker_poll_pr_loop_does_rebase_on_conflicting(tmp_path, monkeypatch):
    """Worker `_poll_pr_loop`: mergeable=CONFLICTING → _rebase_or_resolve
    IS called. Positive case to ensure we didn't accidentally make the
    predicate too strict."""
    w = _worker_with_pr(tmp_path)

    rebase_called = {"hit": False}

    def fake_rebase(self):
        rebase_called["hit"] = True
        # Return BLOCKED outcome to terminate the loop deterministically.
        return WorkerOutcome(State.BLOCKED, "stub")

    monkeypatch.setattr(
        "quikode.worker.github.poll_pr",
        lambda repo, pr: PRStatus(33, "url", "OPEN", "CONFLICTING", "success", []),
    )
    monkeypatch.setattr(TaskWorker, "_rebase_or_resolve", fake_rebase)
    monkeypatch.setattr("quikode.worker.time.sleep", lambda s: None)
    monkeypatch.setattr(TaskWorker, "_handle_parent_rebase_if_needed", lambda self: None)
    monkeypatch.setattr(TaskWorker, "_run_intent_review", lambda self: None)

    outcome = w._poll_pr_loop()
    assert outcome.final_state == State.BLOCKED
    assert rebase_called["hit"] is True
