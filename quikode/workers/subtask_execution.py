"""Subtask do/check/triage mixin (Plan 38 PR-B.5).

The per-subtask loop runs the JsonAgent layer end-to-end:

1. Doer (`subtask_doer`, writes-files) emits a `DoerEnvelope` (bookkeeping
   only — never graded). Schema-validation failure surfaces a
   `parse_failure` triage layer.
2. The worker captures the actual evidence by reading the worktree diff
   via `git diff HEAD --no-color` (status excerpt + unified diff).
3. Witness commands declared in `subtask.behavior_evidence_advanced` run
   inside the container; per-witness + per-subtask caps live in
   `quikode.workers.witness_runner`.
4. Checker (`subtask_checker`, JSON-mode) grades the diff against the
   subtask's rubric / standards / architecture / behavior contract. The
   DoerEnvelope is fed in labeled "doer self-report — informational only".
5. On checker `verdict="fail"`, triage (`subtask_triage`, JSON-mode) emits
   a `SubtaskTriageOutput` (failure_layer + root_cause + cites + teaching).

The Plan 33 SELF_AUDIT contract (parse-and-short-circuit) is gone; the
diff is the evidence and the LLM checker is the judgment. The cost is
one extra checker call per attempt vs. the prior fast-fail short-circuit;
this is acceptable because (a) the short-circuit was structurally
unreliable, (b) checker calls are cheap relative to doer calls, and (c)
JsonAgent schema enforcement guarantees structurally clean checker
output.

Plan 22 carry-forward stays: the next attempt's prompt receives the
prior attempt's `subtask_doer:<id>` artifact as `prior_doer_envelope`
context. Plan 23 same-signature stop-loss runs against the new
`failure_layer` values (`local_ci`, `rubric`, `standards`, `behavior`,
`parse_failure`, `transport`, `architecture`).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, ClassVar

from quikode import fsm_runtime
from quikode.agent_registry import make_agent
from quikode.agent_schemas import (
    DoerEnvelope,
    SubtaskCheckerFinding,
    SubtaskCheckerOutput,
    SubtaskTriageOutput,
)
from quikode.agents.transient_quota import _is_transient_container_failure
from quikode.state import SubtaskState
from quikode.subtask_schema import Subtask
from quikode.types import Verdict
from quikode.workers.outcomes import CheckerOutcome as _CheckerOutcome
from quikode.workers.witness_runner import run_scoped_witnesses


class _TaskWorkerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.workers.task_worker"], name)


_tw = _TaskWorkerGlobals()


@dataclass(frozen=True)
class _DoerCallResult:
    """Worker-side capture of one doer invocation.

    Carries the validated `DoerEnvelope` (or None on parse failure), the
    raw envelope JSON for artifact storage, and the parse-error tuple so
    the worker can surface `parse_failure` to triage if both attempts at
    schema validation failed.
    """

    envelope: DoerEnvelope | None
    raw_text: str
    parse_errors: tuple[str, ...]


class SubtaskExecutionMixin:
    # Cached state set by `_do_subtask`, consumed by `_check_subtask` /
    # `_triage_subtask` within the same attempt. Class-level None
    # defaults so `TaskWorker.__new__`-based test fixtures see safe
    # values without needing to call __init__; production code paths
    # always set them via `_cache_doer_state`.
    _last_doer_envelope: ClassVar[DoerEnvelope | None] = None
    _last_doer_parse_errors: ClassVar[tuple[str, ...]] = ()
    _last_diff_text: ClassVar[str] = ""

    def _do_subtask(self: Any, subtask: Subtask, attempt: int, triage_notes: str | None) -> None:
        fsm_runtime.enter_doing_subtask(self.store, self.node.id, note=f"{subtask.id} attempt {attempt}")
        self.store.update_subtask(self.node.id, subtask.id, state=SubtaskState.DOING.value)
        contract = self._evaluation_contract()
        prior_envelope = self._fetch_prior_doer_envelope(subtask, attempt)
        prompt = _tw.prompts.subtask_doer_prompt(
            self.cfg,
            self.node,
            subtask,
            contract,
            plan=self.plan,
            triage_notes=triage_notes,
            prior_doer_envelope=prior_envelope,
        )
        self._write_log_header(f"SUBTASK DOER {subtask.id} (attempt {attempt})", prompt)
        doer_result = self._run_doer_agent(subtask, prompt, attempt)
        self._cache_doer_state(subtask, doer_result)

    # ----- doer helpers -----

    def _run_doer_agent(self: Any, subtask: Subtask, prompt: str, attempt: int) -> _DoerCallResult:
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
        envelope: DoerEnvelope | None = None
        if isinstance(result.structured, DoerEnvelope):
            envelope = result.structured
        raw_text = result.raw_text or (envelope.model_dump_json(indent=2) if envelope is not None else "")
        artifact_body = envelope.model_dump_json(indent=2) if envelope is not None else (raw_text or "")
        self.last_doer_summary = (envelope.summary if envelope is not None else artifact_body)[-2000:]
        self.store.add_artifact(
            self.node.id,
            f"subtask_doer:{subtask.id}",
            artifact_body,
        )
        _ = attempt  # captured by the log header / artifact ts
        return _DoerCallResult(
            envelope=envelope,
            raw_text=raw_text,
            parse_errors=tuple(result.parse_errors or ()),
        )

    def _cache_doer_state(self: Any, subtask: Subtask, doer_result: _DoerCallResult) -> None:
        """Stash the validated envelope (or parse_errors), the worktree
        diff, and the witness results so `_check_subtask` and
        `_triage_subtask` read them without re-running the doer."""
        self._last_doer_envelope = doer_result.envelope
        self._last_doer_parse_errors = doer_result.parse_errors
        self._last_diff_text = self._compute_subtask_diff_excerpt()
        if doer_result.envelope is None:
            # Parse failure path: the JsonAgent layer already re-prompted
            # once. Surface empty witness results; `_check_subtask` will
            # synthesize a parse_failure FAIL outcome so triage runs.
            self._last_witness_results = {}
            return
        self._last_witness_results = self._run_scoped_witnesses(subtask)

    def _fetch_prior_doer_envelope(self: Any, subtask: Subtask, attempt: int) -> DoerEnvelope | None:
        """Plan 22 carry-forward: feed the next attempt the prior
        attempt's `DoerEnvelope` (was `ParsedSelfAudit` pre-Plan-38). The
        artifact stream stores the envelope JSON; we re-parse it via
        pydantic so the prompt template renders the structured fields."""
        if attempt <= 1:
            return None
        text = self.store.latest_subtask_doer_output(self.node.id, subtask.id)
        if not text:
            return None
        try:
            return DoerEnvelope.model_validate_json(text)
        except Exception:
            # Best-effort: a malformed prior artifact (e.g. pre-Plan-38
            # SELF_AUDIT prose persisted before Plan 38 deployed) just
            # degrades to no carry-forward rather than crashing the next
            # attempt.
            return None

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
        """Plan 38 PR-B.5: always invoke the LLM checker against the diff.

        Two paths:
        * The doer's envelope failed schema validation twice → synthesize
          a parse_failure FAIL outcome so triage runs without an LLM
          checker call (no diff to grade against besides the structural
          failure).
        * Otherwise: invoke the JSON-mode checker against the diff +
          witness output + DoerEnvelope (informational only).
        """
        fsm_runtime.enter_checking_subtask(self.store, self.node.id, note=subtask.id)
        self.store.update_subtask(self.node.id, subtask.id, state=SubtaskState.CHECKING.value)

        if self._last_doer_envelope is None:
            return self._synthesize_parse_failure_outcome(subtask)

        objective_outcome = self._run_subtask_check_command(subtask)
        if objective_outcome is not None:
            return objective_outcome

        return self._run_llm_subtask_checker(subtask)

    def _synthesize_parse_failure_outcome(self: Any, subtask: Subtask) -> _CheckerOutcome:
        """Doer envelope failed schema validation → fail-closed with
        `failure_layer=parse_failure`. The triage agent will see the
        parse_errors as the root cause."""
        errs = self._last_doer_parse_errors or ("DoerEnvelope failed schema validation",)
        details = "\n".join(errs)[:4000]
        text = (
            "VERDICT: FAIL\n"
            "ROOT_CAUSE: doer envelope failed schema validation; "
            "failure_layer=parse_failure.\n"
            f"DETAILS:\n{details}"
        )
        self.store.add_artifact(self.node.id, f"subtask_parse_failure:{subtask.id}", text)
        return _CheckerOutcome(
            verdict=Verdict.FAIL,
            checker_text=text,
            transient=False,
            rc=None,
            stderr="",
        )

    def _run_llm_subtask_checker(self: Any, subtask: Subtask) -> _CheckerOutcome:
        contract = self._evaluation_contract()
        envelope = self._last_doer_envelope
        agent = make_agent("subtask_checker", self.cfg)
        prompt = _tw.prompts.subtask_checker_prompt(
            self.cfg,
            self.node,
            subtask,
            contract,
            doer_envelope=envelope,
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
        cmd_str = (self.cfg.subtask_check_command or "").strip()
        if not cmd_str:
            return None
        _tw.log.info("subtask %s/%s: running objective check `%s`", self.node.id, subtask.id, cmd_str)
        try:
            rc, stdout, stderr = _tw.exec_in(
                self._h,
                ["bash", "-lc", f"cd /workspace && {cmd_str}"],
                log_path=self.log_path,
                timeout=self.cfg.subtask_check_timeout_s,
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

    def _triage_subtask(self: Any, subtask: Subtask, attempt: int, budget: int, checker_output: str) -> str:
        """Plan 38 PR-B.5: senior-engineer-tutoring-junior triage on the
        JsonAgent layer. Inputs are the targeted contract, the validated
        DoerEnvelope (if present), the checker's text, the diff. Output
        is rendered to a human-readable string for the next doer
        attempt's prompt context."""
        _ = attempt
        _ = budget  # retry budget is unused by the new prompt; kept for the call-site signature
        contract = self._evaluation_contract()
        envelope = self._last_doer_envelope
        agent = make_agent("subtask_triage", self.cfg)
        prompt = _tw.prompts.subtask_triage_prompt(
            self.cfg,
            self.node,
            subtask,
            contract,
            doer_envelope=envelope,
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
            return text
        triage_text = _render_triage_output_for_artifact(result.structured)
        self.store.add_artifact(self.node.id, f"subtask_triage:{subtask.id}", triage_text)
        return triage_text


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
