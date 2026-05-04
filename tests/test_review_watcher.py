"""v3 Phase B daemon review-watcher pass.

Tests the orchestrator's `_poll_review_threads` and supporting helpers in
isolation: with stubbed `gh pr view` and `gh api graphql` calls and a stub
worker pool, drive the watcher tick and assert state transitions, response
scheduling, throttling, and addressed-thread bookkeeping.
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

# ----- fixtures -----


def _make_dag(tmp_path: Path) -> DAG:
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


def _seed_awaiting_merge(
    o: Orchestrator,
    *,
    task_id: str = "R-001",
    pr_number: int = 42,
    pr_url: str = "https://github.com/owner/repo/pull/42",
) -> None:
    o.store.upsert_pending(task_id)
    o.store.transition(task_id, State.AWAITING_MERGE, pr_number=pr_number, pr_url=pr_url)


def _make_thread(
    *,
    thread_id: str = "PRRT_1",
    is_resolved: bool = False,
    last_comment_author: str = "alice",
    last_comment_is_bot: bool = False,
    last_comment_created_at: float | None = None,
    body: str = "Please rename this",
    path: str | None = "src/foo.py",
    line: int | None = 42,
) -> ReviewThread:
    return ReviewThread(
        thread_id=thread_id,
        is_resolved=is_resolved,
        is_outdated=False,
        path=path,
        line=line,
        last_comment_id=f"PRC_{thread_id}",
        last_comment_author=last_comment_author,
        last_comment_body=body,
        last_comment_created_at=last_comment_created_at
        if last_comment_created_at is not None
        else time.time(),
        last_comment_is_bot=last_comment_is_bot,
    )


def _open_pr_status() -> PRStatus:
    return PRStatus(
        number=42,
        url="https://github.com/owner/repo/pull/42",
        state="OPEN",
        mergeable="MERGEABLE",
        checks_status="success",
        failed_checks=[],
    )


def _ci_failed_pr_status() -> PRStatus:
    return PRStatus(
        number=42,
        url="https://github.com/owner/repo/pull/42",
        state="OPEN",
        mergeable="MERGEABLE",
        checks_status="failure",
        failed_checks=[
            {"name": "just ci", "conclusion": "FAILURE"},
            {"name": "web checks", "conclusion": "FAILURE"},
        ],
    )


# ----- _classify_threads -----


def test_classify_new_unresolved_human_thread_addressed(tmp_path):
    o = _orch(tmp_path)
    _seed_awaiting_merge(o)
    thread = _make_thread()
    to_address = o._classify_threads("R-001", [thread])
    assert len(to_address) == 1
    assert to_address[0].thread_id == "PRRT_1"
    # Stored row exists now.
    stored = o.store.get_review_thread("R-001", "PRRT_1")
    assert stored is not None
    assert stored["is_resolved"] == 0
    o.store.conn.close()


def test_classify_resolved_thread_skipped(tmp_path):
    o = _orch(tmp_path)
    _seed_awaiting_merge(o)
    thread = _make_thread(is_resolved=True)
    to_address = o._classify_threads("R-001", [thread])
    assert to_address == []
    # Still stored (so future state changes are tracked).
    stored = o.store.get_review_thread("R-001", "PRRT_1")
    assert stored["is_resolved"] == 1
    o.store.conn.close()


def test_classify_bot_thread_skipped_by_default_off(tmp_path):
    o = _orch(tmp_path, respond_to_bot_reviews=False)
    _seed_awaiting_merge(o)
    thread = _make_thread(last_comment_author="dependabot[bot]", last_comment_is_bot=True)
    to_address = o._classify_threads("R-001", [thread])
    assert to_address == []
    o.store.conn.close()


def test_classify_bot_thread_addressed_when_flag_on(tmp_path):
    o = _orch(tmp_path, respond_to_bot_reviews=True)
    _seed_awaiting_merge(o)
    thread = _make_thread(last_comment_author="chatgpt-codex-connector", last_comment_is_bot=True)
    to_address = o._classify_threads("R-001", [thread])
    assert len(to_address) == 1
    o.store.conn.close()


def test_classify_already_addressed_no_new_comment_skipped(tmp_path):
    """Thread with addressed_in_commit_sha set and last_comment_ts unchanged
    is a thread we already responded to — don't re-respond."""
    o = _orch(tmp_path)
    _seed_awaiting_merge(o)
    # Pre-seed: the thread was already addressed at last_comment_ts=1000.0.
    o.store.upsert_review_thread(
        "R-001",
        thread_id="PRRT_1",
        is_resolved=False,
        last_comment_ts=1000.0,
        last_comment_author="alice",
        last_comment_is_bot=False,
    )
    o.store.mark_thread_addressed("R-001", "PRRT_1", "abc123")
    # Live state matches: same last_comment_ts.
    thread = _make_thread(last_comment_created_at=1000.0)
    to_address = o._classify_threads("R-001", [thread])
    assert to_address == []
    # The addressed marker survives the upsert.
    stored = o.store.get_review_thread("R-001", "PRRT_1")
    assert stored["addressed_in_commit_sha"] == "abc123"
    o.store.conn.close()


