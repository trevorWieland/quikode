"""Subtask progress-check mixin."""

from __future__ import annotations

import sys
from typing import Any

from quikode.subtask_schema import Subtask


class _TaskWorkerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.workers.task_worker"], name)


_tw = _TaskWorkerGlobals()


class SubtaskProgressMixin:
    def _should_run_progress_check(self: Any, attempt: int) -> bool:
        """Decide whether the progress-check agent should fire at this attempt."""
        after = self.cfg.subtask_progress_check_after
        every = self.cfg.subtask_progress_check_every
        if attempt < after:
            return False
        if every <= 0:
            return attempt == after
        return (attempt - after) % every == 0

    def _run_progress_check(self: Any, subtask: Subtask, attempt: int) -> _tw.ProgressVerdict:
        """Run the progress-check agent and persist an audit row."""
        attempts = self._recent_attempt_history(subtask)
        agent = _tw.build_progress_agent(self.cfg)
        outcome = agent.check(
            subtask=subtask,
            attempts=attempts,
            acceptance=tuple(subtask.acceptance),
            handle=self._h,
            log_path=self.log_path,
            timeout=180,
        )
        self.store.record_progress_check(
            self.node.id,
            subtask.id,
            attempts_at_check=attempt,
            verdict=outcome.verdict,
            rationale=outcome.rationale,
        )
        _tw.log.info(
            "subtask %s/%s progress check at attempt %d: %s - %s",
            self.node.id,
            subtask.id,
            attempt,
            outcome.verdict,
            outcome.rationale[:200],
        )
        return outcome

    def _recent_attempt_history(self: Any, subtask: Subtask, n: int = 5) -> list[_tw.ProgressAttempt]:
        """Pull the last N checker root-cause and triage-note pairs."""
        checker_outputs = self.store.recent_subtask_checker_outputs(self.node.id, subtask.id, limit=n)
        sub = self.store.get_subtask(self.node.id, subtask.id) or {}
        triage_notes = str(sub.get("triage_notes") or "")
        attempts: list[_tw.ProgressAttempt] = []
        total = len(checker_outputs)
        for i, output in enumerate(checker_outputs):
            attempts.append(
                _tw.ProgressAttempt(
                    attempt_no=i + 1,
                    checker_root_cause=_tw._extract_root_cause(output),
                    triage_notes=triage_notes if i == total - 1 else "(earlier triage notes not retained)",
                )
            )
        return attempts
