"""Subtask do/check/triage mixin (Plan 47).

The per-subtask loop runs the JsonAgent layer end-to-end:

1. Doer (`subtask_doer`, writes-files, no envelope) edits files in
   `/workspace`. Plan 47 retired the bookkeeping `DoerEnvelope`: the
   transport runs in plain-text mode (no `--output-schema` /
   `--json-schema`), the diff is the deliverable, and the doer's
   stdout is captured as a free-text artifact for briefing only.
2. The worker captures the evidence by reading the worktree diff via
   `git diff HEAD --no-color` (status excerpt + unified diff).
3. Witness commands declared in `subtask.behavior_evidence_advanced`
   run inside the container; per-witness + per-subtask caps live in
   `quikode.workers.witness_runner`. The runner reloads the worktree
   DAG before declaring `NO_COMMAND` per orientation §7 — there is no
   doer-envelope-derived fallback.
4. Checker (`subtask_checker`, JSON-mode) grades the diff against the
   subtask's rubric / standards / architecture / behavior contract.
5. On checker `verdict="fail"`, triage (`subtask_triage`, JSON-mode)
   emits a `SubtaskTriageOutput` (failure_layer + root_cause + cites +
   teaching).

Plan-22 carry-forward across attempts flows through `triage_notes`
(the structured triage output's `teaching_narrative` + cites),
plumbed into `subtask_doer_prompt` directly. Plan 23 same-signature
stop-loss runs against the new `failure_layer` values (`local_ci`,
`rubric`, `standards`, `behavior`, `parse_failure`, `transport`,
`architecture`).
"""

from __future__ import annotations

import json
import sys
from typing import Any, ClassVar

from quikode import fsm_runtime
from quikode.agent_registry import make_agent
from quikode.agent_schemas import (
    SubtaskCheckerFinding,
    SubtaskCheckerOutput,
    SubtaskTriageOutput,
)
from quikode.agents.transient_quota import _is_transient_container_failure
from quikode.state import SubtaskState
from quikode.subtask_schema import STABILIZATION_SUBTASK_ID, Subtask
from quikode.types import Verdict
from quikode.workers.outcomes import CheckerOutcome as _CheckerOutcome
from quikode.workers.witness_runner import run_scoped_witnesses


class _TaskWorkerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.workers.task_worker"], name)


_tw = _TaskWorkerGlobals()


_DOER_ARTIFACT_MAX = 20000