def test_classify_already_addressed_new_comment_addressed_again(tmp_path):
    """A new human comment on a previously-addressed thread → respond again."""
    o = _orch(tmp_path)
    _seed_awaiting_merge(o)
    o.store.upsert_review_thread(
        "R-001",
        thread_id="PRRT_1",
        is_resolved=False,
        last_comment_ts=1000.0,
        last_comment_author="alice",
        last_comment_is_bot=False,
    )
    o.store.mark_thread_addressed("R-001", "PRRT_1", "abc123")
    # Live state: a NEW comment landed at 2000.0.
    thread = _make_thread(last_comment_created_at=2000.0)
    to_address = o._classify_threads("R-001", [thread])
    assert len(to_address) == 1
    o.store.conn.close()


# ----- _poll_review_threads (full pass) -----


def _make_pool() -> MagicMock:
    """A pool whose submit returns a finished future."""
    pool = MagicMock()

    def _submit(fn, *args, **kwargs):
        f: Future = Future()
        f.set_result(None)
        return f

    pool.submit.side_effect = _submit
    return pool


def test_poll_first_observation_unresolved_thread_schedules_response(tmp_path):
    o = _orch(tmp_path, max_parallel=3)
    _seed_awaiting_merge(o)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    thread = _make_thread()
    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=_open_pr_status()),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[thread]),
    ):
        o._poll_review_threads(pool, futures, rrf)

    # Submitted exactly one future for R-001.
    assert "R-001" in futures
    assert "R-001" in rrf
    pool.submit.assert_called_once()
    submitted_fn = pool.submit.call_args[0][0]
    submitted_args = pool.submit.call_args[0][1:]
    assert submitted_fn == o._run_review_response_one
    assert submitted_args[0] == "R-001"
    threads_arg = submitted_args[1]
    assert len(threads_arg) == 1 and threads_arg[0].thread_id == "PRRT_1"
    # Task transitioned to RESPONDING_TO_REVIEW synchronously.
    assert o.store.get("R-001")["state"] == State.RESPONDING_TO_REVIEW.value
    o.store.conn.close()


def test_poll_throttles_within_interval(tmp_path):
    """A second call within `review_poll_interval_s` should not re-poll."""
    o = _orch(tmp_path, review_poll_interval_s=60)
    _seed_awaiting_merge(o)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    poll_pr_mock = MagicMock(return_value=_open_pr_status())
    threads_mock = MagicMock(return_value=[])
    with (
        patch("quikode.orchestrator.github.poll_pr", poll_pr_mock),
        patch("quikode.orchestrator.github_graphql.get_review_threads", threads_mock),
    ):
        o._poll_review_threads(pool, futures, rrf)
        # Second call immediately after — last_review_poll_ts is fresh.
        o._poll_review_threads(pool, futures, rrf)

    # poll_pr only called once (the second tick filters R-001 out).
    assert poll_pr_mock.call_count == 1
    o.store.conn.close()


