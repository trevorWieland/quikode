"""Subtask do/check/triage mixin."""

from __future__ import annotations

import sys
from typing import Any

from quikode import fsm_runtime
from quikode.state import SubtaskState
from quikode.subtask_schema import Subtask
from quikode.types import Verdict
from quikode.workers.outcomes import CheckerOutcome as _CheckerOutcome


class _TaskWorkerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.workers.task_worker"], name)


_tw = _TaskWorkerGlobals()


class SubtaskExecutionMixin:
    def _do_subtask(self: Any, subtask: Subtask, attempt: int, triage_notes: str | None) -> None:
        fsm_runtime.enter_doing_subtask(self.store, self.node.id, note=f"{subtask.id} attempt {attempt}")
        self.store.update_subtask(self.node.id, subtask.id, state=SubtaskState.DOING.value)
        agent = _tw.build_agent(self.cfg.doer)
        prompt = _tw.prompts.subtask_doer_prompt(self.cfg, self.node, subtask, triage_notes=triage_notes)
        self._write_log_header(f"SUBTASK DOER {subtask.id} (attempt {attempt})", prompt)
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
        self.last_doer_summary = result.stdout[-2000:]
        self.store.add_artifact(self.node.id, f"subtask_doer:{subtask.id}", result.stdout)

    def _check_subtask(self: Any, subtask: Subtask) -> _CheckerOutcome:
        """Run objective and LLM subtask checks."""
        fsm_runtime.enter_checking_subtask(self.store, self.node.id, note=subtask.id)
        self.store.update_subtask(self.node.id, subtask.id, state=SubtaskState.CHECKING.value)

        objective_outcome = self._run_subtask_check_command(subtask)
        if objective_outcome is not None:
            return objective_outcome

        agent = _tw.build_agent(self.cfg.checker)
        prompt = _tw.prompts.subtask_checker_prompt(self.cfg, self.node, subtask)
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
            transient=False,
            rc=rc,
            stderr=stderr or "",
        )

    def _triage_subtask(self: Any, subtask: Subtask, attempt: int, budget: int, checker_output: str) -> str:
        agent = _tw.build_agent(self.cfg.triage)
        prompt = _tw.prompts.subtask_triage_prompt(
            self.cfg,
            self.node,
            subtask,
            retry_count=attempt,
            retry_budget=budget,
            checker_output=checker_output,
            recent_doer_summary=self.last_doer_summary,
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
