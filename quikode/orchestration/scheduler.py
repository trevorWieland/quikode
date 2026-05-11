"""Shared scheduler helpers — priority scoring + candidate eligibility.

Both `Orchestrator._pick_next` and `TaskWorker._should_yield_at_boundary`
consume these so the priority signal is consistent across "what to start
next" and "should I yield to something more urgent?" decisions.

Lives outside `orchestrator.py` because the worker can't reach the
orchestrator instance — they communicate through `store` + `dag` only.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from quikode.fsm import STACK_READY_STATES as FSM_STACK_READY_STATES
from quikode.state import State

if TYPE_CHECKING:
    from quikode.config import Config
    from quikode.dag import DAG
    from quikode.state import TaskRow


@runtime_checkable
class _SchedulerStore(Protocol):
    """Plan 59 fix C: minimal Store-like interface the scheduler needs.

    Production `Store` satisfies it; the TUI's `_ReadOnlyStoreAdapter`
    (read-only sqlite3 connection) also satisfies it so the TUI's
    `pending` count is computed by the same `collect_pick_candidates`
    pipeline as the orchestrator's `_pick_next`.
    """

    def completed_ids(self) -> set[str]: ...
    def active_ids(self) -> set[str]: ...
    def get(self, task_id: str) -> Any: ...
    def most_recent_awaiting_review_entry_ts(self, task_id: str) -> float | None: ...
    def subtask_progress(self, task_id: str) -> tuple[int, int]: ...


log = logging.getLogger("quikode.scheduler")


# Stack readiness starts with canonical FSM post-PR states. Worker-internal
# active states may also be eligible when they already have a branch/PR base
# that children can stack on.
STACK_READY_STATES = frozenset(
    {
        *(s.value for s in FSM_STACK_READY_STATES),
        State.PENDING_CI.value,
        State.PR_OPENING.value,
        # Plan 58: ADDRESSING_FEEDBACK retired. Audit-stage states are
        # stack-ready: a child can fork off as soon as the parent has a
        # branch + PR/audit machinery running.
        State.AUDIT_LOCAL_CI.value,
        State.AUDIT_RUBRIC.value,
        State.AUDIT_STANDARDS.value,
        State.AUDIT_ARCHITECTURE.value,
        State.AUDIT_BEHAVIOR.value,
        State.PROVISIONING.value,
        State.FIXUP_PLANNING.value,
    }
)


def is_parent_stack_ready(
    *,
    cfg: Config,
    parent_state: str | None,
    parent_id: str,
    store: _SchedulerStore,
    now: float | None = None,
) -> bool:
    """Predicate: is this parent eligible to act as a stack base for children?

    Honors `cfg.stacking_readiness`:

    - `"speculative"` (default): any state in `STACK_READY_STATES` — children
      fork the moment a PR exists on origin.
    - `"settled"` (plan 30): parent must be in `AWAITING_REVIEW` continuously
      for ≥ `cfg.review_ready_settle_s` (default 15 min). The same threshold
      that gates the ntfy notification gates dependent kickoff — children
      always start from a CI-green base the operator could have reviewed.
    """
    if parent_state is None:
        return False
    mode = getattr(cfg, "stacking_readiness", "speculative")
    if mode == "speculative":
        return parent_state in STACK_READY_STATES
    # "settled" mode: must be AWAITING_REVIEW AND have been there long enough.
    if parent_state != State.AWAITING_REVIEW.value:
        return False
    threshold = getattr(cfg, "review_ready_settle_s", 0)
    if threshold <= 0:
        return True
    entered_ts = store.most_recent_awaiting_review_entry_ts(parent_id)
    if entered_ts is None:
        return False
    if now is None:
        now = time.time()
    return (now - entered_ts) >= threshold


# Resume-boost weights. Keep these visible at module top so the score
# calibration is auditable from one place.
_RESUME_PR_OPEN_BOOST = 15
_RESUME_SUBTASK_FRACTION_MAX = 25  # fully-done-but-pending caps here


def score_candidate(
    *,
    task_id: str,
    is_stacked: bool,
    dag: DAG,
    scope: set[str],
    has_open_pr: bool = False,
    subtask_done: int = 0,
    subtask_total: int = 0,
) -> int:
    """Priority score for a pickable task. Higher wins.

    Plan 30: primary vs stacked is now a HARD TIER decided by the caller
    (see `prefer_primary_candidates`), not a soft +50 boost in the scorer.
    `is_stacked` is retained on the signature for back-compat but no longer
    affects the score — both tiers use the same intra-tier ranking below.

    Components:
    - `unblock_boost`: +5 per direct dependent in scope. High-fan-out tasks
      unblock more downstream parallelism.
    - `pr_boost`: +15 if the task already has an open PR (orphan recovery /
      explicit resume put it back in PENDING but we shouldn't re-pick a
      fresh root over it). Plus up to +25 scaled by completed subtask
      fraction. Keeps progressed tasks ahead of cold roots without
      dominating high-fan-out roots.
    """
    del is_stacked  # plan 30: tiering is the caller's job, not the scorer's
    dependents = sum(
        1 for other_id, other in dag.nodes.items() if other_id in scope and task_id in other.depends_on
    )
    unblock_boost = dependents * 5
    pr_boost = _RESUME_PR_OPEN_BOOST if has_open_pr else 0
    if subtask_total > 0:
        fraction = max(0.0, min(1.0, subtask_done / subtask_total))
        progress_boost = round(_RESUME_SUBTASK_FRACTION_MAX * fraction)
    else:
        progress_boost = 0
    return unblock_boost + pr_boost + progress_boost


def prefer_primary_candidates(candidates: list[dict]) -> list[dict]:
    """Plan 30: primary tasks (no unmet deps) take precedence over stacked.

    If any primary candidate is pickable, return only the primary ones.
    Stacked candidates are deferred until the primary pool empties — the
    user's stated preference: "stacked-diff issues only get picked up when
    all available primary nodes are done or awaiting review." Primaries
    unblock more downstream work per slot than stacked children, and a
    stacked child that starts after the parent is review-ready-settled has
    the strongest possible foundation (CI-green, post-settle).
    """
    primaries = [c for c in candidates if not c.get("is_stacked")]
    if primaries:
        return primaries
    return candidates


def _resume_signals(row: TaskRow | None, store: _SchedulerStore) -> tuple[bool, int, int]:
    """Pull (has_open_pr, subtask_done, subtask_total) for a candidate row.

    Cheap: one indexed SUM over the subtasks table. Returns zeros when the
    task hasn't been touched yet (no row, no subtasks).
    """
    if not row:
        return (False, 0, 0)
    has_open_pr = bool(row.get("pr_number"))
    done, total = store.subtask_progress(str(row["id"]))
    return (has_open_pr, done, total)


def collect_pick_candidates(
    *,
    cfg: Config,
    dag: DAG,
    store: _SchedulerStore,
    scope: set[str],
    in_flight: set[str],
    stack_depth_fn,
    stack_root_fn,
    stack_size_under_root_fn,
    would_form_cycle_fn,
    now: float | None = None,
) -> list[dict]:
    """Enumerate eligible task candidates without side effects.

    The stacking helpers (`stack_depth`, `stack_root`, `stack_size_under_root`,
    `would_form_cycle`) are passed as callables so this module doesn't
    re-import the orchestrator. Pass methods bound to the Orchestrator
    instance from the call site.
    """
    completed = store.completed_ids() & scope
    active = store.active_ids() & scope
    candidates: list[dict] = []
    for nid in sorted(scope):
        if nid in completed or nid in active or nid in in_flight:
            continue
        n = dag.nodes.get(nid)
        if n is None:
            continue
        row = store.get(nid)
        if row and row["state"] not in (State.PENDING.value,):
            continue
        in_scope_deps = [d for d in n.depends_on if d in dag.nodes]
        unmet = [d for d in in_scope_deps if d not in completed]
        has_open_pr, sub_done, sub_total = _resume_signals(row, store)
        if not unmet:
            candidates.append(
                {
                    "task_id": nid,
                    "is_stacked": False,
                    "unmet": [],
                    "row": row,
                    "has_open_pr": has_open_pr,
                    "subtask_done": sub_done,
                    "subtask_total": sub_total,
                }
            )
            continue
        if cfg.stacking_strategy == "off":
            continue
        unmet_states = {d: (store.get(d) or {}).get("state") for d in unmet}
        if not all(
            is_parent_stack_ready(
                cfg=cfg,
                parent_state=s,
                parent_id=d,
                store=store,
                now=now,
            )
            for d, s in unmet_states.items()
        ):
            continue
        if cfg.stacking_strategy == "within-milestone" and not all(
            dag.nodes[d].milestone == n.milestone for d in unmet
        ):
            continue
        depth = stack_depth_fn(unmet[0])
        if depth >= cfg.stacking_max_depth:
            continue
        if would_form_cycle_fn(nid, unmet[0]):
            log.warning(
                "refusing to stack %s on %s — would form parent_task_id cycle",
                nid,
                unmet[0],
            )
            continue
        root = stack_root_fn(unmet[0])
        if stack_size_under_root_fn(root) >= cfg.stacking_max_breadth_per_root:
            log.warning(
                "task %s would exceed stacking_max_breadth_per_root (%d) under root %s",
                nid,
                cfg.stacking_max_breadth_per_root,
                root,
            )
            continue
        candidates.append(
            {
                "task_id": nid,
                "is_stacked": True,
                "unmet": unmet,
                "row": row,
                "has_open_pr": has_open_pr,
                "subtask_done": sub_done,
                "subtask_total": sub_total,
            }
        )
    return candidates


def best_queued_priority(
    *,
    cfg: Config,
    dag: DAG,
    store: _SchedulerStore,
    scope: set[str] | None = None,
    in_flight: set[str] | None = None,
    stack_depth_fn=None,
    stack_root_fn=None,
    stack_size_under_root_fn=None,
    would_form_cycle_fn=None,
) -> tuple[str | None, int | None]:
    """Return (best_task_id, best_score) over all pickable candidates.

    Returns (None, None) if no eligible candidate exists. Used by
    `TaskWorker._should_yield_at_boundary` to decide whether to surrender
    its slot. Stacking helpers are optional — if not provided, only fresh
    roots (all-deps-merged) are considered, which is sufficient for the
    common preemption signal (a high-fan-out root becoming pickable).
    """
    if scope is None:
        scope = set(dag.nodes.keys())
    if in_flight is None:
        in_flight = set()
    # When stacking helpers aren't supplied, restrict to fresh roots.
    if any(
        fn is None
        for fn in (
            stack_depth_fn,
            stack_root_fn,
            stack_size_under_root_fn,
            would_form_cycle_fn,
        )
    ):
        completed = store.completed_ids() & scope
        active = store.active_ids() & scope
        candidates: list[dict] = []
        for nid in sorted(scope):
            if nid in completed or nid in active or nid in in_flight:
                continue
            n = dag.nodes.get(nid)
            if n is None:
                continue
            row = store.get(nid)
            if row and row["state"] not in (State.PENDING.value,):
                continue
            in_scope_deps = [d for d in n.depends_on if d in dag.nodes]
            unmet = [d for d in in_scope_deps if d not in completed]
            if unmet:
                continue
            has_open_pr, sub_done, sub_total = _resume_signals(row, store)
            candidates.append(
                {
                    "task_id": nid,
                    "is_stacked": False,
                    "unmet": [],
                    "row": row,
                    "has_open_pr": has_open_pr,
                    "subtask_done": sub_done,
                    "subtask_total": sub_total,
                }
            )
    else:
        candidates = collect_pick_candidates(
            cfg=cfg,
            dag=dag,
            store=store,
            scope=scope,
            in_flight=in_flight,
            stack_depth_fn=stack_depth_fn,
            stack_root_fn=stack_root_fn,
            stack_size_under_root_fn=stack_size_under_root_fn,
            would_form_cycle_fn=would_form_cycle_fn,
        )
    if not candidates:
        return (None, None)
    # Plan 30: hard-tier filter — primaries first, stacked only as fallback.
    candidates = prefer_primary_candidates(candidates)
    scored = [
        (
            score_candidate(
                task_id=c["task_id"],
                is_stacked=c["is_stacked"],
                dag=dag,
                scope=scope,
                has_open_pr=c.get("has_open_pr", False),
                subtask_done=c.get("subtask_done", 0),
                subtask_total=c.get("subtask_total", 0),
            ),
            c["task_id"],
        )
        for c in candidates
    ]
    scored.sort(key=lambda t: (-t[0], t[1]))
    best_score, best_id = scored[0]
    return (best_id, best_score)


def task_priority_if_picked(*, task_id: str, dag: DAG, scope: set[str]) -> int:
    """Score a task as if it were a pickable fresh-root candidate.

    Used by the worker to compute "my priority right now" when deciding
    whether to yield. Stacked-state isn't asked here — at yield time the
    worker is past provisioning, so the stacking-versus-root distinction
    no longer applies; it's the unblock_boost that matters.
    """
    return score_candidate(task_id=task_id, is_stacked=False, dag=dag, scope=scope)