def test_poll_pr_merged_transitions_to_merged(tmp_path):
    o = _orch(tmp_path)
    _seed_awaiting_merge(o)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    merged_status = PRStatus(
        number=42,
        url="https://github.com/owner/repo/pull/42",
        state="MERGED",
        mergeable="MERGEABLE",
        checks_status="success",
        failed_checks=[],
    )
    threads_mock = MagicMock()
    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=merged_status),
        patch("quikode.orchestrator.github_graphql.get_review_threads", threads_mock),
    ):
        o._poll_review_threads(pool, futures, rrf)

    assert o.store.get("R-001")["state"] == State.MERGED.value
    # No graphql call needed once the PR is merged.
    threads_mock.assert_not_called()
    pool.submit.assert_not_called()
    o.store.conn.close()


def test_poll_pr_closed_transitions_to_aborted(tmp_path):
    o = _orch(tmp_path)
    _seed_awaiting_merge(o)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    closed_status = PRStatus(
        number=42,
        url="https://github.com/owner/repo/pull/42",
        state="CLOSED",
        mergeable="UNKNOWN",
        checks_status="none",
        failed_checks=[],
    )
    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=closed_status),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[]),
    ):
        o._poll_review_threads(pool, futures, rrf)

    assert o.store.get("R-001")["state"] == State.ABORTED.value
    pool.submit.assert_not_called()
    o.store.conn.close()


def test_poll_dispatches_ci_fix_on_post_merge_failure(tmp_path):
    """Live regression on R-0002: GitHub CI flipped to FAILURE while the
    task was AWAITING_MERGE (response push triggered CI re-run, CI failed).
    The daemon's review-watcher must detect this + dispatch a ci-fix
    cycle. Without this, the task sits at AWAITING_MERGE indefinitely."""
    o = _orch(tmp_path, max_parallel=3)
    o.cfg.review_response_extra_slots = 1
    _seed_awaiting_merge(o)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=_ci_failed_pr_status()),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[]),
    ):
        o._poll_review_threads(pool, futures, rrf)

    pool.submit.assert_called_once()
    # Submitted via the ci-fix path, not the review-response path.
    args = pool.submit.call_args
    assert args[0][0] == o._run_ci_fix_response_one
    # Task transitioned RESPONDING_TO_REVIEW with a CI-fix note.
    row = o.store.get("R-001")
    assert row["state"] == State.RESPONDING_TO_REVIEW.value
    assert "R-001" in rrf
    o.store.conn.close()


def test_poll_does_not_dispatch_ci_fix_when_pool_full(tmp_path):
    """CI-fix uses the same pool budget as review responses
    (max_parallel + review_response_extra_slots). At cap, defer."""
    o = _orch(tmp_path, max_parallel=1)
    o.cfg.review_response_extra_slots = 1
    _seed_awaiting_merge(o)
    pool = _make_pool()
    futures: dict[str, Future] = {"OTHER-1": Future(), "OTHER-2": Future()}
    rrf: set[str] = set()

    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=_ci_failed_pr_status()),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[]),
    ):
        o._poll_review_threads(pool, futures, rrf)

    pool.submit.assert_not_called()
    assert o.store.get("R-001")["state"] == State.AWAITING_MERGE.value
    o.store.conn.close()


def test_poll_does_not_dispatch_ci_fix_when_no_failed_checks(tmp_path):
    """checks_status='failure' but failed_checks=[] is a stale signal —
    don't dispatch. Only fire when we have concrete check rows to feed
    the fixup planner."""
    o = _orch(tmp_path, max_parallel=3)
    _seed_awaiting_merge(o)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    weird_status = PRStatus(
        number=42,
        url="https://github.com/owner/repo/pull/42",
        state="OPEN",
        mergeable="MERGEABLE",
        checks_status="failure",
        failed_checks=[],
    )
    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=weird_status),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[]),
    ):
        o._poll_review_threads(pool, futures, rrf)

    pool.submit.assert_not_called()
    o.store.conn.close()


