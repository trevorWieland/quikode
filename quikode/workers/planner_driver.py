"""Plan 33 + Plan 38 PR-B.4: planner driver mixin.

Owns the path:

  PROVISIONING → PLANNING → planner agent (JsonAgent layer) → wire→runtime
  translation → Z-99 injection → validators → Plan

Plan 38 PR-B.4 retired the prose-parsing path. The planner is invoked via
`make_agent("planner", cfg)`; the JsonAgent layer hands back a validated
`PlannerOutput` (wire schema). `_wire_to_runtime_plan` translates the wire
schema's plain `list[...]` shape into the runtime `Plan` (tuple-coerced
fields + topo validators) and runs Z-99 stabilization injection. Then the
five Plan 33 / Plan 35 validators run on the runtime plan; on failure the
driver re-prompts the planner up to twice with the validator message,
then BLOCKs with `failure_reason="planner_validator_<which>"`.

Parse-error handling: when `JsonAgentResult.parse_errors` is non-empty
(client_side schema enforcement re-prompted twice and still failed) the
driver re-prompts up to twice with the parse-error feedback under the
same budget. Exhausting the budget BLOCKs with
`failure_reason="planner_parse_failure"`.
"""

from __future__ import annotations

import sys
from typing import Any

from quikode import fsm_runtime
from quikode.agent_registry import make_agent
from quikode.agent_schemas import (
    ArchitectureRefSchema,
    PlannerOutput,
    RubricTargetSchema,
    StandardsRefSchema,
    SubtaskSpec,
)
from quikode.evaluation_contract import EvaluationContract
from quikode.planner_validators import (
    PlannerValidationError,
    validate_architecture_refs,
    validate_evidence_partition,
    validate_gauntlet_strategy,
    validate_rubric_coverage,
    validate_standards_refs,
)
from quikode.subtask_schema import (
    Plan,
    PlanValidationError,
    validate_and_build_plan,
)


class _TaskWorkerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.workers.task_worker"], name)


_tw = _TaskWorkerGlobals()


# ---------- wire ↔ runtime translation ----------


def _wire_subtask_to_runtime_dict(spec: SubtaskSpec) -> dict[str, Any]:
    """Translate one wire `SubtaskSpec` to the runtime `Subtask` ingest dict.

    The wire schema uses plain `list[...]` for collection fields; the
    runtime `Subtask` carries `tuple[...]` (coerced via `_coerce_tuple`).
    Going through the dict shape lets `Subtask.model_validate(...)`
    apply its tuple-coercion and acceptance-min-length validators
    without re-implementing them here.
    """
    return {
        "id": spec.id,
        "title": spec.title,
        "depends_on": list(spec.depends_on),
        "files_to_touch": list(spec.files_to_touch),
        "boundary": spec.boundary,
        "acceptance": list(spec.acceptance),
        "notes": spec.notes,
        "interfaces": list(spec.interfaces),
        "kind": spec.kind,
        "rubric_targets": [
            {"category": t.category, "predicted_score": t.predicted_score} for t in spec.rubric_targets
        ],
        "standards_referenced": [
            {"doc_path": r.doc_path, "section": r.section} for r in spec.standards_referenced
        ],
        "architecture_referenced": [
            {"doc_path": r.doc_path, "section": r.section} for r in spec.architecture_referenced
        ],
        "behavior_evidence_advanced": list(spec.behavior_evidence_advanced),
        "root_cause_hypothesis": spec.root_cause_hypothesis,
    }


