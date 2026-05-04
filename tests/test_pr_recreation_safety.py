"""Item 4: PR-recreation safety net.

Worker.run_rebase_to_main retargets the PR base to main after rebasing.
If retarget fails, we must distinguish:

* PR is OPEN — transient gh hiccup. Retry once with backoff. Still
  failing → BLOCKED. Do NOT create a duplicate PR.
* PR is CLOSED — github auto-closed it (parent base was deleted). Safe
  to create a fresh PR pointing at main.
* PR is unreachable (gh fails to read state at all) — refuse to create
  a new PR (might be transient), mark BLOCKED for human inspection.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from quikode.config import Config
from quikode.dag import DAG, Node
from quikode.state import State, Store
from quikode.worker import TaskWorker


def _node(task_id: str = "T-CHILD") -> Node:
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


def _worker(tmp_path: Path) -> TaskWorker:
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path)
    store = Store(tmp_path / "q.db")

    class _DAG(DAG):
        def __init__(self):
            self.nodes = {"T-CHILD": _node()}

    return TaskWorker(cfg, _DAG(), store, _node())


def _seed_child(w: TaskWorker, pr_number: int = 11) -> None:
    w.store.upsert_pending("T-CHILD")
    w.store.transition(
        "T-CHILD",
        State.REBASING_TO_MAIN,
        branch="quikode/t-child-abc",
        pr_number=pr_number,
        pr_url=f"https://github.com/owner/repo/pull/{pr_number}",
    )


def test_open_pr_retarget_transient_then_success(tmp_path):
    """First retarget call fails, PR is queried as OPEN, retry succeeds → no new PR."""
    w = _worker(tmp_path)
    _seed_child(w)
    calls = {"retarget": 0}

    def _fake_retarget(pr_number):
        calls["retarget"] += 1
        return calls["retarget"] >= 2  # first call fails, second succeeds

    with (
        patch.object(w, "_retarget_pr_to_main", side_effect=_fake_retarget),
        patch.object(w, "_pr_state", return_value="OPEN"),
        patch.object(w, "_create_new_pr_for_rebased_branch") as create_mock,
        patch("quikode.worker.time.sleep"),
    ):
        w._safe_retarget_or_recreate(11)

    assert calls["retarget"] == 2
    create_mock.assert_not_called()
    # PR number unchanged, not BLOCKED
    row = w.store.get("T-CHILD")
    assert row["pr_number"] == 11
    assert row["state"] == State.REBASING_TO_MAIN.value
    w.store.conn.close()


def test_open_pr_retarget_persistent_failure_blocks(tmp_path):
    """PR is OPEN but retarget fails twice → BLOCKED, no duplicate created."""
    w = _worker(tmp_path)
    _seed_child(w)
    with (
        patch.object(w, "_retarget_pr_to_main", return_value=False),
        patch.object(w, "_pr_state", return_value="OPEN"),
        patch.object(w, "_create_new_pr_for_rebased_branch") as create_mock,
        patch("quikode.worker.time.sleep"),
    ):
        w._safe_retarget_or_recreate(11)

    create_mock.assert_not_called()
    row = w.store.get("T-CHILD")
    assert row["state"] == State.BLOCKED.value
    assert "retarget" in (row.get("last_error") or "")
    w.store.conn.close()


def test_closed_pr_creates_new_pr(tmp_path):
    """Retarget fails, PR state is CLOSED → create fresh PR on main."""
    w = _worker(tmp_path)
    _seed_child(w)
    with (
        patch.object(w, "_retarget_pr_to_main", return_value=False),
        patch.object(w, "_pr_state", return_value="CLOSED"),
        patch.object(
            w,
            "_create_new_pr_for_rebased_branch",
            return_value=("https://github.com/owner/repo/pull/22", 22),
        ),
    ):
        w._safe_retarget_or_recreate(11)

    row = w.store.get("T-CHILD")
    assert row["pr_number"] == 22
    assert row["pr_url"] == "https://github.com/owner/repo/pull/22"
    assert row["state"] == State.REBASING_TO_MAIN.value  # not blocked
    w.store.conn.close()


def test_unreachable_pr_blocks_no_duplicate(tmp_path):
    """gh fails entirely to read PR state → BLOCKED, do NOT create new PR."""
    w = _worker(tmp_path)
    _seed_child(w)
    with (
        patch.object(w, "_retarget_pr_to_main", return_value=False),
        patch.object(w, "_pr_state", return_value=None),
        patch.object(w, "_create_new_pr_for_rebased_branch") as create_mock,
    ):
        w._safe_retarget_or_recreate(11)

    create_mock.assert_not_called()
    row = w.store.get("T-CHILD")
    assert row["state"] == State.BLOCKED.value
    w.store.conn.close()


def test_merged_pr_does_nothing(tmp_path):
    """Defensive: PR is already MERGED. Don't create a new PR, don't BLOCK."""
    w = _worker(tmp_path)
    _seed_child(w)
    with (
        patch.object(w, "_retarget_pr_to_main", return_value=False),
        patch.object(w, "_pr_state", return_value="MERGED"),
        patch.object(w, "_create_new_pr_for_rebased_branch") as create_mock,
    ):
        w._safe_retarget_or_recreate(11)

    create_mock.assert_not_called()
    row = w.store.get("T-CHILD")
    assert row["state"] == State.REBASING_TO_MAIN.value  # untouched
    w.store.conn.close()


def test_pr_state_parses_gh_output(tmp_path):
    """_pr_state should run gh pr view and return the parsed state."""
    w = _worker(tmp_path)
    fake_proc = MagicMock(returncode=0, stdout=json.dumps({"state": "OPEN"}), stderr="")
    with patch("quikode.worker.subprocess.run", return_value=fake_proc):
        assert w._pr_state(11) == "OPEN"
    w.store.conn.close()


def test_pr_state_returns_none_on_failure(tmp_path):
    w = _worker(tmp_path)
    fake_proc = MagicMock(returncode=1, stdout="", stderr="boom")
    with patch("quikode.worker.subprocess.run", return_value=fake_proc):
        assert w._pr_state(11) is None
    w.store.conn.close()