def test_poll_skips_when_pool_full(tmp_path):
    """Unresolved threads but pool is at the hard cap (max_parallel +
    review_response_extra_slots) → no submit, log only. Re-tries next tick
    once slack reopens."""
    o = _orch(tmp_path, max_parallel=1)
    o.cfg.review_response_extra_slots = 1  # default; explicit for clarity
    _seed_awaiting_merge(o)
    pool = _make_pool()
    # Pool saturated past the review cap (max_parallel=1 + extra=1 = 2; here 2 in-flight).
    futures: dict[str, Future] = {"OTHER-1": Future(), "OTHER-2": Future()}
    rrf: set[str] = set()

    thread = _make_thread()
    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=_open_pr_status()),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[thread]),
    ):
        o._poll_review_threads(pool, futures, rrf)

    # No new future submitted; R-001 stays AWAITING_MERGE.
    pool.submit.assert_not_called()
    assert "R-001" not in rrf
    assert o.store.get("R-001")["state"] == State.AWAITING_MERGE.value
    # But the thread WAS upserted into review_threads (so next tick's
    # already-stored check works correctly).
    stored = o.store.get_review_thread("R-001", "PRRT_1")
    assert stored is not None
    o.store.conn.close()


def test_poll_uses_extra_slots_for_reviews_when_workers_full(tmp_path):
    """Regression for the 2026-05-04 PR #143 starvation: with `max_parallel=3`
    and 3 long-running task workers occupying the pool, review responses on
    AWAITING_MERGE PRs were deferred indefinitely. The fix: reviews can
    exceed `max_parallel` by `cfg.review_response_extra_slots` (default 1),
    so a fresh thread on a parked PR dispatches even when regular workers
    saturate the pool."""
    o = _orch(tmp_path, max_parallel=3)
    o.cfg.review_response_extra_slots = 1
    _seed_awaiting_merge(o)
    pool = _make_pool()
    # Three regular task workers occupy the pool — at max_parallel exactly.
    futures: dict[str, Future] = {
        "OTHER-1": Future(),
        "OTHER-2": Future(),
        "OTHER-3": Future(),
    }
    rrf: set[str] = set()

    thread = _make_thread()
    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=_open_pr_status()),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[thread]),
    ):
        o._poll_review_threads(pool, futures, rrf)

    # The extra slot kicked in: pool.submit was called for the review.
    pool.submit.assert_called_once()
    assert "R-001" in rrf
    assert o.store.get("R-001")["state"] == State.RESPONDING_TO_REVIEW.value
    o.store.conn.close()


def test_poll_no_pr_number_marks_polled(tmp_path):
    o = _orch(tmp_path)
    o.store.upsert_pending("R-001")
    o.store.transition("R-001", State.AWAITING_MERGE, note="no diff path")
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    poll_pr_mock = MagicMock()
    with (
        patch("quikode.orchestrator.github.poll_pr", poll_pr_mock),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[]),
    ):
        o._poll_review_threads(pool, futures, rrf)

    poll_pr_mock.assert_not_called()
    pool.submit.assert_not_called()
    # last_review_poll_ts was updated.
    row = o.store.get("R-001")
    assert row["last_review_poll_ts"] is not None
    o.store.conn.close()


def test_poll_skips_task_already_in_futures(tmp_path):
    """A task that already has an in-flight future (e.g. a review response
    that's still running) is skipped to avoid double-submitting."""
    o = _orch(tmp_path)
    _seed_awaiting_merge(o)
    pool = _make_pool()
    pending = Future()  # not yet done
    futures: dict[str, Future] = {"R-001": pending}
    rrf: set[str] = {"R-001"}

    poll_pr_mock = MagicMock()
    with (
        patch("quikode.orchestrator.github.poll_pr", poll_pr_mock),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[]),
    ):
        o._poll_review_threads(pool, futures, rrf)

    poll_pr_mock.assert_not_called()
    pool.submit.assert_not_called()
    o.store.conn.close()


def test_poll_resolved_thread_no_response_scheduled(tmp_path):
    o = _orch(tmp_path)
    _seed_awaiting_merge(o)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    thread = _make_thread(is_resolved=True)
    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=_open_pr_status()),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[thread]),
    ):
        o._poll_review_threads(pool, futures, rrf)

    pool.submit.assert_not_called()
    assert o.store.get("R-001")["state"] == State.AWAITING_MERGE.value
    o.store.conn.close()


