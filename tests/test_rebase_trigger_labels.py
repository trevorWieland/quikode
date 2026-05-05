"""Item 5: rebase scheduling carries an accurate trigger label.

State-log notes for REBASING_TO_MAIN transitions name WHY they fired:

* parent_merged — orchestrator detected the parent's PR merged
* sibling_conflict — PR's mergeable flipped to CONFLICTING
* worker_checkpoint_flag — worker's needs_parent_rebase checkpoint
  fired (not used by orchestrator scheduling — covered for completeness
  in the worker-side label tests already in place)
* manual — operator-driven
"""

from __future__ import annotations

import json
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
                "title": nid,
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
            for nid, deps in [("PARENT", []), ("CHILD", ["PARENT"])]
        ],
    }
    p = tmp_path / "dag.json"
    p.write_text(json.dumps(raw))
    return DAG.load(p)


def _orch(tmp_path: Path, **cfg_kw) -> Orchestrator:
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


def _seed(o: Orchestrator, *, child_state: State = State.PENDING_CI, pr_number: int = 11) -> None:
    o.store.upsert_pending("PARENT")
    o.store.transition("PARENT", State.PENDING_CI, branch="quikode/parent-aaa")
    o.store.upsert_pending("CHILD")
    o.store.transition(
        "CHILD",
        child_state,
        branch="quikode/child-bbb",
        pr_number=pr_number,
        pr_url=f"https://github.com/owner/repo/pull/{pr_number}",
    )
    o.store.set_field(
        "CHILD",
        parent_pr_branches='["quikode/parent-aaa"]',
        parent_branches='["quikode/parent-aaa"]',
    )


def _last_log_note(o: Orchestrator, task_id: str) -> str:
    r = o.store.conn.execute(
        "SELECT note FROM state_log WHERE task_id = ? ORDER BY ts DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return str(r["note"] or "") if r else ""


def test_parent_merged_label(tmp_path):
    o = _orch(tmp_path)
    _seed(o)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    o._schedule_rebase_to_main("CHILD", pool, futures, rrf)  # default reason
    note = _last_log_note(o, "CHILD")
    assert "parent merged" in note
    o.store.conn.close()


def test_sibling_conflict_label_via_poll(tmp_path):
    """Driving _poll_review_threads with a CONFLICTING PR for the child
    should schedule the rebase with a sibling-conflict label."""
    o = _orch(tmp_path)
    _seed(o, child_state=State.PENDING_CI, pr_number=11)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    conflicting = PRStatus(
        number=11,
        url="https://github.com/owner/repo/pull/11",
        state="OPEN",
        mergeable="CONFLICTING",
        checks_status="success",
        failed_checks=[],
    )
    # Parent: still OPEN, so its poll won't transition the parent to MERGED.
    parent_status = PRStatus(
        number=10,
        url="",
        state="OPEN",
        mergeable="MERGEABLE",
        checks_status="success",
        failed_checks=[],
    )
    # Need pr_number on parent so it's polled, otherwise it's just marked.
    o.store.set_field("PARENT", pr_number=10, pr_url="https://github.com/owner/repo/pull/10")

    def _poll_pr_side(repo, pr_number):
        return parent_status if pr_number == 10 else conflicting

    with (
        patch("quikode.orchestrator.github.poll_pr", side_effect=_poll_pr_side),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[]),
        patch.object(o, "_repo_identifier", return_value="owner/repo"),
    ):
        o._poll_review_threads(pool, futures, rrf)

    note = _last_log_note(o, "CHILD")
    assert "sibling conflict" in note
    assert o.store.get("CHILD")["state"] == State.REBASING_TO_MAIN.value
    o.store.conn.close()


def test_manual_label(tmp_path):
    o = _orch(tmp_path)
    _seed(o)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    o._schedule_rebase_to_main("CHILD", pool, futures, rrf, trigger_reason="manual")
    note = _last_log_note(o, "CHILD")
    assert "manual" in note
    o.store.conn.close()


def test_unknown_label_passthrough(tmp_path):
    """Unknown reason values pass through verbatim — no crash."""
    o = _orch(tmp_path)
    _seed(o)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    o._schedule_rebase_to_main("CHILD", pool, futures, rrf, trigger_reason="future_reason")
    note = _last_log_note(o, "CHILD")
    assert "future_reason" in note
    o.store.conn.close()
