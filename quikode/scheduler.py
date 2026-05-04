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
from typing import TYPE_CHECKING

from .state import State

if TYPE_CHECKING:
    from .config import Config
    from .dag import DAG
    from .state import Store

log = logging.getLogger("quikode.scheduler")


# States where a dep is "stack-ready" — has a remote branch a child can fork off.
# Includes the transient PROVISIONING / FIXUP_PLANNING states because the
# parent's branch already exists on origin throughout these (the worker only
# tears down + recreates the container, never the branch). Without these, a
# child briefly loses stacking eligibility during the parent's review-response
# provision window — the picker would skip it for one tick and re-evaluate
# next loop. Cosmetic but worth fixing for cleaner picker behavior.
#
# This is the "speculative" set — used by `cfg.stacking_readiness="speculative"`
# (default). The "settled" set is just {AWAITING_MERGE} plus a quiet-time
# predicate, evaluated by `is_parent_stack_ready`.
STACK_READY_STATES = frozenset(
    {
        State.POLLING_CI.value,
        State.AWAITING_MERGE.value,
        State.PR_OPENING.value,
        State.RESPONDING_TO_REVIEW.value,
        State.PROVISIONING.value,
        State.FIXUP_PLANNING.value,
    }
)


def is_parent_stack_ready(
    *,
    cfg: Config,
    parent_state: str | None,
    parent_id: str,
    store: Store,
    now: float | None = None,
) -> bool:
    """Predicate: is this parent eligible to act as a stack base for children?

    Honors `cfg.stacking_readiness`:

    - `"speculative"` (default): any state in `STACK_READY_STATES`. A parent
      that just opened its PR is fair game; children fork immediately.
    - `"settled"`: parent must be in AWAITING_MERGE quietly for at least
      `cfg.stack_settle_quiet_s` (per the most recent transition INTO
      AWAITING_MERGE, read from state_log). A flap through RESPONDING_TO_REVIEW
      resets the quiet timer.

    `parent_state` is the cached state we already read for the candidate's
    deps — passed in to avoid an extra Store.get per evaluation.
    """
    if parent_state is None:
        return False
    mode = getattr(cfg, "stacking_readiness", "speculative")
    if mode == "speculative":
        return parent_state in STACK_READY_STATES
    if parent_state != State.AWAITING_MERGE.value:
        return False
    quiet_s = int(getattr(cfg, "stack_settle_quiet_s", 0))
    if quiet_s <= 0:
        return True
    last_ts = store.last_entered_state_ts(parent_id, State.AWAITING_MERGE)
    if last_ts is None:
        return False
    if now is None:
        now = time.time()
    return (now - last_ts) >= quiet_s


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

    Components:
    - `stacked_boost`: +50 for stacked children (Phase 3+ chain throughput).
    - `unblock_boost`: +5 per direct dependent in scope. High-fan-out tasks
      unblock more downstream parallelism.
    - `resume_boost`: +15 if the task already has an open PR (orphan
      recovery / explicit resume put it back in PENDING but we shouldn't
      re-pick a fresh root over it). Plus up to +25 scaled by completed
      subtask fraction. Keeps progressed tasks ahead of cold roots without
      dominating high-fan-out roots (max +40).
    - `id_penalty`: -(R-XXXX // 10), so R-0001 ≈ 0, R-0220 ≈ -22. Tiebreak
      toward lower IDs (rough milestone order) without dominating fan-out.

    NOT scored here: review/CI-fix priorities — those are dispatched on
    separate code paths (`_poll_review_threads`, `_poll_pr_loop` ci-branch)
    and do not compete for slots through this scorer.
    """
    stacked_boost = 50 if is_stacked else 0
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
    try:
        id_num = int(task_id.split("-")[1])
    except (IndexError, ValueError):
        id_num = 9999
    id_penalty = id_num // 10
    return stacked_boost + unblock_boost + pr_boost + progress_boost - id_penalty


def _resume_signals(row: dict | None, store: Store) -> tuple[bool, int, int]:
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
    store: Store,
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
    store: Store,
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