def test_poll_bot_only_thread_no_response_when_disabled(tmp_path):
    o = _orch(tmp_path, respond_to_bot_reviews=False)
    _seed_awaiting_merge(o)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    thread = _make_thread(last_comment_author="github-actions[bot]", last_comment_is_bot=True)
    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=_open_pr_status()),
        patch("quikode.orchestrator.github_graphql.get_review_threads", return_value=[thread]),
    ):
        o._poll_review_threads(pool, futures, rrf)

    pool.submit.assert_not_called()
    o.store.conn.close()


# ----- _repo_identifier -----


def test_repo_identifier_from_pr_url(tmp_path):
    o = _orch(tmp_path)
    repo = o._repo_identifier({"pr_url": "https://github.com/octocat/widgets/pull/7"})
    assert repo == "octocat/widgets"
    o.store.conn.close()


def test_repo_identifier_falls_back_to_gh(tmp_path):
    o = _orch(tmp_path)
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = json.dumps({"nameWithOwner": "trevorWieland/quikode"})
    with patch("quikode.orchestrator.subprocess.run", return_value=fake):
        repo = o._repo_identifier({})
    assert repo == "trevorWieland/quikode"
    o.store.conn.close()


# ----- heartbeat -----


def test_heartbeat_writes_payload(tmp_path):
    o = _orch(tmp_path)
    _seed_awaiting_merge(o)
    o._write_heartbeat(in_flight=2, responding_to_review_futures=1)
    hb_path = o.cfg.state_dir / "orchestrator.heartbeat"
    assert hb_path.exists()
    payload = json.loads(hb_path.read_text())
    assert payload["in_flight"] == 2
    assert payload["responding_to_review_futures"] == 1
    assert payload["awaiting_merge"] == 1
    assert "ts" in payload
    o.store.conn.close()


# ----- store helpers -----


def test_tasks_needing_review_poll_filters_correctly(tmp_path):
    o = _orch(tmp_path)
    o.store.upsert_pending("A")
    o.store.upsert_pending("B")
    o.store.upsert_pending("C")
    o.store.transition("A", State.AWAITING_MERGE)
    o.store.transition("B", State.AWAITING_MERGE)
    o.store.transition("C", State.MERGED)
    # B has a recent poll; A has none.
    o.store.set_field("B", last_review_poll_ts=time.time())
    cutoff = time.time() - 10
    rows = o.store.tasks_needing_review_poll(cutoff=cutoff)
    ids = sorted(r["id"] for r in rows)
    # A: never polled → included. B: polled recently → excluded. C: not awaiting → excluded.
    assert ids == ["A"]
    o.store.conn.close()


def test_poll_conflicting_pr_schedules_rebase_to_main(tmp_path):
    """cleanup-5: When a sibling task merges and creates a mergeability
    conflict on this PR, mergeable flips to CONFLICTING. The watcher must
    schedule a rebase-to-main + conflict-resolve cycle. Pre-v3 this lived
    in the worker's _poll_pr_loop, but v3 hands it off to the daemon.
    """
    o = _orch(tmp_path, max_parallel=3)
    _seed_awaiting_merge(o)
    pool = _make_pool()
    futures: dict[str, Future] = {}
    rrf: set[str] = set()

    conflicting = PRStatus(
        number=42,
        url="https://github.com/owner/repo/pull/42",
        state="OPEN",
        mergeable="CONFLICTING",
        checks_status="success",
        failed_checks=[],
    )
    with (
        patch("quikode.orchestrator.github.poll_pr", return_value=conflicting),
        patch("quikode.orchestrator.github_graphql.get_review_threads") as gt_mock,
    ):
        o._poll_review_threads(pool, futures, rrf)

    # The CONFLICTING branch short-circuits — review threads NOT fetched.
    assert gt_mock.call_count == 0
    # A future was submitted (the rebase worker).
    pool.submit.assert_called_once()
    submitted_fn = pool.submit.call_args[0][0]
    assert submitted_fn == o._run_rebase_to_main_one
    assert "R-001" in futures
    # Task transitioned to REBASING_TO_MAIN.
    assert o.store.get("R-001")["state"] == State.REBASING_TO_MAIN.value
    o.store.conn.close()