class SubtaskExecutionMixin:
    # Cached state set by `_do_subtask`, consumed by `_check_subtask` /
    # `_triage_subtask` within the same attempt. Class-level defaults
    # so `TaskWorker.__new__`-based test fixtures see safe values
    # without needing to call __init__; production code paths always
    # set them via `_cache_doer_state`.
    _last_diff_text: ClassVar[str] = ""

    def _do_subtask(self: Any, subtask: Subtask, attempt: int, triage_notes: str | None) -> None:
        fsm_runtime.enter_doing_subtask(self.store, self.node.id, note=f"{subtask.id} attempt {attempt}")
        self.store.update_subtask(self.node.id, subtask.id, state=SubtaskState.DOING.value)
        contract = self._evaluation_contract()
        prompt = _tw.prompts.subtask_doer_prompt(
            self.cfg,
            self.node,
            subtask,
            contract,
            plan=self.plan,
            triage_notes=triage_notes,
        )
        self._write_log_header(f"SUBTASK DOER {subtask.id} (attempt {attempt})", prompt)
        self._run_doer_agent(subtask, prompt, attempt)
        self._cache_doer_state(subtask)

    # ----- doer helpers -----

    def _run_doer_agent(self: Any, subtask: Subtask, prompt: str, attempt: int) -> None:
        """Plan 47: run the doer with no schema enforcement and persist
        the stdout tail as the `subtask_doer:<id>` artifact (plain
        text, not JSON). The diff in `/workspace` is the deliverable;
        this artifact exists for briefing/log purposes only."""
        agent = make_agent("subtask_doer", self.cfg)
        call_id = self.store.record_agent_call_started(
            self.node.id,
            phase="subtask_doer",
            cli="json_agent",
            model=self.cfg.subtask_doer_model,
            subtask_id=subtask.id,
        )
        result = agent.invoke(
            prompt,
            handle=self._h,
            log_path=self.log_path,
            timeout=self.cfg.subtask_doer_timeout_s,
        )
        self.store.record_agent_call_finished(
            call_id,
            rc=result.rc,
            duration_s=result.duration_s or 0,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            cost_usd=result.cost_usd,
        )
        artifact_body = (result.raw_text or "")[-_DOER_ARTIFACT_MAX:]
        self.last_doer_summary = artifact_body[-2000:]
        self.store.add_artifact(
            self.node.id,
            f"subtask_doer:{subtask.id}",
            artifact_body,
        )
        _ = attempt  # captured by the log header / artifact ts

    def _cache_doer_state(self: Any, subtask: Subtask) -> None:
        """Compute the worktree diff and run scoped witnesses so
        `_check_subtask` and `_triage_subtask` read them without
        re-running the doer."""
        self._last_diff_text = self._compute_subtask_diff_excerpt()
        self._last_witness_results = self._run_scoped_witnesses(subtask)

    def _compute_subtask_diff_excerpt(self: Any) -> str:
        """`git status --porcelain` (one-line summary of touched paths)
        plus the unified diff against HEAD. The doer's edits are
        uncommitted at this point (the orchestrator commits on settled),
        so HEAD is the right base."""
        try:
            rc_status, status_text = self._git_in_workspace(["status", "--porcelain"])
        except Exception as e:
            _tw.log.warning("subtask diff (status): %s", e)
            status_text = ""
            rc_status = -1
        try:
            rc_unified, unified = self._git_in_workspace(["diff", "HEAD", "--no-color"])
        except Exception as e:
            _tw.log.warning("subtask diff: %s", e)
            return ""
        if rc_unified != 0 or not unified:
            if rc_status == 0 and status_text:
                return f"git status --porcelain:\n{status_text}\n"
            return ""
        max_lines = 1500
        lines = unified.splitlines()
        if len(lines) > max_lines:
            head = lines[:max_lines]
            head.append(f"... (diff truncated; {len(lines) - max_lines} more lines)")
            unified = "\n".join(head)
        if rc_status == 0 and status_text:
            return f"git status --porcelain:\n{status_text}\n\n{unified}"
        return unified

    def _run_scoped_witnesses(self: Any, subtask: Subtask) -> dict[str, dict[str, Any]]:
        evidence_ids = list(subtask.behavior_evidence_advanced)
        if not evidence_ids:
            return {}
        try:
            results = run_scoped_witnesses(
                handle=self._h,
                expected_evidence=list(self.node.expected_evidence or ()),
                evidence_ids=evidence_ids,
                per_witness_timeout_s=int(self.cfg.subtask_witness_timeout_seconds),
                exec_in=_tw.exec_in,
                log_path=self.log_path,
            )
        except Exception as e:
            _tw.log.warning(
                "subtask %s/%s: scoped witness runner raised %s; degrading to empty results",
                self.node.id,
                subtask.id,
                e,
            )
            return {}
        try:
            self.store.add_artifact(
                self.node.id,
                f"subtask_witness_results:{subtask.id}",
                json.dumps(results, indent=2)[:20000],
            )
        except Exception as e:
            _tw.log.debug("subtask witness artifact persist failed: %s", e)
        return results

    # ----- checker -----

    def _check_subtask(self: Any, subtask: Subtask) -> _CheckerOutcome:
        """Plan 47: always invoke the LLM checker against the diff +
        witness output. The doer no longer emits a bookkeeping
        envelope, so there's nothing to short-circuit on; the worker
        runs the objective command first, and otherwise hands the
        diff to the checker."""
        fsm_runtime.enter_checking_subtask(self.store, self.node.id, note=subtask.id)
        self.store.update_subtask(self.node.id, subtask.id, state=SubtaskState.CHECKING.value)

        objective_outcome = self._run_subtask_check_command(subtask)
        if objective_outcome is not None:
            return objective_outcome

        return self._run_llm_subtask_checker(subtask)

    def _run_llm_subtask_checker(self: Any, subtask: Subtask) -> _CheckerOutcome:
        contract = self._evaluation_contract()
        agent = make_agent("subtask_checker", self.cfg)
        prompt = _tw.prompts.subtask_checker_prompt(
            self.cfg,
            self.node,
            subtask,
            contract,
            diff_text=self._last_diff_text,
            witness_results=self._last_witness_results,
        )
        self._write_log_header(f"SUBTASK CHECKER {subtask.id}", prompt)
        call_id = self.store.record_agent_call_started(
            self.node.id,
            phase="subtask_checker",
            cli="json_agent",
            model=self.cfg.subtask_checker_model,
            subtask_id=subtask.id,
        )
        result = agent.invoke(
            prompt,
            handle=self._h,
            log_path=self.log_path,
            timeout=self.cfg.subtask_checker_timeout_s,
        )
        self.store.record_agent_call_finished(
            call_id,
            rc=result.rc,
            duration_s=result.duration_s or 0,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            cost_usd=result.cost_usd,
        )
        if result.parse_errors or not isinstance(result.structured, SubtaskCheckerOutput):
            return self._checker_parse_failure(subtask, result)
        checker_text = _render_checker_output_for_artifact(result.structured)
        self.store.add_artifact(self.node.id, f"subtask_checker:{subtask.id}", checker_text)
        verdict = Verdict.PASS if result.structured.verdict == "pass" else Verdict.FAIL
        transient = bool(result.transient)
        if not transient and result.rc != 0 and (result.duration_s or 0) < 5:
            transient = True
        return _CheckerOutcome(
            verdict=verdict,
            checker_text=checker_text,
            transient=transient,
            rc=int(result.rc) if result.rc is not None else None,
            stderr=getattr(result, "stderr_excerpt", "") or "",
        )

    def _checker_parse_failure(self: Any, subtask: Subtask, result: Any) -> _CheckerOutcome:
        errs = list(result.parse_errors or ())
        if not errs and result.structured is not None:
            errs.append(
                f"checker returned unexpected schema {type(result.structured).__name__}; "
                "expected SubtaskCheckerOutput"
            )
        details = "; ".join(errs)[:4000]
        text = (
            "VERDICT: FAIL\n"
            "ROOT_CAUSE: subtask checker output failed schema validation; "
            "failure_layer=parse_failure.\n"
            f"DETAILS:\n{details}"
        )
        self.store.add_artifact(self.node.id, f"subtask_checker:{subtask.id}", text)
        transient = bool(getattr(result, "transient", False))
        return _CheckerOutcome(
            verdict=Verdict.FAIL,
            checker_text=text,
            transient=transient,
            rc=int(result.rc) if result.rc is not None else None,
            stderr=getattr(result, "stderr_excerpt", "") or "",
        )

    def _run_subtask_check_command(self: Any, subtask: Subtask) -> _CheckerOutcome | None:
        if subtask.id == STABILIZATION_SUBTASK_ID:
            cmd_str = (self.cfg.local_ci_command or "").strip()
            timeout_s = self.cfg.local_ci_timeout_s
        else:
            cmd_str = (self.cfg.subtask_check_command or "").strip()
            timeout_s = self.cfg.subtask_check_timeout_s
        if not cmd_str:
            return None
        _tw.log.info("subtask %s/%s: running objective check `%s`", self.node.id, subtask.id, cmd_str)
        try:
            rc, stdout, stderr = _tw.exec_in(
                self._h,
                ["bash", "-lc", f"cd /workspace && {cmd_str}"],
                log_path=self.log_path,
                timeout=timeout_s,
            )
        except (_tw.subprocess.TimeoutExpired, OSError) as e:
            _tw.log.warning(
                "subtask %s/%s: objective check raised %s; treating as transient",
                self.node.id,
                subtask.id,
                e,
            )
            return _CheckerOutcome(
                verdict=Verdict.FAIL,
                checker_text=f"subtask check command raised: {e}",
                transient=True,
                rc=124,
                stderr=str(e),
            )
        if rc == 0:
            return None
        blob = (stdout or "") + ("\n" + stderr if stderr else "")
        head = blob[:6000]
        synthesized = (
            f"VERDICT: FAIL\nROOT_CAUSE: objective subtask check `{cmd_str}` failed (rc={rc})\n"
            f"DETAILS:\n{head}"
        )
        self.store.add_artifact(self.node.id, f"subtask_objective_check:{subtask.id}", blob[:20000])
        is_transient = _is_transient_container_failure(rc, stderr or "") or _is_transient_container_failure(
            rc, blob
        )
        if is_transient:
            _tw.log.warning(
                "subtask %s/%s: objective check transient failure (rc=%d); container likely vanished",
                self.node.id,
                subtask.id,
                rc,
            )
        else:
            _tw.log.info(
                "subtask %s/%s: objective check FAILED (rc=%d, %d bytes of output)",
                self.node.id,
                subtask.id,
                rc,
                len(blob),
            )
        return _CheckerOutcome(
            verdict=Verdict.FAIL,
            checker_text=synthesized,
            transient=is_transient,
            rc=rc,
            stderr=stderr or "",
        )

    # ----- triage -----

    def _triage_subtask(
        self: Any, subtask: Subtask, attempt: int, budget: int, checker_output: str
    ) -> tuple[str, str | None]:
        """Plan 47: senior-engineer-tutoring-junior triage on the
        JsonAgent layer. Inputs are the targeted contract, the
        checker's text output, and the unified diff.

        Returns `(rendered_text, failure_layer)`. `failure_layer` is the
        structured layer from `SubtaskTriageOutput` (one of `local_ci`,
        `rubric`, `standards`, `architecture`, `behavior`,
        `parse_failure`, `transport`) when triage produced one;
        transport / parse-failure paths return `None` because no
        structured triage was emitted (plan 48 — the caller stamps a
        layer-less signature in that case rather than fabricating a
        layer)."""
        _ = attempt
        _ = budget  # retry budget is unused by the new prompt; kept for the call-site signature
        contract = self._evaluation_contract()
        agent = make_agent("subtask_triage", self.cfg)
        prompt = _tw.prompts.subtask_triage_prompt(
            self.cfg,
            self.node,
            subtask,
            contract,
            checker_verdict=checker_output,
            diff_text=self._last_diff_text,
        )
        self._write_log_header(f"SUBTASK TRIAGE {subtask.id} (attempt {attempt})", prompt)
        call_id = self.store.record_agent_call_started(
            self.node.id,
            phase="subtask_triage",
            cli="json_agent",
            model=self.cfg.subtask_triage_model,
            subtask_id=subtask.id,
        )
        result = agent.invoke(
            prompt,
            handle=self._h,
            log_path=self.log_path,
            timeout=self.cfg.subtask_triage_timeout_s,
        )
        self.store.record_agent_call_finished(
            call_id,
            rc=result.rc,
            duration_s=result.duration_s or 0,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            cost_usd=result.cost_usd,
        )
        if result.rc != 0 or result.transient:
            text = (
                "TRIAGE TRANSPORT FAILURE\n"
                "failure_layer: transport\n"
                "root_cause: triage agent transport failed before producing structured guidance\n"
                f"details: rc={result.rc}; {(result.stderr_excerpt or '')[:1000]}"
            )
            self.store.add_artifact(self.node.id, f"subtask_triage:{subtask.id}", text)
            return text, None
        if result.parse_errors or not isinstance(result.structured, SubtaskTriageOutput):
            errs = list(result.parse_errors or ())
            if not errs and result.structured is not None:
                errs.append(
                    f"triage returned unexpected schema {type(result.structured).__name__}; "
                    "expected SubtaskTriageOutput"
                )
            text = (
                "TRIAGE PARSE FAILURE\nfailure_layer: parse_failure\n"
                f"root_cause: triage agent output failed schema validation\n"
                f"details: {'; '.join(errs)[:1000]}"
            )
            self.store.add_artifact(self.node.id, f"subtask_triage:{subtask.id}", text)
            return text, None
        triage_text = _render_triage_output_for_artifact(result.structured)
        self.store.add_artifact(self.node.id, f"subtask_triage:{subtask.id}", triage_text)
        return triage_text, result.structured.failure_layer


