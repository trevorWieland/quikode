"""Plan 59 fixes (A) (B) (C) (E'): scoped tests.

- (A): fallback chain with 3 stubs, first two return quota → 3rd is
  invoked within seconds (prior bug would hang for hours on the
  secondary's quota-retry loop).
- (B): worker hits auth-refresh; `_run_with_retry` flips
  `agent_calls.status` to `backoff_auth` during sleep and back to
  `running` on retry. The TUI's `_detail_agent_in_flight` surfaces
  the transition.
- (C): stacking-breadth cap scenario where the prior approximation
  diverges from `collect_pick_candidates` → TUI controller matches
  the real count.
- (E'): quota outcome returns in seconds with `category="quota_exhausted"`;
  the worker's `_record_transient_subtask_failure` sleeps the
  configured 600s default.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from pydantic import BaseModel

from quikode.agent_schemas import ProgressVerdict
from quikode.agents.json_fallback import QuotaFallbackJsonAgent
from quikode.agents.json_protocol import (
    JsonOutputAgent,
    RawTransportResult,
    _run_with_retry,
    agent_call_status_scope,
)
from quikode.config import Config, StackingStrategy
from quikode.dag import DAG, Node
from quikode.state import State, Store
from quikode.tui.controllers.pending_eligibility import count_pending_eligible
from quikode.types import Verdict
from quikode.workers import task_worker
from quikode.workers.outcomes import CheckerOutcome
from quikode.workers.subtasks import SubtaskWorkerMixin

# ---------- (A) 3-link chain fast-fail ----------


@dataclass
class _StubTransport:
    name: str = "stub"
    schema_enforcement: str = "client_side"
    responses: list[RawTransportResult] = field(default_factory=list)
    invocations: list[str] = field(default_factory=list)

    def invoke(
        self,
        prompt: str,
        *,
        output_schema: type[BaseModel] | None,
        handle: Any,
        log_path: Path | None,
        timeout: int,
    ) -> RawTransportResult:
        self.invocations.append(prompt)
        if not self.responses:
            raise AssertionError(f"StubTransport {self.name}: no queued response")
        return self.responses.pop(0)

    def invoke_raw(
        self,
        prompt: str,
        *,
        handle: Any,
        log_path: Path | None,
        timeout: int,
    ) -> RawTransportResult:
        return self.invoke(prompt, output_schema=None, handle=handle, log_path=log_path, timeout=timeout)


def _quota_result(name: str) -> RawTransportResult:
    return RawTransportResult(
        raw_text=None,
        structured=None,
        rc=1,
        transient=False,
        duration_s=0.5,
        stderr_excerpt=f"HTTP 429: {name} exhausted",
        category="quota_exhausted",
    )


def test_plan_59_a_three_link_chain_walks_to_tertiary_in_seconds(tmp_path):
    """Plan 59 fix A: with all transports fast-failing on quota,
    a 3-link chain reaches the tertiary inside a couple seconds — the
    prior bug had the secondary's `_run_with_retry` loop sleep for
    hours on its own quota-retry. Now every transport returns
    immediately, the chain cascades, and the tertiary's PASS lands."""
    primary = _StubTransport(name="glm-zai", responses=[_quota_result("glm-zai")])
    secondary = _StubTransport(name="glm-wafer", responses=[_quota_result("glm-wafer")])
    tertiary = _StubTransport(
        name="codex",
        schema_enforcement="cli_native",
        responses=[
            RawTransportResult(
                raw_text=None,
                structured={"verdict": "progressing", "rationale": "tertiary survived"},
                rc=0,
                transient=False,
                duration_s=1.0,
            )
        ],
    )
    transport = QuotaFallbackJsonAgent(primary=primary, fallbacks=(secondary, tertiary))
    wrapper = JsonOutputAgent(transport=transport, output_schema=ProgressVerdict)

    t0 = time.time()
    result = wrapper.invoke("hi", handle=object(), log_path=tmp_path / "agent.log", timeout=60)
    elapsed = time.time() - t0

    assert elapsed < 5.0, f"chain walk took {elapsed:.1f}s; should be <5s"
    assert isinstance(result.structured, ProgressVerdict)
    assert result.structured.rationale == "tertiary survived"
    assert len(primary.invocations) == 1
    assert len(secondary.invocations) == 1
    assert len(tertiary.invocations) == 1


# ---------- (B) agent_call backoff visibility ----------


