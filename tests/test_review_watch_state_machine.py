from __future__ import annotations

import json
import time
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock, patch

from quikode.config import Config
from quikode.dag import DAG
from quikode.github import PRStatus
from quikode.orchestrator import Orchestrator
from quikode.state import State, Store


def _make_dag(tmp_path: Path) -> DAG:
    raw = {
        "schema": "test",
        "milestones": [{"id": "M-1", "title": "x", "goal": "x", "status": "planned"}],
        "nodes": [
            {
                "id": nid,
                "kind": "behavior",
                "milestone": "M-1",
                "title": title,
                "scope": "x",
                "depends_on": deps,
                "completes_behaviors": [],
                "supports_behaviors": [],
                "boundary_with_neighbors": "",
                "expected_evidence": [],
                "playbook": [],
                "rationale": "",
                "risks": [],
            }
            for nid, title, deps in [
                ("PARENT", "Parent work", []),
                ("CHILD", "Child work", ["PARENT"]),
            ]
        ],
    }
    p = tmp_path / "dag.json"
    p.write_text(json.dumps(raw))
    return DAG.load(p)


def _orch(tmp_path: Path, **cfg_kw: object) -> Orchestrator:
    dag = _make_dag(tmp_path)
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        log_dir=tmp_path / ".quikode" / "logs",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        sccache_dir=tmp_path / ".quikode" / "sccache",
        **cfg_kw,
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


def _seed_awaiting_review(o: Orchestrator, task_id: str = "PARENT", pr_number: int = 10) -> None:
    o.store.upsert_pending(task_id)
    o.store.transition(
        task_id,
        State.PENDING_CI,
        branch=f"quikode/{task_id.lower()}-aaa",
        pr_number=pr_number,
        pr_url=f"https://github.com/owner/repo/pull/{pr_number}",
    )
    o.store.transition(task_id, State.AWAITING_REVIEW)
    o.store.set_field(task_id, last_review_poll_ts=0)


def _pr(state: str, *, pr_number: int = 10, checks_status: str = "success") -> PRStatus:
    return PRStatus(
        number=pr_number,
        url=f"https://github.com/owner/repo/pull/{pr_number}",
        state=state,
        mergeable="MERGEABLE",
        checks_status=checks_status,
        failed_checks=[],
    )


def test_review_ready_notification_fires_once_after_settle(tmp_path):
    o = _orch(tmp_path, notify_ntfy_topic="quikode-test", review_ready_settle_s=0)
    _seed_awaiting_review(o)
    row = o.store.get("PARENT")
    assert row is not None

    with patch("quikode.orchestration.review_watch.notify.notify_review_ready", return_value=True) as notify:
        o._maybe_notify_review_ready(row)
        o._maybe_notify_review_ready(o.store.get("PARENT"))

    notify.assert_called_once()
    msg = notify.call_args.kwargs["msg"]
    assert msg.task_id == "PARENT"
    assert msg.title == "Parent work"
    assert msg.pr_url == "https://github.com/owner/repo/pull/10"
    assert o.store.get_last_review_ready_notified_ts("PARENT") is not None
    o.store.conn.close()


def test_review_ready_notification_waits_for_settle_window(tmp_path):
    o = _orch(tmp_path, notify_ntfy_topic="quikode-test", review_ready_settle_s=3600)
    _seed_awaiting_review(o)
    row = o.store.get("PARENT")
    assert row is not None

    with patch("quikode.orchestration.review_watch.notify.notify_review_ready", return_value=True) as notify:
        o._maybe_notify_review_ready(row)

    notify.assert_not_called()
    assert o.store.get_last_review_ready_notified_ts("PARENT") is None
    o.store.conn.close()


def test_ci_success_poll_moves_pending_ci_to_awaiting_review(tmp_path):
    o = _orch(tmp_path)
    o.store.upsert_pending("PARENT")
    o.store.transition(
        "PARENT",
        State.PENDING_CI,
        branch="quikode/parent-aaa",
        pr_number=10,
        pr_url="https://github.com/owner/repo/pull/10",
    )
    o.store.set_field("PARENT", last_review_poll_ts=0)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    review_response_futures: set[str] = set()

    with patch("quikode.orchestrator.github.poll_pr", return_value=_pr("OPEN")):
        o._poll_review_threads(pool, futures, review_response_futures)

    row = o.store.get("PARENT")
    assert row["state"] == State.AWAITING_REVIEW.value
    assert o.store.most_recent_awaiting_review_entry_ts("PARENT") is not None
    o.store.conn.close()


def test_merged_pr_poll_marks_db_merged_and_schedules_child_rebase(tmp_path):
    o = _orch(tmp_path)
    _seed_awaiting_review(o)
    o.store.upsert_pending("CHILD")
    o.store.transition(
        "CHILD",
        State.PENDING_CI,
        branch="quikode/child-bbb",
    )
    o.store.set_field(
        "CHILD",
        parent_pr_branches='["quikode/parent-aaa"]',
        parent_branches='["quikode/parent-aaa"]',
        last_review_poll_ts=time.time(),
    )
    pool = _make_pool()
    futures: dict[str, Future] = {}
    review_response_futures: set[str] = set()

    with patch("quikode.orchestrator.github.poll_pr", return_value=_pr("MERGED")):
        o._poll_review_threads(pool, futures, review_response_futures)

    parent = o.store.get("PARENT")
    child = o.store.get("CHILD")
    assert parent["state"] == State.MERGED.value
    assert parent["last_review_poll_ts"] is not None
    assert child["state"] == State.REBASING_TO_MAIN.value
    assert child["pre_rebase_state"] == State.PENDING_CI.value
    assert "CHILD" in futures
    assert "CHILD" in review_response_futures
    o.store.conn.close()
