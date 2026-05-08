"""Subtask do/check/triage mixin (Plan 33 PR-B).

The per-subtask loop executes:

1. Doer LLM call → emits diff + SELF_AUDIT block.
2. Parse SELF_AUDIT via `quikode.self_audit.parse_self_audit`. On parse
   error, re-prompt once with a targeted message; if the second attempt
   also fails, the subtask fails with `failure_layer="self_audit_mismatch"`.
3. Apply `short_circuit_decision`. If `FAIL_FAST`, skip the LLM checker
   and synthesize a FAIL `CheckerOutcome` so triage runs immediately.
4. Else: run scoped witnesses (`workers.witness_runner.run_scoped_witnesses`)
   for the subtask's `behavior_evidence_advanced` ids.
5. Invoke the LLM checker with `(contract, subtask, parsed_self_audit,
   diff_text, witness_results)`.
6. On checker FAIL: triage runs with the structured inputs and produces
   `triage_notes` for the next doer attempt.

The next doer attempt receives the prior `parsed_self_audit` (structured)
plus `triage_notes` (string) — replacing PR-A's loose
`prior_doer_output: str` plumbing.
"""

from __future__ import annotations

import sys
from typing import Any, ClassVar

from quikode import fsm_runtime
from quikode.agents.base import _is_transient_container_failure
from quikode.self_audit import (
    ParsedSelfAudit,
    ShortCircuit,
    parse_self_audit,
    short_circuit_decision,
)
from quikode.state import SubtaskState
from quikode.subtask_schema import Subtask
from quikode.types import Verdict
from quikode.workers.outcomes import CheckerOutcome as _CheckerOutcome
from quikode.workers.witness_runner import run_scoped_witnesses


class _TaskWorkerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.workers.task_worker"], name)


_tw = _TaskWorkerGlobals()


_SELF_AUDIT_REPROMPT_BANNER = (
    "Your SELF_AUDIT block was missing or malformed: {err}. "
    "Re-emit the SELF_AUDIT block in the exact format documented in §7 of "
    "the prior prompt. Do NOT change the diff. Do NOT re-investigate. "
    "Just print the corrected SELF_AUDIT block, in full, exactly once."
)


