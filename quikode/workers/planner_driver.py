"""Plan 33: planner driver mixin.

Extracted from `quikode/workers/subtasks.py` so that file stays under
the 600-line architecture budget. Owns the path:

  PROVISIONING → PLANNING → planner agent → parse → validators → Plan

The planner is invoked through `_invoke_planner_with_validators` which:
1. Calls the planner agent.
2. Parses the output via `parse_planner_output` (one schema-level retry
   on pydantic ValidationError via `_parse_or_retry_plan`).
3. Runs the four Plan 33 validators. On failure, re-prompts up to twice
   with the validator message, then BLOCKs with
   `failure_reason="planner_validator_<which>"`.
"""

from __future__ import annotations

import sys
from typing import Any

from quikode import fsm_runtime
from quikode.planner_validators import (
    PlannerValidationError,
    validate_architecture_refs,
    validate_evidence_partition,
    validate_gauntlet_strategy,
    validate_rubric_coverage,
    validate_standards_refs,
)
from quikode.subtask_schema import Plan, PlanValidationError


class _TaskWorkerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.workers.task_worker"], name)


_tw = _TaskWorkerGlobals()


class PlannerDriverMixin:
    """Owns the planner-invocation surface. Mixed into SubtaskWorkerMixin
    so the existing `_plan` entry point on `TaskWorker` keeps its shape.
    """

    def _invoke_planner_with_validators(self: Any, contract: Any) -> Plan:
        """Run the planner, parse it, run the Plan 33 validators, re-prompt
        up to twice if a validator fails, then BLOCK with
        `failure_reason="planner_validator_<which>"`.

        The pydantic schema-level errors flow through `_parse_or_retry_plan`
        (one re-prompt, separate budget). Validator-level errors run on the
        already-parsed Plan and re-prompt with the validator message.
        """
        prior_attempt_notes: str | None = None
        max_validator_retries = 2
        for attempt_no in range(max_validator_retries + 1):
            stdout = self._run_planner_agent(
                contract,
                phase=("planner" if attempt_no == 0 else f"planner_validator_retry_{attempt_no}"),
                prior_attempt_notes=prior_attempt_notes,
            )
            self.plan_text = stdout
            self.store.set_field(self.node.id, plan_text=self.plan_text)
            plan = self._parse_or_retry_plan(stdout)
            try:
                validate_rubric_coverage(plan, contract)
                validate_evidence_partition(plan, self.node)
                validate_standards_refs(plan, contract)
                validate_architecture_refs(plan, contract)
                validate_gauntlet_strategy(plan)
            except PlannerValidationError as ve:
                if attempt_no >= max_validator_retries:
                    note = (
                        f"planner output failed validator after "
                        f"{max_validator_retries + 1} attempts: {ve.message}"
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
                    attempt_no + 1,
                    max_validator_retries + 1,
                )
                prior_attempt_notes = (
                    f"Your previous plan failed validator `{ve.which}`. "
                    f"Re-emit the COMPLETE plan correcting the following:\n\n"
                    f"{ve.message}"
                )
                continue
            return plan
        # Unreachable — the loop either returns or raises.
        raise RuntimeError("planner validator loop fell through unexpectedly")

    def _run_planner_agent(
        self: Any,
        contract: Any,
        *,
        phase: str,
        prior_attempt_notes: str | None,
    ) -> str:
        """Invoke the planner agent once, record the call, return stdout."""
        agent = _tw.build_agent(self.cfg.planner)
        prompt = _tw.prompts.planner_prompt(
            self.cfg,
            self.dag,
            self.node,
            contract,
            prior_attempt_notes=prior_attempt_notes,
        )
        log_label = "PLANNER" if phase == "planner" else f"PLANNER ({phase})"
        self._write_log_header(log_label, prompt)
        result = agent.run(prompt, handle=self._h, log_path=self.log_path, timeout=1800)
        self.store.record_agent_call(
            self.node.id,
            phase=phase,
            cli=self.cfg.planner.cli,
            model=self.cfg.planner.model,
            rc=result.rc,
            duration_s=result.duration_s or 0,
            tokens_used=result.tokens_used,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            tokens_cached_read=result.tokens_cached_read,
            tokens_cached_creation=result.tokens_cached_creation,
            cost_usd=result.cost_usd,
        )
        if not result.ok:
            raise RuntimeError(f"planner agent exited {result.rc}: {result.stderr[:500]}")
        self.store.add_artifact(self.node.id, "planner_output", result.stdout)
        return result.stdout

    def _parse_or_retry_plan(self: Any, stdout: str) -> Plan:
        """One pydantic-level retry. Validator-level retries are owned by
        `_invoke_planner_with_validators`; this layer only handles JSON-
        schema mismatches (the planner emitted a malformed `Plan` shape
        — extra/missing/typo'd fields)."""
        rubric_categories = list(self.cfg.pre_pr_rubric_categories or [])
        rubric_min_score = int(self.cfg.pre_pr_rubric_min_score)
        try:
            return _tw.parse_planner_output(
                stdout,
                expected_node_id=self.node.id,
                spec_gate_command=self.cfg.local_ci_command,
                rubric_categories=rubric_categories,
                rubric_min_score=rubric_min_score,
            )
        except PlanValidationError as e:
            _tw.log.warning("planner output failed validation (%s); re-prompting once", e)
            agent = _tw.build_agent(self.cfg.planner)
            contract = self._evaluation_contract()
            prompt = _tw.prompts.planner_prompt(
                self.cfg,
                self.dag,
                self.node,
                contract,
                prior_attempt_notes=(
                    f"Your prior output failed JSON-schema validation:\n\n"
                    f"```\n{e}\n```\n\n"
                    f"Re-emit a single fenced ```json ... ``` block that conforms "
                    f"strictly to the schema. No prose outside the fence other than "
                    f"a one-line preamble."
                ),
            )
            self._write_log_header("PLANNER (retry after validation error)", prompt)
            result = agent.run(prompt, handle=self._h, log_path=self.log_path, timeout=1800)
            self.store.record_agent_call(
                self.node.id,
                phase="planner_retry",
                cli=self.cfg.planner.cli,
                model=self.cfg.planner.model,
                rc=result.rc,
                duration_s=result.duration_s or 0,
                tokens_used=result.tokens_used,
                tokens_input=result.tokens_input,
                tokens_output=result.tokens_output,
                tokens_cached_read=result.tokens_cached_read,
                tokens_cached_creation=result.tokens_cached_creation,
                cost_usd=result.cost_usd,
            )
            self.store.add_artifact(self.node.id, "planner_output", result.stdout)
            return _tw.parse_planner_output(
                result.stdout,
                expected_node_id=self.node.id,
                spec_gate_command=self.cfg.local_ci_command,
                rubric_categories=rubric_categories,
                rubric_min_score=rubric_min_score,
            )
