"""Subtasks worker mixin."""

from __future__ import annotations

import json
import sys
from typing import Any

from pydantic import ValidationError

from quikode import fsm_runtime
from quikode.agent_schemas import PlannerOutput
from quikode.state import State, SubtaskState
from quikode.subtask_schema import PlanValidationError, Subtask
from quikode.types import Verdict
from quikode.workers import subtask_stop_loss
from quikode.workers.outcomes import WorkerOutcome
from quikode.workers.planner_driver import PlannerDriverMixin, _wire_to_runtime_plan
from quikode.workers.subtask_completion import SubtaskCompletionMixin
from quikode.workers.subtask_execution import (
    _CANNOT_REPRODUCE_CHECKER_PREFIX,
    _EMPTY_DIFF_CHECKER_PREFIX,
    SubtaskExecutionMixin,
)
from quikode.workers.subtask_progress import SubtaskProgressMixin


class _TaskWorkerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.workers.task_worker"], name)


_tw = _TaskWorkerGlobals()


def _subtask_from_row(row: dict) -> Subtask:
    """Reconstruct a Subtask from a store row. Used for resuming leftover
    fixup subtasks whose original FixupPlan object was not persisted."""
    return Subtask(
        id=row["subtask_id"],
        title=row.get("title", "") or "",
        depends_on=tuple(json.loads(row.get("depends_on") or "[]")),
        files_to_touch=tuple(json.loads(row.get("files_to_touch") or "[]")),
        boundary=row.get("boundary", "") or "",
        acceptance=tuple(json.loads(row.get("acceptance") or "[]")) or ("(reconstructed)",),
        notes=row.get("notes", "") or "",
        kind=row.get("kind", "spec") or "spec",
    )


