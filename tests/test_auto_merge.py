"""Item 2: opt-in auto-merge for clean PENDING_CI tasks.

Daemon polls PENDING_CI PRs. When `cfg.auto_merge_when_clean` is True
and the PR is OPEN+MERGEABLE+success-checks+threads-all-resolved AND
the task has been parked >= `cfg.auto_merge_min_age_s`, the daemon
issues `gh pr merge --squash --delete-branch`.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock, patch

from quikode.config import Config
from quikode.dag import DAG
from quikode.github import PRStatus
from quikode.github_graphql import ReviewThread
from quikode.orchestrator import Orchestrator
from quikode.state import State, Store


def _make_dag(tmp_path: Path) -> DAG:
    raw = {
        "schema": "test",
        "milestones": [{"id": "M-1", "title": "x", "goal": "x", "status": "planned"}],
        "nodes": [
            {
                "id": "T",
                "kind": "behavior",
                "milestone": "M-1",
                "title": "T",
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


def _seed(o: Orchestrator, *, pr_number: int = 11, state_age_s: float = 3600.0) -> None:
    o.store.upsert_pending("T")
    o.store.transition(
        "T",
        State.MERGE_READY,
        branch="quikode/t-aaa",
        pr_number=pr_number,
        pr_url=f"https://github.com/owner/repo/pull/{pr_number}",
    )
    # Backdate the state_log row so the age check passes/fails as desired.
    o.store.conn.execute(
        "UPDATE state_log SET ts = ? WHERE task_id = ? AND to_state = ?",
        (time.time() - state_age_s, "T", State.MERGE_READY.value),
    )


def _pr(state="OPEN", mergeable="MERGEABLE", checks="success") -> PRStatus:
    return PRStatus(
        number=11,
        url="https://github.com/owner/repo/pull/11",
        state=state,
        mergeable=mergeable,
        checks_status=checks,
        failed_checks=[],
    )


def _thread(resolved: bool = True) -> ReviewThread:
    return ReviewThread(
        thread_id="T_abc",
        is_resolved=resolved,
        is_outdated=False,
        path="x.py",
        line=1,
        last_comment_id="C_1",
        last_comment_author="reviewer",
        last_comment_body="ok",
        last_comment_created_at=time.time() - 600,
        last_comment_is_bot=False,
    )


def _drive_poll(o: Orchestrator) -> None:
    pool: MagicMock = MagicMock()

    def _submit(fn, *args, **kwargs):
        f: Future = Future()
        f.set_result(None)
        return f

    pool.submit.side_effect = _submit
    o._poll_review_threads(pool, {}, set())


def test_auto_merges_when_clean_and_opted_in(tmp_path):
    o = _orch(tmp_path, auto_merge_when_clean=True, auto_merge_min_age_s=0)
    _seed(o)
    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=_pr()),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[_thread(True)]),
        patch.object(o, "_repo_identifier", return_value="owner/repo"),
        patch("quikode.orchestrator.subprocess.run") as run_mock,
    ):
        run_mock.return_value = MagicMock(returncode=0, stdout="", stderr="")
        _drive_poll(o)

    # gh pr merge invoked
    assert run_mock.called
    args = run_mock.call_args.args[0]
    assert args[:3] == ["gh", "pr", "merge"]
    assert "--squash" in args and "--delete-branch" in args
    # Audit flag set
    assert o.store.get("T")["auto_merged"] == 1
    o.store.conn.close()


def test_no_auto_merge_when_feature_off(tmp_path):
    o = _orch(tmp_path, auto_merge_when_clean=False)
    _seed(o)
    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=_pr()),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[]),
        patch.object(o, "_repo_identifier", return_value="owner/repo"),
        patch("quikode.orchestrator.subprocess.run") as run_mock,
    ):
        run_mock.return_value = MagicMock(returncode=0, stdout="", stderr="")
        _drive_poll(o)

    assert not run_mock.called
    assert (o.store.get("T").get("auto_merged") or 0) == 0
    o.store.conn.close()


def test_no_auto_merge_when_unresolved_thread(tmp_path):
    o = _orch(tmp_path, auto_merge_when_clean=True, auto_merge_min_age_s=0)
    _seed(o)
    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=_pr()),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[_thread(False)]),
        patch.object(o, "_repo_identifier", return_value="owner/repo"),
        patch("quikode.orchestrator.subprocess.run") as run_mock,
    ):
        # Default respond_to_bot_reviews=True + non-bot thread → response
        # cycle would normally fire; we patch the scheduler so it doesn't
        # actually try to spawn a worker.
        with patch.object(o, "_schedule_review_response") as sched:
            run_mock.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _drive_poll(o)
            sched.assert_called_once()
        run_mock.assert_not_called()
        assert (o.store.get("T").get("auto_merged") or 0) == 0
    o.store.conn.close()


def test_no_auto_merge_before_age_threshold(tmp_path):
    o = _orch(tmp_path, auto_merge_when_clean=True, auto_merge_min_age_s=600)
    # Task entered PENDING_CI 30s ago; threshold is 600s → blocked.
    _seed(o, state_age_s=30.0)
    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=_pr()),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[]),
        patch.object(o, "_repo_identifier", return_value="owner/repo"),
        patch("quikode.orchestrator.subprocess.run") as run_mock,
    ):
        run_mock.return_value = MagicMock(returncode=0, stdout="", stderr="")
        _drive_poll(o)

    run_mock.assert_not_called()
    assert (o.store.get("T").get("auto_merged") or 0) == 0
    o.store.conn.close()


def test_no_auto_merge_when_failing_checks(tmp_path):
    o = _orch(tmp_path, auto_merge_when_clean=True, auto_merge_min_age_s=0)
    _seed(o)
    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=_pr(checks="failure")),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[]),
        patch.object(o, "_repo_identifier", return_value="owner/repo"),
        patch("quikode.orchestrator.subprocess.run") as run_mock,
    ):
        run_mock.return_value = MagicMock(returncode=0, stdout="", stderr="")
        _drive_poll(o)

    run_mock.assert_not_called()
    assert (o.store.get("T").get("auto_merged") or 0) == 0
    o.store.conn.close()


def test_auto_merge_failure_does_not_crash_or_mark(tmp_path):
    """Transient gh failure: log it, don't mark auto_merged, let next
    tick retry."""
    o = _orch(tmp_path, auto_merge_when_clean=True, auto_merge_min_age_s=0)
    _seed(o)
    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=_pr()),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[]),
        patch.object(o, "_repo_identifier", return_value="owner/repo"),
        patch("quikode.orchestrator.subprocess.run") as run_mock,
    ):
        run_mock.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
        _drive_poll(o)

    assert run_mock.called  # tried
    assert (o.store.get("T").get("auto_merged") or 0) == 0
    o.store.conn.close()