def test_poll_conflicting_skips_when_already_in_futures(tmp_path):
    """If a task's already mid-rebase, don't double-schedule."""
    o = _orch(tmp_path, max_parallel=3)
    _seed_awaiting_merge(o)
    pool = _make_pool()
    fake_future = MagicMock()
    futures: dict[str, Future] = {"R-001": fake_future}
    rrf: set[str] = set()

    conflicting = PRStatus(
        number=42,
        url="https://github.com/owner/repo/pull/42",
        state="OPEN",
        mergeable="CONFLICTING",
        checks_status="success",
        failed_checks=[],
    )
    with patch("quikode.orchestrator.github.poll_pr", return_value=conflicting):
        o._poll_review_threads(pool, futures, rrf)

    # No new future was submitted — the in-flight one stands.
    pool.submit.assert_not_called()
    # State stays AWAITING_MERGE since the already-running future will handle it.
    assert o.store.get("R-001")["state"] == State.AWAITING_MERGE.value
    o.store.conn.close()


def test_tasks_needing_review_poll_includes_blocked_with_pr(tmp_path):
    """cleanup-2: BLOCKED tasks with a PR should also be polled, so review
    comments posted as the human-intervention path actually fire response
    cycles. BLOCKED tasks WITHOUT a PR (e.g., subtask-loop blocked before
    any PR opened) are skipped — there's nothing to poll."""
    o = _orch(tmp_path)
    o.store.upsert_pending("BLOCKED_WITH_PR")
    o.store.upsert_pending("BLOCKED_NO_PR")
    o.store.upsert_pending("AM_OK")
    o.store.transition("BLOCKED_WITH_PR", State.BLOCKED)
    o.store.set_field("BLOCKED_WITH_PR", pr_number=42)
    o.store.transition("BLOCKED_NO_PR", State.BLOCKED)  # no pr_number
    o.store.transition("AM_OK", State.AWAITING_MERGE)
    o.store.set_field("AM_OK", pr_number=43)

    cutoff = time.time() + 1  # everything's overdue
    rows = o.store.tasks_needing_review_poll(cutoff=cutoff)
    ids = sorted(r["id"] for r in rows)
    assert ids == ["AM_OK", "BLOCKED_WITH_PR"]
    o.store.conn.close()


def test_increment_review_round(tmp_path):
    o = _orch(tmp_path)
    o.store.upsert_pending("R-001")
    assert o.store.increment_review_round("R-001") == 1
    assert o.store.increment_review_round("R-001") == 2
    row = o.store.get("R-001")
    assert row["review_round"] == 2
    o.store.conn.close()


def test_upsert_review_thread_preserves_addressed(tmp_path):
    o = _orch(tmp_path)
    o.store.upsert_review_thread(
        "R-001",
        thread_id="T1",
        is_resolved=False,
        last_comment_ts=1.0,
        last_comment_author="alice",
        last_comment_is_bot=False,
    )
    o.store.mark_thread_addressed("R-001", "T1", "sha-abc")
    # Re-upsert with new state.
    o.store.upsert_review_thread(
        "R-001",
        thread_id="T1",
        is_resolved=False,
        last_comment_ts=2.0,
        last_comment_author="alice",
        last_comment_is_bot=False,
    )
    row = o.store.get_review_thread("R-001", "T1")
    assert row["addressed_in_commit_sha"] == "sha-abc"  # preserved
    assert row["last_comment_ts"] == 2.0  # updated
    o.store.conn.close()


# ----- smoke: interleave normal task scheduling with review responses -----


def test_pick_next_unaffected_by_responding_to_review(tmp_path):
    """RESPONDING_TO_REVIEW is a transient active state — it should not
    look like a "ready" task to _pick_next."""
    o = _orch(tmp_path)
    o.store.upsert_pending("R-001")
    o.store.transition("R-001", State.RESPONDING_TO_REVIEW)
    # _pick_next should not return R-001 (it's already active).
    assert o._pick_next({"R-001"}, set()) is None
    o.store.conn.close()