class SubtaskWorkerMixin(
    PlannerDriverMixin,
    SubtaskProgressMixin,
    SubtaskExecutionMixin,
    SubtaskCompletionMixin,
):
    def _plan(self: Any) -> None:
        # Plan 33 D1: build (or load) the EvaluationContract before
        # invoking the planner. The contract drives every prompt render
        # downstream (planner, doer, checker, triage, fixup, merge).
        contract = self._evaluation_contract()
        rubric_categories = list(self.cfg.pre_pr_rubric_categories or [])
        rubric_min_score = int(self.cfg.pre_pr_rubric_min_score)

        # Resume path: when `quikode resume <id>` set the flag, skip the
        # planner agent and reconstruct the Plan from the existing subtasks
        # (and stored plan_text). The subtask loop will skip rows already
        # in DONE state, so work picks up where it left off.
        row = self._row()
        if row.get("resume_from_existing_subtasks") and row.get("plan_text"):
            fsm_runtime.environment_ready(self.store, self.node.id, note="resume - skipping planner")
            self.plan_text = str(row["plan_text"] or "")
            # Plan 26: skip Z-99 stabilization injection on resume when the
            # task already has fixup subtasks (re-injecting Z-99 mid-fixup
            # creates a parallel-but-unsequenced spec subtask that competes
            # with the in-flight fixups and burns retries on a gate that
            # can't pass until the fixups land).
            spec_gate_command = self.cfg.local_ci_command
            if self._has_existing_fixup_subtasks():
                spec_gate_command = None
            # Plan 38 PR-B.4: plan_text is now the wire-schema PlannerOutput
            # JSON (no fences, no prose) since the planner runs through the
            # JsonAgent layer. Parse via pydantic, then translate wire →
            # runtime via the same helper the live planner driver uses.
            try:
                planner_output = PlannerOutput.model_validate_json(self.plan_text)
                self.plan = _wire_to_runtime_plan(
                    planner_output,
                    expected_node_id=self.node.id,
                    spec_gate_command=spec_gate_command,
                    rubric_categories=rubric_categories,
                    rubric_min_score=rubric_min_score,
                )
            except (ValidationError, PlanValidationError) as e:
                # plan_text was malformed for some reason — fall through to
                # re-plan with the agent rather than crash.
                _tw.log.warning(
                    "resume: stored plan_text failed re-parse (%s); falling back to fresh planning", e
                )
            else:
                # Clear the flag so subsequent runs follow the normal path.
                # Plan 52: also clear the replan-cycle marker once the
                # worker has consumed the resume — the matching planner
                # phase will re-fire naturally as the worker progresses
                # (fixup planner on audit failure, replan planner on
                # CHANGES_REQUESTED, merge planner on integration). The
                # marker is a hint, not a control-flow gate.
                self.store.set_field(
                    self.node.id,
                    resume_from_existing_subtasks=0,
                    replan_cycle_marker=None,
                )
                return

        fsm_runtime.environment_ready(self.store, self.node.id)
        plan = self._invoke_planner_with_validators(contract)
        self.plan = plan
        # Persist subtasks so `quikode show / subtasks / export` can surface them.
        # Plan 52: tag the initial planner emission as cycle 1 / kind="initial"
        # so `qk replan-cycle` knows which rows belong to the first cycle.
        self.store.upsert_subtasks(
            self.node.id,
            [
                {
                    "subtask_id": s.id,
                    "title": s.title,
                    "depends_on": list(s.depends_on),
                    "files_to_touch": list(s.files_to_touch),
                    "boundary": s.boundary,
                    "acceptance": list(s.acceptance),
                    "notes": s.notes,
                }
                for s in plan.subtasks
            ],
            planning_cycle=1,
            planning_kind="initial",
        )

    def _has_existing_fixup_subtasks(self: Any) -> bool:
        """True if the task already has any `kind="fixup-…"` subtask rows.
        Used by plan 26 to skip Z-99 stabilization injection on resume —
        once the pre-PR audit has produced fixups, re-injecting Z-99
        mid-fixup creates a competing spec subtask that can't pass until
        the fixups land, wasting retries."""
        for row in self.store.list_subtasks(self.node.id):
            kind = row.get("kind") or "spec"
            if kind.startswith("fixup-"):
                return True
        return False

    def _subtask_loop(self: Any) -> WorkerOutcome | None:
        """Iterate subtasks in topological order. Each subtask retries
        (effectively) unbounded; the v3 progress-check agent decides when
        to give up.

        Three gating fields collaborate:
        - ``subtask_hard_max_attempts`` (default 30): absolute ceiling.
        - ``subtask_progress_check_after`` / ``_every``: cadence at which
          the progress-check agent fires.
        - ``subtask_flatline_block_count``: consecutive FLATLINED verdicts
          before BLOCK.

        Transient retries (push-network blip, container OOM mid-doer)
        DO NOT bump the attempt counter — only real-failure attempts
        count. This lets infrastructure flakes free-retry without eating
        into the convergence budget.

        Critical contract: a subtask exhausting attempts (hard ceiling or
        flatline block) is a **task-level failure**. We return BLOCKED
        immediately and skip final_check — every later subtask depends,
        transitively or explicitly, on its predecessors having landed
        cleanly. Continuing past a BLOCKED subtask just builds on broken
        foundation, burns token budget, and produces a misleadingly-
        further-along task that can't actually pass.

        The user's recovery path is `quikode resume <id>`: fix whatever
        was wrong (prompt change, model swap, manual code edit), then
        resume from the failed subtask with the rest of the plan intact.
        """
        assert self.plan is not None, "_plan() must run before _subtask_loop()"
        spec_outcome = self._run_subtask_set(self.plan.topo_order())
        if spec_outcome is not None:
            return spec_outcome
        # Pick up any non-DONE fixup subtasks left over from a prior fixup
        # round (e.g. resume mid-fixup). They live in the store but not in
        # `self.plan.topo_order()` because the original planner didn't emit
        # them — the audit-driven fixup planner did. Without this, resume
        # silently skips pending fixups and the worker advances to
        # LOCAL_CI_CHECKING with half-applied audit findings.
        leftover = self._collect_leftover_fixup_subtasks()
        if leftover:
            _tw.log.info(
                "subtask loop: driving %d leftover fixup subtask(s) from prior round: %s",
                len(leftover),
                ", ".join(s.id for s in leftover),
            )
            return self._run_subtask_set(leftover)
        return None

    def _collect_leftover_fixup_subtasks(self: Any) -> list[Subtask]:
        """Reconstruct Subtask objects for any non-DONE rows in the store
        that aren't part of `self.plan.topo_order()`. These are fixup slices
        from a prior `_run_fixup_round` that didn't finish before a daemon
        restart / orphan recovery."""
        assert self.plan is not None
        plan_ids = {s.id for s in self.plan.topo_order()}
        out: list[Subtask] = []
        for row in self.store.list_subtasks(self.node.id):
            sid = row.get("subtask_id")
            if not sid or sid in plan_ids:
                continue
            if row.get("state") == SubtaskState.DONE.value:
                continue
            try:
                sub = _subtask_from_row(row)
            except Exception as e:
                _tw.log.warning(
                    "could not reconstruct leftover fixup subtask %s: %s; skipping",
                    sid,
                    e,
                )
                continue
            out.append(sub)
        return out

    def _run_subtask_set(self: Any, subtasks: list[Subtask]) -> WorkerOutcome | None:
        """Drive a sequence of subtasks through the doer/checker/triage loop.

        Used by both the original spec loop (`_subtask_loop`) and the v3
        fixup-decomposition flow (`_run_fixup_round`). Subtasks already in
        DONE state in the store are skipped (idempotent for resume + for
        re-entry after a fixup round). On a subtask block, the task enters
        BLOCKED immediately and later PENDING subtasks remain PENDING with an
        explanatory note so resume/rewind can continue without a stale terminal
        cascade marker.
        """
        hard_max = self.cfg.subtask_hard_max_attempts
        for subtask in subtasks:
            outcome = self._run_one_subtask(subtask, subtasks, hard_max)
            if outcome is not None:
                return outcome
        return None  # all subtasks settled — fall through to caller (final_check or fixup re-check)

    def _run_one_subtask(
        self: Any, subtask: Subtask, subtasks: list[Subtask], hard_max: int
    ) -> WorkerOutcome | None:
        boundary_outcome = self._subtask_boundary_checks(subtask)
        if boundary_outcome is not None:
            return boundary_outcome
        existing = self.store.get_subtask(self.node.id, subtask.id)
        if existing and existing["state"] == SubtaskState.DONE.value:
            return None
        if existing and existing["state"] == SubtaskState.SKIPPED.value:
            _tw.log.warning(
                "task %s subtask %s had old SKIPPED state; repairing to PENDING",
                self.node.id,
                subtask.id,
            )
            self.store.update_subtask(self.node.id, subtask.id, state=SubtaskState.PENDING.value)
        yield_outcome = self._maybe_yield_at_boundary()
        if yield_outcome is not None:
            return yield_outcome
        settled, block_reason = self._attempt_subtask_until_settled(subtask, hard_max, existing)
        return None if settled else self._block_subtask_set(subtask, subtasks, block_reason, hard_max)

    def _subtask_boundary_checks(self: Any, subtask: Subtask) -> WorkerOutcome | None:
        rebase_outcome = self._handle_parent_rebase_if_needed()
        if rebase_outcome:
            return rebase_outcome
        divergence_outcome = self._handle_branch_divergence_if_needed()
        if divergence_outcome:
            return divergence_outcome
        return None

    def _attempt_subtask_until_settled(
        self: Any, subtask: Subtask, hard_max: int, existing: dict | None
    ) -> tuple[bool, str | None]:
        triage_notes: str | None = None
        attempt = int((existing or {}).get("retries") or 0)
        consecutive_transients = 0
        consecutive_reprovisions = 0
        while attempt < hard_max:
            # Pre-flight: ensure the dev container is alive before each attempt.
            # If it isn't, recreate cleanly. Caps consecutive recreations at 3
            # so a permanently-broken provisioning path (bad image, missing
            # mount, etc.) doesn't pin the worker indefinitely.
            try:
                recreated = self.execution_backend.ensure_running(self._h, self._existing_worktree_path())
            except Exception as exc:
                _tw.log.warning(
                    "subtask %s/%s: ensure_dev_container_running raised %s; proceeding",
                    self.node.id,
                    subtask.id,
                    exc,
                )
                recreated = False
            if recreated:
                consecutive_reprovisions += 1
                _tw.log.warning(
                    "subtask %s/%s: dev container recreated (consecutive=%d/3)",
                    self.node.id,
                    subtask.id,
                    consecutive_reprovisions,
                )
                if consecutive_reprovisions > 3:
                    return False, (
                        f"subtask {subtask.id}: dev container recreation failed "
                        f"3 consecutive times; aborting attempt loop"
                    )
            else:
                consecutive_reprovisions = 0
            attempt += 1
            fsm_runtime.enter_doing_subtask(self.store, self.node.id, note=f"{subtask.id} attempt {attempt}")
            self._do_subtask(subtask, attempt, triage_notes)
            fsm_runtime.enter_checking_subtask(self.store, self.node.id, note=subtask.id)
            outcome = self._check_subtask(subtask)
            if outcome.transient:
                consecutive_transients, capped = self._record_transient_subtask_failure(
                    subtask, attempt, outcome, consecutive_transients
                )
                if capped:
                    return False, capped
                attempt -= 1
                continue
            consecutive_transients = 0
            checker_text = outcome.checker_text
            if outcome.verdict is Verdict.PASS:
                settled, retry, checker_text = self._handle_passed_subtask(subtask, outcome.checker_text)
                if settled:
                    return True, None
                if retry:
                    attempt -= 1
                    continue
            triage_notes = self._record_subtask_triage(subtask, attempt, hard_max, checker_text, outcome)
            sig_block = self._maybe_signature_stop_loss(subtask)
            if sig_block:
                return False, sig_block
            progress_block = self._maybe_record_progress_block(subtask, attempt)
            if progress_block:
                return False, progress_block
        return False, None

    def _handle_passed_subtask(self: Any, subtask: Subtask, checker_text: str) -> tuple[bool, bool, str]:
        pass_outcome = self._handle_subtask_pass(subtask, checker_text=checker_text)
        if pass_outcome.kind == "settled":
            fsm_runtime.enter_committing(self.store, self.node.id, note=f"{subtask.id} passed")
            fsm_runtime.enter_pushing(self.store, self.node.id, note=f"{subtask.id} committed and pushed")
            return True, False, ""
        if pass_outcome.kind == "transient_retry":
            fsm_runtime.enter_triaging_subtask(
                self.store, self.node.id, note=f"{subtask.id} transient commit/push failure"
            )
            return False, True, ""
        return False, False, pass_outcome.synthesized_checker_text

    def _record_transient_subtask_failure(
        self: Any, subtask: Subtask, attempt: int, outcome: Any, consecutive_transients: int
    ) -> tuple[int, str | None]:
        consecutive_transients += 1
        if consecutive_transients > self.cfg.subtask_transient_max_retries:
            return consecutive_transients, (
                f"subtask transient checker failures exceeded cap ({self.cfg.subtask_transient_max_retries})"
            )
        self.store.increment_subtask_transient_retries(self.node.id, subtask.id)
        self._append_retry_reason(subtask, attempt, outcome, transient=True)
        fsm_runtime.enter_triaging_subtask(
            self.store, self.node.id, note=f"{subtask.id} transient checker failure"
        )
        _tw.time.sleep(15)
        return consecutive_transients, None

    def _record_subtask_triage(
        self: Any, subtask: Subtask, attempt: int, hard_max: int, checker_text: str, outcome: Any
    ) -> str:
        fsm_runtime.enter_triaging_subtask(
            self.store, self.node.id, note=f"{subtask.id} attempt {attempt} failed"
        )
        # Plan 51: empty-diff transport failures skip the LLM triage
        # call. The triage agent has nothing to teach against (the diff
        # is empty by construction), so synthesize a transport-layer
        # note directly and stamp `failure_layer="transport"` on the
        # retry signature. The synthesized text mirrors the shape of
        # the existing triage-transport-failure artifact for
        # consistency with `qk show`.
        if checker_text.startswith(_EMPTY_DIFF_CHECKER_PREFIX):
            triage_notes = (
                "TRIAGE TRANSPORT FAILURE\n"
                "failure_layer: transport\n"
                "root_cause: doer produced no diff (empty git status). "
                "Transport-class failure; no LLM triage call needed."
            )
            failure_layer: str | None = "transport"
            self.store.add_artifact(self.node.id, f"subtask_triage:{subtask.id}", triage_notes)
        elif checker_text.startswith(_CANNOT_REPRODUCE_CHECKER_PREFIX):
            # Plan 53: empty-diff + green-gates + kind=fixup_ci means
            # the doer cannot reproduce the GitHub CI failure locally.
            # Skip the LLM triage call (it has nothing to teach against
            # — the local state is green) and stamp
            # `failure_layer="cannot_reproduce"` directly so the new
            # K=2 stop-loss fires on the second occurrence.
            triage_notes = (
                "TRIAGE CANNOT_REPRODUCE\n"
                "failure_layer: cannot_reproduce\n"
                "root_cause: GitHub CI failed but the local container's "
                "objective gate and scoped witnesses are green on an "
                "empty diff. Likely environmental drift (cached "
                "intermediate artifacts, pinned-version divergence, or "
                "missing checked-in generated file). No LLM triage "
                "call needed; operator should investigate the "
                "environmental delta."
            )
            failure_layer = "cannot_reproduce"
            self.store.add_artifact(self.node.id, f"subtask_triage:{subtask.id}", triage_notes)
        else:
            triage_notes, failure_layer = self._triage_subtask(subtask, attempt, hard_max, checker_text)
        self.store.update_subtask(
            self.node.id, subtask.id, triage_notes=triage_notes, state=SubtaskState.TRIAGING.value
        )
        self.store.increment_subtask_retries(self.node.id, subtask.id)
        self._append_retry_reason(subtask, attempt, outcome, transient=False, failure_layer=failure_layer)
        return triage_notes

    def _append_retry_reason(
        self: Any,
        subtask: Subtask,
        attempt: int,
        outcome: Any,
        *,
        transient: bool,
        failure_layer: str | None = None,
    ) -> None:
        verdict = getattr(outcome, "verdict", None)
        cat, sig = _tw.retry_classify.classify_retry(
            rc=outcome.rc,
            stderr=outcome.stderr,
            stdout=outcome.checker_text,
            hint="checker",
            verdict=verdict,
            failure_layer=failure_layer,
        )
        self.store.append_retry_reason(
            self.node.id, subtask.id, attempt=attempt, category=cat, signature=sig, transient=transient
        )

    def _maybe_signature_stop_loss(self: Any, subtask: Subtask) -> str | None:
        """Block when the last K non-transient retry_reasons indicate
        a cannot_reproduce / transport / same-signature storm. Each
        check is delegated to a pure helper in
        `quikode.workers.subtask_stop_loss` so the helpers stay
        independently testable; ordering matters: cannot_reproduce
        runs first (K=2), then transport (K=3), then same-signature
        (K=N). All three checks are independent of
        `_maybe_record_progress_block` — that one relies on the
        progress-check agent's verdict, which can rate different-but-
        equally-invalid output as 'progressing' and so misses retry
        storms with stable failure signatures.
        """
        cap_cr = self.cfg.subtask_cannot_reproduce_stop_loss_count
        cap_tr = self.cfg.subtask_transport_stop_loss_count
        cap_ss = self.cfg.subtask_same_signature_block_count
        max_cap = max(cap_cr, cap_tr, cap_ss)
        # One DB read covers all three checks; each helper slices the
        # tail it needs.
        sigs_all = self.store.last_n_retry_signatures(self.node.id, subtask.id, limit=max_cap)
        block = subtask_stop_loss.maybe_cannot_reproduce_stop_loss(
            subtask_id=subtask.id, sigs=sigs_all[:cap_cr], k=cap_cr
        )
        if block is not None:
            return block
        block = subtask_stop_loss.maybe_transport_stop_loss(sigs=sigs_all[:cap_tr], k=cap_tr)
        if block is not None:
            return block
        return subtask_stop_loss.maybe_same_signature_stop_loss(sigs=sigs_all[:cap_ss], n=cap_ss)

    def _maybe_record_progress_block(self: Any, subtask: Subtask, attempt: int) -> str | None:
        if not self._should_run_progress_check(attempt):
            return None
        verdict_obj = self._run_progress_check(subtask, attempt)
        if verdict_obj.verdict != "flatlined":
            self.store.reset_subtask_flatline_count(self.node.id, subtask.id)
            return None
        flatline_count = self.store.increment_subtask_flatline_count(self.node.id, subtask.id)
        if flatline_count >= self.cfg.subtask_flatline_block_count:
            return f"progress check flatlined {flatline_count} consecutive times"
        return None

    def _block_subtask_set(
        self: Any, subtask: Subtask, subtasks: list[Subtask], block_reason: str | None, hard_max: int
    ) -> WorkerOutcome:
        reason_text = block_reason or f"exhausted hard ceiling of {hard_max} attempts"
        self._mark_subtask_blocked(subtask, reason_text)
        self._mark_remaining_pending_after_block(after=subtask.id, subtasks=subtasks)
        reason = f"subtask {subtask.id} blocked: {reason_text}; remaining subtasks held pending."
        fsm_runtime.block_current(self.store, self.node.id, note=reason, last_error=reason[:1000])
        return WorkerOutcome(State.BLOCKED, reason)

    def _maybe_yield_at_boundary(self: Any) -> WorkerOutcome | None:
        """Check if a higher-priority queued task warrants yielding this slot.

        At a subtask-completion boundary, the worktree is clean (commit just
        landed, push just finished) — a safe pause point. If yielding, we
        transition the task back to PENDING with the resume marker, return
        a special outcome whose final_state is PENDING so the orchestrator
        frees the slot. The yielded task gets re-picked when its priority
        is highest among queued candidates.

        Off by default. Returns None unless `cfg.preempt_at_subtask_boundary`
        is True AND the priority delta exceeds `cfg.preempt_yield_threshold`.
        """
        if not self.cfg.preempt_at_subtask_boundary:
            return None
        # Score self as if currently pickable. Stacked-state isn't relevant
        # at yield time; what matters is unblock_boost + id ranking.
        my_score = _tw.scheduler.task_priority_if_picked(
            task_id=self.node.id,
            dag=self.dag,
            scope=set(self.dag.nodes.keys()),
        )
        # Best queued (PENDING) priority across all tasks. We pass scope=all
        # nodes since the worker doesn't see the orchestrator's --only scope;
        # in practice scope == all nodes for most ops modes, and a yield to a
        # task outside the orchestrator's scope is a no-op (the orchestrator
        # just won't pick it; some other in-flight slot will fill instead).
        best_id, best_score = _tw.scheduler.best_queued_priority(
            cfg=self.cfg,
            dag=self.dag,
            store=self.store,
            scope=set(self.dag.nodes.keys()),
            in_flight=set(),
        )
        if best_score is None:
            return None
        delta = best_score - my_score
        if delta <= self.cfg.preempt_yield_threshold:
            return None
        # Yield: re-pend self with resume marker so the next provision
        # picks up at the next subtask. The container/worktree are torn
        # down by the worker's normal exit path. Cost is ~2-3 min of
        # re-provision overhead — only worth it for sizable priority deltas.
        _tw.log.info(
            "task %s yielding subtask-boundary slot to %s (priority delta=%d, threshold=%d)",
            self.node.id,
            best_id,
            delta,
            self.cfg.preempt_yield_threshold,
        )
        _tw.log.info("task %s remains active; preemptive yielding is outside the canonical FSM", self.node.id)
        return None