def _wire_to_runtime_plan(
    planner_output: PlannerOutput,
    *,
    expected_node_id: str | None,
    spec_gate_command: str | None,
    rubric_categories: list[str] | None,
    rubric_min_score: int | None,
) -> Plan:
    """Translate a wire `PlannerOutput` into a runtime `Plan`.

    Steps:
    1. Convert each wire `SubtaskSpec` to a runtime ingest dict (plain
       `list[...]`; the runtime `Subtask` validators coerce to `tuple[...]`).
    2. Hand the dict-shape plan to `validate_and_build_plan`, which:
       - injects the Z-99 stabilization subtask (when `spec_gate_command`
         is supplied) — the wire schema does NOT carry Z-99; only the
         runtime plan does, post-translation;
       - runs the runtime `Plan` model validators (uniqueness, acyclic
         depends_on, topo order);
       - cross-checks `expected_node_id` matches the planner's claim.
    """
    raw_plan = {
        "node_id": planner_output.node_id,
        "summary": planner_output.summary,
        "gauntlet_strategy": planner_output.gauntlet_strategy,
        "subtasks": [_wire_subtask_to_runtime_dict(s) for s in planner_output.subtasks],
        "final_acceptance": list(planner_output.final_acceptance),
    }
    return validate_and_build_plan(
        raw_plan,
        expected_node_id=expected_node_id,
        spec_gate_command=spec_gate_command,
        rubric_categories=rubric_categories,
        rubric_min_score=rubric_min_score,
    )


# Re-export so Plan 35's source-level test can monkeypatch the names on
# this module (`monkeypatch.setattr(planner_driver, "validate_*", ...)`).
__all__ = [
    "PlannerDriverMixin",
    "_wire_to_runtime_plan",
    "validate_architecture_refs",
    "validate_evidence_partition",
    "validate_gauntlet_strategy",
    "validate_rubric_coverage",
    "validate_standards_refs",
]


# Bookkeeping: how many times we'll re-prompt the planner per Plan 33 D3.
_MAX_VALIDATOR_RETRIES = 2
_MAX_PARSE_ERROR_RETRIES = 2


class _PlannerTransientFailure(RuntimeError):
    """Planner transport failed for an infra/agent-auth reason."""


class _PlannerInvokeOutcome:
    """Internal result of one `_invoke_planner_once` round-trip.

    Either `plan` is non-None (success) OR `parse_error_feedback` is non-
    None (caller should re-prompt). `plan_text` is always populated so
    the caller can persist it to `tasks.plan_text` for resume + PR body.
    """

    __slots__ = ("parse_error_feedback", "plan", "plan_text")

    def __init__(
        self,
        *,
        plan: Plan | None,
        plan_text: str,
        parse_error_feedback: str | None,
    ) -> None:
        self.plan = plan
        self.plan_text = plan_text
        self.parse_error_feedback = parse_error_feedback


def _build_parse_error_feedback(parse_errors: tuple[str, ...]) -> str:
    """Render a JSON-schema-failure feedback block for the next prompt."""
    if not parse_errors:
        body = "(agent returned no structured output)"
    else:
        body = "\n".join(f"- {err}" for err in parse_errors[:20])
    return (
        "Your previous response failed JSON schema validation:\n\n"
        f"{body}\n\n"
        "Re-emit a single fenced ```json ... ``` block that conforms "
        "strictly to the planner schema. No prose outside the fence."
    )


