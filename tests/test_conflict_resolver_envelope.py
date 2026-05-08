"""Plan 38 PR-B.7: conflict-resolver runs on the JsonAgent layer.

The resolver writes files in the worktree; the diff is the evidence.
The `ConflictResolverEnvelope` only carries bookkeeping the worker
branches on — `gave_up: bool` replaces the prior `"GIVE_UP:"`
substring match in stdout.

These tests stub `make_agent("conflict_resolver", cfg)` with a
canned `_StubAgentResult` and verify:
1. `gave_up=True` triggers the BLOCKED give-up branch (rebase --abort
   + `block_current` with the give_up_reason in the note).
2. `gave_up=False` continues — the worker calls `git add -A` and
   `git rebase --continue` against the worktree.
3. A parse failure (`parse_errors` populated) BLOCKs with the parse
   errors surfaced.
4. A transport failure (`rc != 0`) BLOCKs with the rc message.
5. Schema-level: `ConflictResolverEnvelope(gave_up=True, give_up_reason="")`
   raises a pydantic ValidationError.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MethodType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from quikode.agent_schemas import ConflictResolverEnvelope
from quikode.config import Config
from quikode.dag import DAG
from quikode.state import State, Store
from quikode.worker import TaskWorker


@dataclass
class _StubAgentResult:
    structured: Any = None
    rc: int = 0
    transient: bool = False
    duration_s: float = 1.0
    tokens_input: int | None = None
    tokens_output: int | None = None
    cost_usd: float | None = None
    parse_errors: tuple[str, ...] = ()
    raw_text: str | None = None
    stderr_excerpt: str = ""


class _StubAgent:
    def __init__(self, result: _StubAgentResult):
        self.result = result
        self.last_prompt: str | None = None

    def invoke(self, prompt: str, **kwargs: Any) -> _StubAgentResult:
        self.last_prompt = prompt
        return self.result


def _build_dag(tmp_path: Path) -> DAG:
    raw = {
        "schema": "test",
        "milestones": [{"id": "M-1", "title": "x", "goal": "x", "status": "planned"}],
        "nodes": [
            {
                "id": "R-001",
                "kind": "behavior",
                "milestone": "M-1",
                "title": "test node",
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
    dag = _build_dag(tmp_path)
    store = Store(cfg.state_dir / "quikode.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.REBASING_TO_MAIN)
    store.transition("R-001", State.CONFLICT_RESOLVING)
    store.set_field("R-001", branch="quikode/r-001-abc", base_ref_sha="aaa")
    worker = TaskWorker(cfg, dag, store, dag.nodes["R-001"])
    worker.handle = MagicMock()
    worker.handle.container_name = "qk-stub"
    return worker


# ---------- schema-level guard ----------


def test_envelope_requires_give_up_reason_when_gave_up_true() -> None:
    """`gave_up=True` with empty reason fails validation; the BLOCK
    note would otherwise be uninformative to the human triaging it."""
    with pytest.raises(ValidationError):
        ConflictResolverEnvelope(gave_up=True, give_up_reason="")


def test_envelope_accepts_gave_up_false_with_empty_reason() -> None:
    env = ConflictResolverEnvelope(gave_up=False)
    assert env.gave_up is False
    assert env.give_up_reason == ""


def test_envelope_accepts_gave_up_true_with_reason() -> None:
    env = ConflictResolverEnvelope(
        gave_up=True,
        give_up_reason="cross-parent semantic conflict; needs human integration",
    )
    assert env.gave_up is True
    assert "semantic conflict" in env.give_up_reason


# ---------- worker branching on envelope ----------


def _patch_git_helpers(worker: TaskWorker, conflicted_files: list[dict] | None = None):
    """Stub all the git-in-workspace surface a `_resolve_one_conflict_step`
    call touches so we can drive the agent stub without docker / git."""
    files = conflicted_files or [{"path": "foo.rs", "content": "<<<<<<<\nmine\n=======\ntheirs\n>>>>>>>"}]

    def fake_git(self: Any, args: list[str]) -> tuple[int, str]:
        # `diff --name-only --diff-filter=U` returns the conflicted file list
        if args[:3] == ["diff", "--name-only", "--diff-filter=U"]:
            return 0, "\n".join(f["path"] for f in files)
        return 0, ""

    def fake_exec_in(handle, cmd, **kwargs):
        # `cat /workspace/<path>` for conflicted-file content
        joined = " ".join(cmd)
        for f in files:
            if f["path"] in joined:
                return 0, f["content"], ""
        return 0, "", ""

    worker.__dict__["_git_in_workspace"] = MethodType(fake_git, worker)
    return fake_exec_in


def _last_state_log_note(worker: TaskWorker) -> str:
    with worker.store.tx() as c:
        rows = list(
            c.execute(
                "SELECT note FROM state_log WHERE task_id = ? ORDER BY ts DESC LIMIT 1",
                ("R-001",),
            )
        )
    return rows[0][0] if rows else ""


def test_resolver_gave_up_triggers_block(tmp_path) -> None:
    worker = _build_worker(tmp_path)
    fake_exec = _patch_git_helpers(worker)
    envelope = ConflictResolverEnvelope(
        gave_up=True,
        give_up_reason="task intent and main both reshape the same contract",
        summary="cannot merge",
        files_touched=["foo.rs"],
    )
    stub = _StubAgent(_StubAgentResult(structured=envelope, rc=0))
    with (
        patch("quikode.workers.rebase_conflicts.make_agent", return_value=stub),
        patch("quikode.workers.task_worker.exec_in", side_effect=fake_exec),
    ):
        outcome = worker._resolve_one_conflict_step(iteration=1)
    assert outcome is not None
    assert outcome.final_state == State.BLOCKED
    note = _last_state_log_note(worker)
    assert "gave up" in note.lower()
    assert "reshape the same contract" in note


def test_resolver_clean_envelope_continues(tmp_path) -> None:
    """With `gave_up=False`, the worker proceeds to add+continue. Returning
    None from `_resolve_one_conflict_step` is the "continue iterating" signal."""
    worker = _build_worker(tmp_path)
    fake_exec = _patch_git_helpers(worker)
    git_calls: list[list[str]] = []

    def fake_git(self: Any, args: list[str]) -> tuple[int, str]:
        git_calls.append(list(args))
        if args[:3] == ["diff", "--name-only", "--diff-filter=U"]:
            return 0, "foo.rs"
        return 0, ""

    worker.__dict__["_git_in_workspace"] = MethodType(fake_git, worker)
    envelope = ConflictResolverEnvelope(
        gave_up=False,
        summary="resolved foo.rs by taking task side and adapting call site",
        files_touched=["foo.rs"],
    )
    stub = _StubAgent(_StubAgentResult(structured=envelope, rc=0))
    with (
        patch("quikode.workers.rebase_conflicts.make_agent", return_value=stub),
        patch("quikode.workers.task_worker.exec_in", side_effect=fake_exec),
    ):
        outcome = worker._resolve_one_conflict_step(iteration=1)
    # `git add -A` + `rebase --continue` were called after the agent.
    assert ["add", "-A"] in git_calls
    assert any(a[:2] == ["-c", "core.editor=true"] and "rebase" in a and "--continue" in a for a in git_calls)
    assert outcome is None


def test_resolver_parse_errors_block(tmp_path) -> None:
    worker = _build_worker(tmp_path)
    fake_exec = _patch_git_helpers(worker)
    stub = _StubAgent(
        _StubAgentResult(
            structured=None,
            rc=0,
            parse_errors=("gave_up: field required", "summary: field required"),
        )
    )
    with (
        patch("quikode.workers.rebase_conflicts.make_agent", return_value=stub),
        patch("quikode.workers.task_worker.exec_in", side_effect=fake_exec),
    ):
        outcome = worker._resolve_one_conflict_step(iteration=1)
    assert outcome is not None
    assert outcome.final_state == State.BLOCKED
    note = _last_state_log_note(worker)
    assert "field required" in note


def test_resolver_transport_failure_blocks(tmp_path) -> None:
    """rc != 0 (transport-level failure) blocks even with no parse errors."""
    worker = _build_worker(tmp_path)
    fake_exec = _patch_git_helpers(worker)
    stub = _StubAgent(_StubAgentResult(structured=None, rc=124, transient=True))
    with (
        patch("quikode.workers.rebase_conflicts.make_agent", return_value=stub),
        patch("quikode.workers.task_worker.exec_in", side_effect=fake_exec),
    ):
        outcome = worker._resolve_one_conflict_step(iteration=1)
    assert outcome is not None
    assert outcome.final_state == State.BLOCKED
    note = _last_state_log_note(worker)
    assert "rc=124" in note