def _render_checker_output_for_artifact(out: SubtaskCheckerOutput) -> str:
    """Render the structured checker output to the artifact text
    shape. Includes a `VERDICT: PASS|FAIL` line so the existing
    `_parse_verdict` helper still resolves on store reads, plus a
    `ROOT_CAUSE:` block for `_extract_root_cause` (used by the progress
    agent's attempt history)."""
    lines: list[str] = []
    lines.append(f"VERDICT: {out.verdict.upper()}")
    if out.overall_assessment:
        lines.append(f"ROOT_CAUSE: {out.overall_assessment[:600]}")
    if out.findings:
        lines.append("FINDINGS:")
        for f in out.findings:
            lines.append(_render_finding(f))
    return "\n".join(lines)


def _render_finding(f: SubtaskCheckerFinding) -> str:
    rationale = f" — {f.rationale}" if f.rationale else ""
    return f"  - [{f.verdict.upper()}] {f.category}{rationale}"


def _render_triage_output_for_artifact(out: SubtaskTriageOutput) -> str:
    """Render the structured triage output to a human-readable artifact
    string suitable for the next doer attempt's prompt context."""
    lines: list[str] = []
    lines.append(f"failure_layer: {out.failure_layer}")
    lines.append(f"root_cause: {out.root_cause}")
    if out.file_line_cites:
        lines.append("file_line_cites:")
        for cite in out.file_line_cites:
            lines.append(f"  - {cite}")
    if out.teaching_narrative:
        lines.append("teaching_narrative:")
        lines.append(out.teaching_narrative)
    return "\n".join(lines)
