"""Plan 55: fresh dev container + bootstrap command per pre-PR audit cycle.

These tests exercise `quikode.workers.audit_bootstrap` in isolation and via
its `prepare_audit_cycle` integration point (called from `pre_pr.py`'s
`_run_pre_pr_pipeline` at the start of each fresh audit cycle, before any
gauntlet stages run).

Coverage:
- Off (`audit_fresh_container=False`) → no re-provision, no bootstrap call.
- On + empty `audit_bootstrap_command` → re-provision happens, no bootstrap.
- On + non-empty bootstrap, rc=0, no diff → re-provision + bootstrap, no commit.
- On + non-empty bootstrap, rc=0, with diff → bootstrap drift auto-committed
  + pushed with `audit-bootstrap: cycle <N>` BEFORE the gauntlet runs.
- Bootstrap rc != 0 → task BLOCKs with `audit_bootstrap_failed` reason.
- Bootstrap commit failure → task BLOCKs with the same reason family.
- Bootstrap push failure → task BLOCKs with the same reason family.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from quikode import github as gh_mod
from quikode.config import Config
from quikode.dag import Node
from quikode.state import State, Store
from quikode.worker import TaskWorker
from quikode.workers import audit_bootstrap as ab_mod


def _node() -> Node:
    return Node(
        id="T-1",
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


def _worker(tmp_path, **cfg_kwargs: Any) -> TaskWorker:
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path, **cfg_kwargs)
    store = Store(tmp_path / "q.db")

    class _DAG:
        def __init__(self) -> None:
            self.nodes = {"T-1": _node()}

    return TaskWorker(cfg, _DAG(), store, _node())


def _stub_provisioned(w: TaskWorker, monkeypatch) -> dict[str, int]:
    """Shared scaffolding: stamp a worktree path / branch, stub the handle,
    and intercept `_provision_container` so re-provision is observable."""
    w.store.upsert_pending("T-1")
    w.store.transition("T-1", State.LOCAL_CI_CHECKING)
    w.store.set_field(
        "T-1",
        branch="quikode/t-1-abc",
        worktree_path=str(w.cfg.worktree_root / "t-1-abc"),
    )
    w.handle = MagicMock(container_name="qk-stub-old")
    counters = {"provision": 0, "teardown": 0}

    def fake_provision_container(self_w, wt_path):
        counters["provision"] += 1
        self_w.handle = MagicMock(container_name=f"qk-stub-new-{counters['provision']}")

    monkeypatch.setattr(TaskWorker, "_provision_container", fake_provision_container)

    def fake_teardown(sandbox):
        counters["teardown"] += 1

    w.execution_backend = MagicMock(teardown=fake_teardown)

    # _existing_worktree_path checks disk; pretend the dir exists.
    monkeypatch.setattr(
        TaskWorker,
        "_existing_worktree_path",
        lambda self_w: w.cfg.worktree_root / "t-1-abc",
    )
    return counters


def test_off_skips_reprovision_and_bootstrap(tmp_path, monkeypatch):
    """`audit_fresh_container=False` → no-op; the existing container stays."""
    w = _worker(tmp_path, audit_fresh_container=False, audit_bootstrap_command="should-not-run")
    counters = _stub_provisioned(w, monkeypatch)
    exec_calls: list[list[str]] = []
    monkeypatch.setattr(
        ab_mod._tw,
        "exec_in",
        lambda h, cmd, log_path=None, timeout=None: exec_calls.append(list(cmd)) or (0, "", ""),
    )

    outcome = ab_mod.prepare_audit_cycle(w, cycle=1)

    assert outcome is None
    assert counters["provision"] == 0
    assert counters["teardown"] == 0
    assert exec_calls == []


def test_on_with_empty_command_reprovisions_only(tmp_path, monkeypatch):
    """`audit_fresh_container=True` + empty command → re-provision; no bootstrap."""
    w = _worker(tmp_path, audit_fresh_container=True, audit_bootstrap_command="")
    counters = _stub_provisioned(w, monkeypatch)
    exec_calls: list[list[str]] = []
    monkeypatch.setattr(
        ab_mod._tw,
        "exec_in",
        lambda h, cmd, log_path=None, timeout=None: exec_calls.append(list(cmd)) or (0, "", ""),
    )

    outcome = ab_mod.prepare_audit_cycle(w, cycle=1)

    assert outcome is None
    assert counters["teardown"] == 1
    assert counters["provision"] == 1
    assert exec_calls == []  # bootstrap step is skipped on empty command


def test_on_with_command_no_diff_reprovisions_and_runs(tmp_path, monkeypatch):
    """Bootstrap rc=0 + clean worktree → no auto-commit; audit proceeds."""
    w = _worker(
        tmp_path,
        audit_fresh_container=True,
        audit_bootstrap_command="pnpm install --frozen-lockfile",
    )
    counters = _stub_provisioned(w, monkeypatch)
    exec_calls: list[list[str]] = []

    def fake_exec(h, cmd, log_path=None, timeout=None):
        exec_calls.append(list(cmd))
        return 0, "ok", ""

    monkeypatch.setattr(ab_mod._tw, "exec_in", fake_exec)
    git_calls: list[list[str]] = []

    def fake_git(self_w, args):
        git_calls.append(list(args))
        if args[0] == "status":
            return 0, ""  # clean worktree
        return 0, ""

    monkeypatch.setattr(TaskWorker, "_git_in_workspace", fake_git)

    push_called: list[Any] = []
    monkeypatch.setattr(gh_mod, "push", lambda *a, **kw: push_called.append((a, kw)) or (0, ""))

    outcome = ab_mod.prepare_audit_cycle(w, cycle=2)

    assert outcome is None
    assert counters["provision"] == 1
    # Bootstrap ran inside the new container.
    assert any("pnpm install --frozen-lockfile" in " ".join(c) for c in exec_calls)
    # Status was checked; no commit/push since clean.
    assert any(c[0] == "status" for c in git_calls)
    assert all(c[0] != "commit" for c in git_calls)
    assert push_called == []


def test_on_with_command_with_diff_auto_commits_and_pushes(tmp_path, monkeypatch):
    """Bootstrap rc=0 + dirty worktree → auto-commit + push as `audit-bootstrap: cycle N`."""
    w = _worker(
        tmp_path,
        audit_fresh_container=True,
        audit_bootstrap_command="just regenerate-all",
    )
    counters = _stub_provisioned(w, monkeypatch)

    monkeypatch.setattr(ab_mod._tw, "exec_in", lambda *a, **kw: (0, "regenerated 3 files", ""))

    git_calls: list[list[str]] = []

    def fake_git(self_w, args):
        git_calls.append(list(args))
        if args[0] == "status":
            return 0, " M packages/contracts/src/generated.ts\n"
        if args[0] == "add":
            return 0, ""
        if args[0] == "commit":
            return 0, "[branch abc1234] audit-bootstrap: cycle 3"
        return 0, ""

    monkeypatch.setattr(TaskWorker, "_git_in_workspace", fake_git)

    push_calls: list[Any] = []

    def fake_push(h, branch, remote="origin", log_path=None):
        push_calls.append((branch, remote))
        return 0, ""

    monkeypatch.setattr(gh_mod, "push", fake_push)

    outcome = ab_mod.prepare_audit_cycle(w, cycle=3)

    assert outcome is None
    assert counters["provision"] == 1
    # Status, add, commit, in that order; push fired with the row's branch.
    kinds = [c[0] for c in git_calls]
    assert kinds[:3] == ["status", "add", "commit"]
    commit_msg = next(c for c in git_calls if c[0] == "commit")
    assert commit_msg == ["commit", "-m", "audit-bootstrap: cycle 3"]
    assert push_calls == [("quikode/t-1-abc", "origin")]


def test_bootstrap_rc_nonzero_blocks_with_distinct_reason(tmp_path, monkeypatch):
    """Bootstrap rc != 0 → task BLOCKED with `audit_bootstrap_failed` reason."""
    w = _worker(
        tmp_path,
        audit_fresh_container=True,
        audit_bootstrap_command="just regenerate-all",
    )
    _stub_provisioned(w, monkeypatch)

    monkeypatch.setattr(
        ab_mod._tw,
        "exec_in",
        lambda *a, **kw: (1, "compile error", "ts(2304): cannot find name 'Foo'"),
    )
    monkeypatch.setattr(TaskWorker, "_git_in_workspace", lambda self_w, args: (0, ""))

    outcome = ab_mod.prepare_audit_cycle(w, cycle=1)

    assert outcome is not None
    assert outcome.final_state == State.BLOCKED
    assert "audit_bootstrap_failed" in outcome.note
    assert "cycle 1" in outcome.note
    row = w._row()
    assert row["state"] == State.BLOCKED.value


def test_bootstrap_commit_failure_blocks(tmp_path, monkeypatch):
    """Bootstrap produced drift but `git commit` failed → BLOCKED."""
    w = _worker(
        tmp_path,
        audit_fresh_container=True,
        audit_bootstrap_command="just regenerate-all",
    )
    _stub_provisioned(w, monkeypatch)

    monkeypatch.setattr(ab_mod._tw, "exec_in", lambda *a, **kw: (0, "", ""))

    def fake_git(self_w, args):
        if args[0] == "status":
            return 0, " M file.ts\n"
        if args[0] == "add":
            return 0, ""
        if args[0] == "commit":
            return 1, "pre-commit hook failed: lint errors"
        return 0, ""

    monkeypatch.setattr(TaskWorker, "_git_in_workspace", fake_git)

    outcome = ab_mod.prepare_audit_cycle(w, cycle=4)

    assert outcome is not None
    assert outcome.final_state == State.BLOCKED
    assert "audit_bootstrap_failed" in outcome.note


def test_bootstrap_push_failure_blocks(tmp_path, monkeypatch):
    """Bootstrap drift committed but `git push` failed → BLOCKED."""
    w = _worker(
        tmp_path,
        audit_fresh_container=True,
        audit_bootstrap_command="just regenerate-all",
    )
    _stub_provisioned(w, monkeypatch)

    monkeypatch.setattr(ab_mod._tw, "exec_in", lambda *a, **kw: (0, "", ""))

    def fake_git(self_w, args):
        if args[0] == "status":
            return 0, " M file.ts\n"
        return 0, ""

    monkeypatch.setattr(TaskWorker, "_git_in_workspace", fake_git)

    monkeypatch.setattr(
        gh_mod,
        "push",
        lambda h, branch, remote="origin", log_path=None: (1, "remote rejected: stale ref"),
    )

    outcome = ab_mod.prepare_audit_cycle(w, cycle=2)

    assert outcome is not None
    assert outcome.final_state == State.BLOCKED
    assert "audit_bootstrap_failed" in outcome.note