def test_plan_59_b_auth_refresh_flips_agent_call_status(monkeypatch, tmp_path):
    """Plan 59 fix B: while `_run_with_retry` sleeps between
    auth-refresh retries the worker's bound status callback flips
    `agent_calls.status` to `backoff_auth` and back to `running`.
    The store's `agent_in_flight_status` surfaces the transition so
    the TUI shows "subtask_doer backoff_auth 45s" instead of an
    undifferentiated "in-flight 45s"."""
    store = Store(tmp_path / "q.db")
    store.upsert_pending("R-1")
    call_id = store.record_agent_call_started("R-1", phase="subtask_doer", cli="json_agent", model="m")

    observed: list[str] = []

    def fake_exec_in(*args: Any, **kwargs: Any) -> tuple[int, str, str]:
        # First call: auth-refresh transient; second: clean.
        if not observed:
            observed.append("called1")
            return (
                99,
                "",
                '{"code":"refresh_token_reused","message":"refresh token was already used"}',
            )
        observed.append("called2")
        return 0, '{"ok": true}', ""

    sleep_log: list[float] = []

    def fake_sleep(s: float) -> None:
        sleep_log.append(s)
        # Snapshot the status mid-sleep so we can prove the callback fired
        # BEFORE the retry started.
        status, _, _, _, sub = store.agent_in_flight_status("R-1")
        observed.append(f"midsleep:{status}:{sub}")

    monkeypatch.setenv("QUIKODE_AUTH_BACKOFF_INITIAL_S", "1")
    monkeypatch.setattr("quikode.agents.json_protocol.exec_in", fake_exec_in)
    monkeypatch.setattr("quikode.agents.json_protocol.time.sleep", fake_sleep)

    callback = MagicMock(wraps=lambda s: store.update_agent_call_status(call_id, s))
    with agent_call_status_scope(callback):
        out = _run_with_retry(
            object(), ["codex"], stdin="prompt", log_path=tmp_path / "agent.log", timeout=60
        )

    # The exec succeeded after the retry.
    assert out.rc == 0
    assert sleep_log  # sleep fired at least once
    # The callback fired with `backoff_auth` (entering sleep) and
    # `running` (exiting). Order matters.
    statuses = [args[0] for args, _ in callback.call_args_list]
    assert statuses == ["backoff_auth", "running"]
    # And the DB observed the `backoff_auth` value mid-sleep.
    assert any(s.startswith("midsleep:running:backoff_auth") for s in observed)
    # After the retry completed and the call_id wasn't explicitly
    # finished, status is back to `running` on the row.
    _, _, _, _, sub = store.agent_in_flight_status("R-1")
    assert sub == "running"


# ---------- (C) TUI pending_eligible parity ----------


def test_plan_59_c_tui_pending_count_matches_collect_pick_candidates(tmp_path):
    """Plan 59 fix C: the TUI controller's pending count is now
    computed by `collect_pick_candidates` + `prefer_primary_candidates`,
    so a stack-breadth-capped scenario surfaces correctly. The prior
    approximation counted every "deps stack-ready" pending row; the
    real scheduler also rejects when the stack root has already hit
    `stacking_max_breadth_per_root` children.
    """
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path,
        stacking_strategy=StackingStrategy.AGGRESSIVE,
        stacking_max_breadth_per_root=3,
        stacking_max_depth=20,
    )

    # Build a DAG: root R-0 + 5 children that all stack off R-0.
    def _node(nid: str, deps: tuple[str, ...]) -> Node:
        return Node(
            id=nid,
            kind="behavior",
            milestone="m1",
            title=nid,
            scope="",
            depends_on=deps,
            completes_behaviors=(),
            supports_behaviors=(),
            boundary_with_neighbors="",
            expected_evidence=(),
            playbook=(),
            rationale="",
            risks=(),
            raw={},
        )

    nodes = {"R-0": _node("R-0", ())}
    for i in range(1, 6):
        nid = f"R-{i}"
        nodes[nid] = _node(nid, ("R-0",))
    dag = DAG(nodes=nodes, milestones={}, raw={})

    store = Store(tmp_path / "q.db")
    # Seed R-0 in AWAITING_REVIEW (stack-ready).
    store.upsert_pending("R-0")
    store.transition("R-0", State.AWAITING_REVIEW, note="seed")
    # Stamp R-0 with a branch so children can stack.
    store.set_field("R-0", branch="feature/R-0")
    # Two children already stamped as stacked (parent_task_ids = R-0).
    # They count toward the breadth-under-root.
    for child_id in ("R-1", "R-2"):
        store.upsert_pending(child_id)
        store.set_parent_chain(
            child_id,
            parent_task_ids=["R-0"],
            parent_branches=["feature/R-0"],
            parent_pr_branches=["feature/R-0"],
        )
    # The remaining 3 (R-3, R-4, R-5) are PENDING with no stack
    # bookkeeping. The prior approximation would call them all
    # eligible (deps stack-ready); the real scheduler rejects because
    # the breadth cap (2) is already saturated under root R-0.
    for child_id in ("R-3", "R-4", "R-5"):
        store.upsert_pending(child_id)

    # Post-plan-59 `count_pending_eligible` reads exclusively from `c`
    # via the read-only Store adapter — `rows` is kept on the signature
    # for back-compat but unused, so we pass real `sqlite3.Row`s
    # fetched from the same connection.
    rows = list(store.conn.execute("SELECT id, state FROM tasks").fetchall())
    count = count_pending_eligible(c=store.conn, cfg=cfg, dag=dag, rows=rows)
    # `stack_size_under_root(R-0)` returns the number of tasks whose
    # stack_root resolves to R-0 — that's R-0 itself plus the
    # already-stamped stacked children R-1 and R-2 (parent_task_ids
    # set), for a total of 3. The breadth cap is also 3, so EVERY
    # candidate that would land under R-0 is rejected via
    # `depth >= cap` (3 >= 3). Under the prior approximation in
    # `pending_eligibility.py` all five children showed up as
    # "pending eligible" (deps stack-ready) — the breadth check
    # wasn't run, so the TUI displayed `pending 5` while the
    # scheduler would have picked 0. Post fix C the counts match.
    assert count == 0, f"expected 0 candidates with breadth cap saturated, got {count}"