class SubtaskExecutionMixin:
    # Cached state set by `_do_subtask`, consumed by `_check_subtask` /
    # `_triage_subtask` within the same attempt. Class-level None
    # defaults so `TaskWorker.__new__`-based test fixtures see safe
    # values without needing to call __init__; production code paths
    # always set them via `_cache_doer_state`.
    _last_parsed_self_audit: ClassVar[ParsedSelfAudit | None] = None
    _last_self_audit_outcome: ClassVar[_CheckerOutcome | None] = None
    _last_diff_text: ClassVar[str] = ""

    def _do_subtask(self: Any, subtask: Subtask, attempt: int, triage_notes: str | None) -> None:
        fsm_runtime.enter_doing_subtask(self.store, self.node.id, note=f"{subtask.id} attempt {attempt}")
        self.store.update_subtask(self.node.id, subtask.id, state=SubtaskState.DOING.value)
        contract = self._evaluation_contract()
        prior_self_audit = self._fetch_prior_self_audit(subtask, attempt)
        prompt = _tw.prompts.subtask_doer_prompt(
            self.cfg,
            self.node,
            subtask,
            contract,
            plan=self.plan,
            triage_notes=triage_notes,
            prior_self_audit=prior_self_audit,
        )
        self._write_log_header(f"SUBTASK DOER {subtask.id} (attempt {attempt})", prompt)
        result = self._run_doer_agent(subtask, prompt, attempt)
        parsed = parse_self_audit(result.stdout)
        if parsed.parse_errors:
            parsed = self._handle_self_audit_parse_failure(subtask, attempt, parsed, result)
        self._cache_doer_state(subtask, parsed)

    # ----- doer helpers -----

    def _run_doer_agent(self: Any, subtask: Subtask, prompt: str, attempt: int) -> Any:
        agent = _tw.build_agent(self.cfg.doer)
        result = agent.run(
            prompt, handle=self._h, log_path=self.log_path, timeout=self.cfg.subtask_doer_timeout_s
        )
        self.store.record_agent_call(
            self.node.id,
            phase="subtask_doer",
            cli=self.cfg.doer.cli,
            model=self.cfg.doer.model,
            rc=result.rc,
            duration_s=result.duration_s or 0,
            tokens_used=result.tokens_used,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            tokens_cached_read=result.tokens_cached_read,
            tokens_cached_creation=result.tokens_cached_creation,
            cost_usd=result.cost_usd,
            subtask_id=subtask.id,
        )
        self.last_doer_summary = (result.stdout or "")[-2000:]
        self.store.add_artifact(
            self.node.id,
            f"subtask_doer:{subtask.id}",
            result.stdout or "",
        )
        _ = attempt  # attempt is captured by the log header / artifact ts
        return result

    def _handle_self_audit_parse_failure(
        self: Any,
        subtask: Subtask,
        attempt: int,
        parsed: ParsedSelfAudit,
        first_result: Any,
    ) -> ParsedSelfAudit:
        """Plan 33 §6.3: re-prompt once with a targeted message. Second
        failure surfaces as a synthesized FAIL outcome with
        `failure_layer="self_audit_mismatch"` (recorded on the cache so
        `_check_subtask` returns it without invoking the LLM checker)."""
        first_err = parsed.parse_errors[0] if parsed.parse_errors else "unknown parse error"
        _tw.log.warning(
            "subtask %s/%s attempt %d: SELF_AUDIT parse failed (%s); re-prompting once",
            self.node.id,
            subtask.id,
            attempt,
            first_err,
        )
        contract = self._evaluation_contract()
        reprompt = _SELF_AUDIT_REPROMPT_BANNER.format(err=first_err)
        # Build a focused re-prompt: original prompt unchanged is too
        # large; just send the targeted ask plus the original SELF_AUDIT
        # format spec is already in the doer's context within the same
        # session. Use `subtask_doer_prompt` with `triage_notes=reprompt`
        # so the doer sees the structured banner under §5.
        prompt = _tw.prompts.subtask_doer_prompt(
            self.cfg,
            self.node,
            subtask,
            contract,
            plan=self.plan,
            triage_notes=reprompt,
            prior_self_audit=parsed,
        )
        self._write_log_header(f"SUBTASK DOER {subtask.id} (attempt {attempt}, SELF_AUDIT re-prompt)", prompt)
        result = self._run_doer_agent(subtask, prompt, attempt)
        re_parsed = parse_self_audit(result.stdout)
        if re_parsed.parse_errors:
            _tw.log.warning(
                "subtask %s/%s attempt %d: SELF_AUDIT re-prompt also failed (%s); fail-fast",
                self.node.id,
                subtask.id,
                attempt,
                re_parsed.parse_errors[0],
            )
        _ = first_result  # the original result is preserved on disk; nothing more to do
        return re_parsed

    def _cache_doer_state(self: Any, subtask: Subtask, parsed: ParsedSelfAudit) -> None:
        """Stash parser output + computed short-circuit + diff + witnesses
        so `_check_subtask` and `_triage_subtask` can read them without
        re-running the doer."""
        self._last_parsed_self_audit = parsed
        self._last_diff_text = self._compute_subtask_diff_excerpt()
        # Persist the parsed audit for the next attempt's prior_self_audit
        # carry-forward (Plan 22 → Plan 33 D14).
        self.store.add_artifact(
            self.node.id,
            f"subtask_self_audit:{subtask.id}",
            self._render_parsed_audit_for_artifact(parsed),
        )
        if parsed.parse_errors:
            err = parsed.parse_errors[0] if parsed.parse_errors else "self_audit unparseable"
            self._last_self_audit_outcome = _CheckerOutcome(
                verdict=Verdict.FAIL,
                checker_text=(
                    "VERDICT: FAIL\nROOT_CAUSE: SELF_AUDIT block missing or malformed "
                    f"after re-prompt; failure_layer=self_audit_mismatch.\nDETAILS:\n{err}"
                ),
                transient=False,
                rc=None,
                stderr="",
            )
            self._last_witness_results = {}
            return
        contract = self._evaluation_contract()
        decision = short_circuit_decision(
            parsed,
            contract=contract,
            subtask=subtask,
            rubric_min_score=int(self.cfg.pre_pr_rubric_min_score),
        )
        if decision.decision is ShortCircuit.FAIL_FAST:
            self._last_self_audit_outcome = _CheckerOutcome(
                verdict=Verdict.FAIL,
                checker_text=(
                    f"VERDICT: FAIL\nROOT_CAUSE: short-circuit "
                    f"failure_layer={decision.failure_layer}.\nDETAILS:\n{decision.reason}"
                ),
                transient=False,
                rc=None,
                stderr="",
            )
            self._last_witness_results = {}
            return
        # PROCEED: run scoped witnesses now so `_check_subtask` sees them.
        self._last_self_audit_outcome = None
        self._last_witness_results = self._run_scoped_witnesses(subtask)

    def _fetch_prior_self_audit(self: Any, subtask: Subtask, attempt: int) -> ParsedSelfAudit | None:
        """Plan 22 + Plan 33 D14: feed the next attempt the structured
        prior SELF_AUDIT, not loose stdout. Read from the most recent
        `subtask_self_audit:<id>` artifact and re-parse it."""
        if attempt <= 1:
            return None
        # Look up the most recent persisted artifact and re-parse.
        text = self.store.latest_subtask_doer_output(self.node.id, subtask.id)
        if not text:
            return None
        # Even with parse_errors we hand back the partial — the prompt
        # renders the structured fields it can show and the doer learns
        # what structurally failed last time.
        return parse_self_audit(text)

    @staticmethod
    def _render_parsed_audit_for_artifact(parsed: ParsedSelfAudit) -> str:
        """A compact rendering used as the artifact body. Re-parsing this
        artifact is unnecessary; the source-of-truth doer artifact still
        carries the verbatim block. This artifact is mostly a forensic
        breadcrumb for `qk show`."""
        lines: list[str] = []
        lines.append(f"gate_local_ci: rc={parsed.gate_local_ci_rc} (cmd: {parsed.gate_local_ci_cmd})")
        lines.append("gate_rubric:")
        for cat, row in parsed.gate_rubric.items():
            lines.append(
                f"  {cat}: predicted_score={row.predicted_score}  "
                f"rationale: {row.rationale}  evidence: {row.evidence}"
            )
        lines.append("gate_standards:")
        for key, srow in parsed.gate_standards.items():
            lines.append(f"  {key}: {srow.body}")
        lines.append("gate_behavior:")
        for evid, brow in parsed.gate_behavior.items():
            lines.append(f"  {evid}: witnessed_by={brow.witnessed_by}  output_excerpt={brow.output_excerpt}")
        lines.append("diff_reconcile:")
        for fpath, status in parsed.diff_reconcile.items():
            lines.append(f"  {fpath}: {status}")
        if parsed.parse_errors:
            lines.append("# parse_errors:")
            for err in parsed.parse_errors:
                lines.append(f"#   - {err}")
        return "\n".join(lines)

    def _compute_subtask_diff_excerpt(self: Any) -> str:
        """`git diff HEAD --stat` + a capped unified diff against HEAD.
        The doer's edits are still uncommitted at this point (the
        orchestrator commits on settled), so HEAD is the right base."""
        try:
            rc_unified, unified = self._git_in_workspace(["diff", "HEAD", "--no-color"])
        except Exception as e:
            _tw.log.warning("subtask diff: %s", e)
            return ""
        if rc_unified != 0 or not unified:
            return ""
        max_lines = 1500
        lines = unified.splitlines()
        if len(lines) > max_lines:
            head = lines[:max_lines]
            head.append(f"... (diff truncated; {len(lines) - max_lines} more lines)")
            return "\n".join(head)
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
        # Forensic artifact for `qk show`.
        try:
            self.store.add_artifact(
                self.node.id,
                f"subtask_witness_results:{subtask.id}",
                _tw.json.dumps(results, indent=2)[:20000],
            )
        except Exception as e:
            _tw.log.debug("subtask witness artifact persist failed: %s", e)
        return results

    # ----- checker -----

    def _check_subtask(self: Any, subtask: Subtask) -> _CheckerOutcome:
        """Plan 33 PR-B: short-circuit before any LLM call when the
        cached `_last_self_audit_outcome` already carries a fast-fail
        verdict. Otherwise run the objective gate + LLM checker."""
        fsm_runtime.enter_checking_subtask(self.store, self.node.id, note=subtask.id)
        self.store.update_subtask(self.node.id, subtask.id, state=SubtaskState.CHECKING.value)

        if self._last_self_audit_outcome is not None:
            # Short-circuit / parse-fail path. Skip the objective gate
            # too — triage will name the failure layer; running the gate
            # here would just burn time on a path that's already failed.
            outcome = self._last_self_audit_outcome
            self.store.add_artifact(
                self.node.id,
                f"subtask_short_circuit:{subtask.id}",
                outcome.checker_text,
            )
            return outcome

        objective_outcome = self._run_subtask_check_command(subtask)
        if objective_outcome is not None:
            return objective_outcome

        return self._run_llm_subtask_checker(subtask)

    def _run_llm_subtask_checker(self: Any, subtask: Subtask) -> _CheckerOutcome:
        contract = self._evaluation_contract()
        parsed = self._last_parsed_self_audit
        if parsed is None:
            # Defensive: should never happen since `_do_subtask` always
            # sets it. If it does, we synthesize a structurally-empty
            # parse so the prompt still renders.
            parsed = ParsedSelfAudit(raw="", parse_errors=("internal: parsed audit not cached",))
        agent = _tw.build_agent(self.cfg.checker)
        prompt = _tw.prompts.subtask_checker_prompt(
            self.cfg,
            self.node,
            subtask,
            contract,
            self_audit=parsed,
            diff_text=self._last_diff_text,
            witness_results=self._last_witness_results,
        )
        self._write_log_header(f"SUBTASK CHECKER {subtask.id}", prompt)
        result = agent.run(
            prompt, handle=self._h, log_path=self.log_path, timeout=self.cfg.subtask_checker_timeout_s
        )
        self.store.record_agent_call(
            self.node.id,
            phase="subtask_checker",
            cli=self.cfg.checker.cli,
            model=self.cfg.checker.model,
            rc=result.rc,
            duration_s=result.duration_s or 0,
            tokens_used=result.tokens_used,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            tokens_cached_read=result.tokens_cached_read,
            tokens_cached_creation=result.tokens_cached_creation,
            cost_usd=result.cost_usd,
            subtask_id=subtask.id,
        )
        self.store.add_artifact(self.node.id, f"subtask_checker:{subtask.id}", result.stdout)
        transient = bool(result.transient)
        if (
            not transient
            and result.rc != 0
            and (result.duration_s or 0) < 5
            and "VERDICT:" not in (result.stdout or "")
        ):
            transient = True
        return _CheckerOutcome(
            verdict=_tw._parse_verdict(result.stdout),
            checker_text=result.stdout or "",
            transient=transient,
            rc=int(result.rc) if result.rc is not None else None,
            stderr=getattr(result, "stderr", "") or "",
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
        """Plan 33 PR-B: senior-engineer-tutoring-junior triage. Inputs
        are the structured `ParsedSelfAudit`, the checker's verdict,
        and the unified diff."""
        _ = attempt
        _ = budget  # retry budget is unused by the new prompt; kept for the call-site signature
        contract = self._evaluation_contract()
        parsed = self._last_parsed_self_audit or ParsedSelfAudit(
            raw="", parse_errors=("internal: parsed audit not cached for triage",)
        )
        agent = _tw.build_agent(self.cfg.triage)
        prompt = _tw.prompts.subtask_triage_prompt(
            self.cfg,
            self.node,
            subtask,
            contract,
            self_audit=parsed,
            checker_verdict=checker_output,
            diff_text=self._last_diff_text,
        )
        self._write_log_header(f"SUBTASK TRIAGE {subtask.id} (attempt {attempt})", prompt)
        result = agent.run(
            prompt, handle=self._h, log_path=self.log_path, timeout=self.cfg.subtask_checker_timeout_s
        )
        self.store.record_agent_call(
            self.node.id,
            phase="subtask_triage",
            cli=self.cfg.triage.cli,
            model=self.cfg.triage.model,
            rc=result.rc,
            duration_s=result.duration_s or 0,
            tokens_used=result.tokens_used,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            tokens_cached_read=result.tokens_cached_read,
            tokens_cached_creation=result.tokens_cached_creation,
            cost_usd=result.cost_usd,
            subtask_id=subtask.id,
        )
        self.store.add_artifact(self.node.id, f"subtask_triage:{subtask.id}", result.stdout)
        return result.stdout
