"""Plan 56: worker-side auto-detect-merged-via-ancestry coverage.

When the PR poll sees `state=CLOSED, merged=false`, the worker now runs
`git merge-base --is-ancestor <task_branch_tip> origin/main` (in the
task's container) BEFORE entering the existing closed-without-merge
handling. If the branch tip is reachable from origin/main, the task
auto-transitions to MERGED via the same FSM path `qk mark-merged` uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from quikode.config import Config
from quikode.dag import DAG, Node
from quikode.state import State, Store
from quikode.worker import TaskWorker


def _node(task_id: str = "R-AUTO") -> Node:
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


def _worker(tmp_path: Path, **cfg_overrides: Any) -> TaskWorker:
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path, **cfg_overrides)
    store = Store(tmp_path / "q.db")

    class _DAG(DAG):
        def __init__(self) -> None:
            self.nodes = {"R-AUTO": _node()}

    return TaskWorker(cfg, _DAG(), store, _node())


def _seed_pending_ci(w: TaskWorker, pr_number: int = 77, branch: str = "quikode/r-auto-abc") -> None:
    """Seed a task into AWAITING_REVIEW with a recorded branch + PR.

    AWAITING_REVIEW is the realistic state for the release-batch flow:
    the operator pulled the PR into an integration branch + pushed
    main + closed the PR; the daemon's next poll will report
    `state=CLOSED, merged=false`.
    """
    w.store.upsert_pending("R-AUTO")
    w.store.transition(
        "R-AUTO",
        State.PENDING_CI,
        branch=branch,
        pr_number=pr_number,
        pr_url=f"https://github.com/owner/repo/pull/{pr_number}",
    )
    w.store.transition("R-AUTO", State.AWAITING_REVIEW)


class _ClosedStatus:
    """Stub for the `status` value `_handle_closed_polled_pr` reads.

    Only `state` + `base_ref_name` are touched on this code path; mirror
    the real polled-status shape narrowly.
    """

    def __init__(self, base_ref_name: str = "main") -> None:
        self.state = "CLOSED"
        self.base_ref_name = base_ref_name
        self.mergeable = ""
        self.checks_status = "none"


def _install_git_stub(worker: TaskWorker, fake: Any) -> None:
    """Bind a fake `_git_in_workspace` onto a TaskWorker instance.

    The attribute exists on a mixin; writing through `__dict__`
    installs the instance-level override that shadows the bound method
    cleanly. Going through `__dict__` rather than plain attribute
    assignment keeps the type checker quiet on the method-shape
    mismatch without sprinkling per-line suppression comments across
    every test.
    """
    worker.__dict__["_git_in_workspace"] = fake


def _git_stub_factory(calls: list[list[str]], *, is_ancestor_rc: int, branch_tip: str = "deadbeef1234"):
    """Build a `_git_in_workspace` replacement that records every call and
    returns canned answers for `rev-parse` + `merge-base --is-ancestor`.

    Other git invocations (e.g. an opportunistic fetch) succeed with empty
    output so we don't need to enumerate every shape the helper might
    invoke.
    """

    def _git(args: list[str]) -> tuple[int, str]:
        calls.append(list(args))
        if args[:1] == ["rev-parse"]:
            return 0, branch_tip + "\n"
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return is_ancestor_rc, ""
        if args[:1] == ["fetch"]:
            return 0, ""
        return 0, ""

    return _git


def test_closed_pr_with_ancestor_branch_auto_marks_merged(tmp_path):
    """The release-batch happy path: PR closed without merge, branch tip
    reachable from origin/main → task auto-transitions to MERGED."""
    w = _worker(tmp_path)
    _seed_pending_ci(w)
    calls: list[list[str]] = []
    _install_git_stub(w, _git_stub_factory(calls, is_ancestor_rc=0))
    outcome = w._handle_closed_polled_pr(_ClosedStatus())
    assert outcome.final_state == State.MERGED
    row = w.store.get("R-AUTO")
    assert row["state"] == State.MERGED.value
    # The note recorded on the state-log carries the auto-merge attribution.
    log_rows = w.store.conn.execute(
        "SELECT note FROM state_log WHERE task_id = ? AND to_state = ?",
        ("R-AUTO", State.MERGED.value),
    ).fetchall()
    assert any("ancestry" in (r["note"] or "") for r in log_rows)
    # A fetch was issued before the ancestry check.
    assert any(c[:1] == ["fetch"] for c in calls)
    # And the actual ancestry comparison ran with the resolved branch tip.
    assert any(c[:2] == ["merge-base", "--is-ancestor"] for c in calls)
    w.store.conn.close()


def test_closed_pr_with_non_ancestor_branch_falls_through_to_aborted(tmp_path):
    """Commits genuinely abandoned: ancestry check returns rc != 0 →
    existing closed-without-merge handling fires (task → ABORTED)."""
    w = _worker(tmp_path)
    _seed_pending_ci(w)
    calls: list[list[str]] = []
    _install_git_stub(w, _git_stub_factory(calls, is_ancestor_rc=1))

    outcome = w._handle_closed_polled_pr(_ClosedStatus())

    assert outcome.final_state == State.ABORTED
    row = w.store.get("R-AUTO")
    assert row["state"] == State.ABORTED.value
    # Auto-merge did NOT fire.
    log_rows = w.store.conn.execute(
        "SELECT to_state FROM state_log WHERE task_id = ?",
        ("R-AUTO",),
    ).fetchall()
    assert State.MERGED.value not in [r["to_state"] for r in log_rows]
    w.store.conn.close()


def test_closed_pr_with_branch_missing_falls_through_to_aborted(tmp_path):
    """Worktree wiped / branch ref missing locally: rev-parse rc != 0 →
    no auto-merge attempt, existing closed-without-merge handling fires.

    Documents the corner case: a task could land here AFTER an external
    cleanup (rare in the polling flow since the container is alive, but
    the ancestry primitive returns gracefully either way)."""
    w = _worker(tmp_path)
    _seed_pending_ci(w)
    calls: list[list[str]] = []

    def _git_missing_branch(args: list[str]) -> tuple[int, str]:
        calls.append(list(args))
        if args[:1] == ["rev-parse"]:
            return 128, "fatal: ambiguous argument 'quikode/r-auto-abc'"
        return 0, ""

    _install_git_stub(w, _git_missing_branch)

    outcome = w._handle_closed_polled_pr(_ClosedStatus())

    assert outcome.final_state == State.ABORTED
    # `merge-base --is-ancestor` is NEVER called when rev-parse fails — we
    # don't want a stale or empty SHA to accidentally test as ancestor.
    assert not any(c[:2] == ["merge-base", "--is-ancestor"] for c in calls)
    w.store.conn.close()


def test_ancestry_check_skipped_when_flag_disabled(tmp_path):
    """`auto_detect_merged_via_ancestry=False` short-circuits the ancestry
    path entirely — closed-without-merge tasks always abort, no git
    invocation at all. Useful for regulated workflows where every closed
    PR must be operator-acknowledged."""
    w = _worker(tmp_path, auto_detect_merged_via_ancestry=False)
    _seed_pending_ci(w)
    calls: list[list[str]] = []
    _install_git_stub(w, _git_stub_factory(calls, is_ancestor_rc=0))

    outcome = w._handle_closed_polled_pr(_ClosedStatus())

    assert outcome.final_state == State.ABORTED
    # No git calls fired — the cfg knob short-circuited before any
    # ancestry plumbing ran.
    assert calls == []
    w.store.conn.close()


def test_handle_polled_terminal_status_routes_closed_through_ancestry(tmp_path):
    """The `_handle_polled_terminal_status` entry point routes CLOSED PRs
    through the new ancestry path (not just the helper). This guards
    against future refactors that bypass `_handle_closed_polled_pr`."""
    w = _worker(tmp_path)
    _seed_pending_ci(w)
    calls: list[list[str]] = []
    _install_git_stub(w, _git_stub_factory(calls, is_ancestor_rc=0))
    # _handle_closed_polled_pr also checks `_remote_branch_exists` for
    # parent-deleted recovery; stub it False so the ancestry path is
    # reached cleanly.
    with patch.object(w, "_remote_branch_exists", return_value=True):
        outcome = w._handle_polled_terminal_status(_ClosedStatus())

    assert outcome is not None
    assert outcome.final_state == State.MERGED
    row = w.store.get("R-AUTO")
    assert row["state"] == State.MERGED.value
    w.store.conn.close()