# ---------- (E') worker layer category-aware sleep ----------


def test_plan_59_e_prime_worker_transient_handler_sleeps_quota_default(monkeypatch, tmp_path):
    """Plan 59 fix E': the worker's `_record_transient_subtask_failure`
    looks up the outcome's `category` in `cfg.transient_retry_delays_s`
    and sleeps the matching duration. With the default config a
    quota-exhausted outcome sleeps 600s.
    """
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path)
    # Defaults already populated by the model.
    assert cfg.transient_retry_delays_s["quota_exhausted"] == 600
    assert cfg.transient_retry_delays_s["container_vanished"] == 15
    assert cfg.transient_retry_delays_s["auth_refresh"] == 60

    # Stub the worker just enough to drive `_record_transient_subtask_failure`.
    worker: Any = SubtaskWorkerMixin.__new__(SubtaskWorkerMixin)
    worker.cfg = cfg
    node = MagicMock()
    node.id = "R-1"
    worker.node = node
    store = MagicMock()
    worker.store = store

    # Subtask + outcome carrying the quota category.
    subtask = MagicMock()
    subtask.id = "S-01"
    outcome = CheckerOutcome(
        verdict=Verdict.FAIL,
        checker_text="quota",
        transient=True,
        rc=1,
        stderr="HTTP 429",
        category="quota_exhausted",
    )

    sleep_log: list[float] = []
    monkeypatch.setattr(task_worker.time, "sleep", sleep_log.append)

    # Append-retry-reason path needs classify_retry to exist; stub it.
    classify_retry = MagicMock()
    classify_retry.classify_retry = MagicMock(return_value=("quota", "sig"))
    monkeypatch.setattr(task_worker, "retry_classify", classify_retry, raising=False)

    # The append helper calls into _tw and store; just bypass.
    worker._append_retry_reason = MagicMock()
    # fsm_runtime.enter_triaging_subtask reads from store; mock it via task_worker module.
    fsm_runtime = MagicMock()
    monkeypatch.setattr("quikode.workers.subtasks.fsm_runtime", fsm_runtime)

    new_count, capped = worker._record_transient_subtask_failure(
        subtask, attempt=1, outcome=outcome, consecutive_transients=0
    )

    assert capped is None
    assert new_count == 1
    # 600s sleep for quota_exhausted category.
    assert sleep_log == [600]


def test_plan_59_e_prime_worker_transient_handler_sleeps_auth_refresh(monkeypatch, tmp_path):
    """Plan 59 fix E': auth_refresh category sleeps 60s by default."""
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path)

    worker: Any = SubtaskWorkerMixin.__new__(SubtaskWorkerMixin)
    worker.cfg = cfg
    node = MagicMock()
    node.id = "R-1"
    worker.node = node
    worker.store = MagicMock()

    subtask = MagicMock()
    subtask.id = "S-01"
    outcome = CheckerOutcome(
        verdict=Verdict.FAIL,
        checker_text="auth race",
        transient=True,
        rc=124,
        stderr="token_revoked",
        category="auth_refresh",
    )

    sleep_log: list[float] = []
    monkeypatch.setattr(task_worker.time, "sleep", sleep_log.append)
    classify_retry = MagicMock()
    classify_retry.classify_retry = MagicMock(return_value=("transport", "sig"))
    monkeypatch.setattr(task_worker, "retry_classify", classify_retry, raising=False)
    worker._append_retry_reason = MagicMock()
    monkeypatch.setattr("quikode.workers.subtasks.fsm_runtime", MagicMock())

    worker._record_transient_subtask_failure(subtask, attempt=1, outcome=outcome, consecutive_transients=0)

    assert sleep_log == [60]