class PlannerDriverMixin:
    """Owns the planner-invocation surface. Mixed into SubtaskWorkerMixin
    so the existing `_plan` entry point on `TaskWorker` keeps its shape.
    """

    def _invoke_planner_with_validators(self: Any, contract: EvaluationContract) -> Plan:
        """Run the planner, translate wire→runtime, run validators,
        re-prompt up to twice if a validator OR a parse error fails, then
        BLOCK with `failure_reason="planner_validator_<which>"` (or
        `planner_parse_failure`).

        The two retry budgets are independent: parse errors share their
        own budget (the JsonAgent layer already re-prompts once
        client_side; we do up to two more rounds at the driver layer
        with planner-context-aware feedback). Validator errors run on
        the already-translated `Plan` and re-prompt with the validator
        message.
        """
        prior_attempt_notes: str | None = None
        validator_retries = 0
        parse_retries = 0
        transient_retries = 0
        # First attempt + up to (validator_retries + parse_retries) re-prompts.
        while True:
            phase = self._planner_phase(validator_retries, parse_retries)
            try:
                outcome = self._invoke_planner_once(contract, phase, prior_attempt_notes)
            except _PlannerTransientFailure as e:
                max_transient = int(getattr(self.cfg, "planner_retries_on_transient", 3))
                if transient_retries >= max_transient:
                    note = (
                        f"planner transport failed after {max_transient + 1} transient attempts: "
                        f"{str(e)[:500]}"
                    )
                    fsm_runtime.block_current(
                        self.store,
                        self.node.id,
                        note=note,
                        last_error=note[:1000],
                        failure_reason="planner_transport",
                    )
                    raise RuntimeError(note) from e
                transient_retries += 1
                _tw.log.warning(
                    "planner transport transient for %s (attempt %d/%d): %s",
                    self.node.id,
                    transient_retries,
                    max_transient + 1,
                    str(e)[:300],
                )
                continue
            # Persist plan_text immediately so resumes can read what the
            # planner emitted on this attempt (mirrors the prior contract:
            # plan_text is the JSON the worker is consuming).
            self.plan_text = outcome.plan_text
            self.store.set_field(self.node.id, plan_text=self.plan_text)
            if outcome.parse_error_feedback is not None:
                if parse_retries >= _MAX_PARSE_ERROR_RETRIES:
                    note = (
                        f"planner output failed schema validation after "
                        f"{_MAX_PARSE_ERROR_RETRIES + 1} attempts: "
                        f"{outcome.parse_error_feedback[:500]}"
                    )
                    fsm_runtime.block_current(
                        self.store,
                        self.node.id,
                        note=note,
                        last_error=note[:1000],
                        failure_reason="planner_parse_failure",
                    )
                    raise RuntimeError(note)
                _tw.log.warning(
                    "planner output failed schema validation (attempt %d/%d); re-prompting",
                    parse_retries + 1,
                    _MAX_PARSE_ERROR_RETRIES + 1,
                )
                prior_attempt_notes = outcome.parse_error_feedback
                parse_retries += 1
                continue
            assert outcome.plan is not None
            # Validators are looked up via the module so tests can swap
            # them with `monkeypatch.setattr(planner_driver,
            # "validate_*", spy)` and have the driver pick the spy up.
            try:
                this_module = sys.modules[__name__]
                this_module.validate_rubric_coverage(outcome.plan, contract)
                this_module.validate_evidence_partition(outcome.plan, self.node)
                this_module.validate_standards_refs(outcome.plan, contract)
                this_module.validate_architecture_refs(outcome.plan, contract)
                this_module.validate_gauntlet_strategy(outcome.plan)
            except PlannerValidationError as ve:
                if validator_retries >= _MAX_VALIDATOR_RETRIES:
                    note = (
                        f"planner output failed validator after "
                        f"{_MAX_VALIDATOR_RETRIES + 1} attempts: {ve.message}"
                    )
                    fsm_runtime.block_current(
                        self.store,
                        self.node.id,
                        note=note,
                        last_error=note[:1000],
                        failure_reason=f"planner_validator_{ve.which}",
                    )
                    raise RuntimeError(note) from ve
                _tw.log.warning(
                    "planner output failed validator %r (attempt %d/%d); re-prompting",
                    ve.which,
                    validator_retries + 1,
                    _MAX_VALIDATOR_RETRIES + 1,
                )
                prior_attempt_notes = (
                    f"Your previous plan failed validator `{ve.which}`. "
                    f"Re-emit the COMPLETE plan correcting the following:\n\n"
                    f"{ve.message}"
                )
                validator_retries += 1
                continue
            return outcome.plan

    @staticmethod
    def _planner_phase(validator_retries: int, parse_retries: int) -> str:
        """Phase label used in agent-call records + log headers."""
        total = validator_retries + parse_retries
        if total == 0:
            return "planner"
        if parse_retries > 0 and validator_retries == 0:
            return f"planner_parse_retry_{parse_retries}"
        if validator_retries > 0 and parse_retries == 0:
            return f"planner_validator_retry_{validator_retries}"
        return f"planner_retry_{total}"

    def _invoke_planner_once(
        self: Any,
        contract: EvaluationContract,
        phase: str,
        prior_attempt_notes: str | None,
    ) -> _PlannerInvokeOutcome:
        """Single planner round-trip: agent.invoke + record + translate.

        Returns a `_PlannerInvokeOutcome` carrying either a translated
        runtime `Plan` OR a `parse_error_feedback` string. The caller
        decides whether to re-prompt or run validators.
        """
        agent = make_agent("planner", self.cfg)
        prompt = _tw.prompts.planner_prompt(
            self.cfg,
            self.dag,
            self.node,
            contract,
            prior_attempt_notes=prior_attempt_notes,
        )
        log_label = "PLANNER" if phase == "planner" else f"PLANNER ({phase})"
        self._write_log_header(log_label, prompt)
        call_id = self.store.record_agent_call_started(
            self.node.id,
            phase=phase,
            cli="json_agent",
            model=self.cfg.planner_model,
        )
        result = agent.invoke(
            prompt,
            handle=self._h,
            log_path=self.log_path,
            timeout=self.cfg.planner_timeout_s,
        )
        self.store.record_agent_call_finished(
            call_id,
            rc=result.rc,
            duration_s=result.duration_s or 0,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            cost_usd=result.cost_usd,
        )
        plan_text = result.raw_text or (
            result.structured.model_dump_json() if result.structured is not None else ""
        )
        if plan_text:
            self.store.add_artifact(self.node.id, "planner_output", plan_text)
        if result.rc != 0:
            if result.transient:
                raise _PlannerTransientFailure(
                    f"planner agent exited transient rc={result.rc}: {(result.stderr_excerpt or '')[:500]}"
                )
            raise RuntimeError(f"planner agent exited rc={result.rc}: {(result.stderr_excerpt or '')[:500]}")
        if result.parse_errors or result.structured is None:
            feedback = _build_parse_error_feedback(result.parse_errors)
            return _PlannerInvokeOutcome(plan=None, plan_text=plan_text, parse_error_feedback=feedback)
        if not isinstance(result.structured, PlannerOutput):
            # Defensive: registry binds "planner" to PlannerOutput; only
            # fires on a registry misconfiguration.
            raise RuntimeError(
                f"planner agent returned unexpected schema "
                f"{type(result.structured).__name__}; expected PlannerOutput"
            )
        try:
            plan = _wire_to_runtime_plan(
                result.structured,
                expected_node_id=self.node.id,
                spec_gate_command=self.cfg.local_ci_command,
                rubric_categories=list(self.cfg.pre_pr_rubric_categories or []),
                rubric_min_score=int(self.cfg.pre_pr_rubric_min_score),
            )
        except PlanValidationError as e:
            # Wire schema was valid but the runtime layer rejected it
            # (duplicate ids, unknown depends_on, cycle, node_id mismatch).
            # Treat as a parse-error class re-prompt: the wire schema can't
            # express these invariants, so the planner needs to re-emit.
            feedback = (
                "Your previous plan was structurally valid JSON but failed "
                "the runtime plan validators:\n\n"
                f"```\n{e}\n```\n\n"
                "Re-emit the COMPLETE plan correcting the issue (e.g. "
                "duplicate subtask ids, unknown `depends_on`, cycles, or "
                "a `node_id` that doesn't match the task you were planning)."
            )
            return _PlannerInvokeOutcome(plan=None, plan_text=plan_text, parse_error_feedback=feedback)
        return _PlannerInvokeOutcome(plan=plan, plan_text=plan_text, parse_error_feedback=None)


# Defensive: the wire schema imports stay live so the test suite's
# `tests/test_planner_validators_refs.py` (which constructs SubtaskSpec /
# StandardsRefSchema / etc. directly to drive the validators) keeps
# importing them through this module too.
_ = (
    ArchitectureRefSchema,
    RubricTargetSchema,
    StandardsRefSchema,
    SubtaskSpec,
)
