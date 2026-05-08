"""Plan 38 PR-B.2: progress-check agent on the JsonAgent layer.

The progress-check agent is invoked periodically inside `_subtask_loop`
when the doer/checker pair has been retrying without converging. Given
the last few attempts' checker root-causes and triage notes, it returns
one of three verdicts:

    - "progressing": root cause shifted, area narrowed → keep retrying.
    - "flatlined":   same root cause repeating → consecutive flatline
                     count bumps; on N flatlines in a row the worker
                     blocks the subtask.
    - "uncertain":   not enough signal yet → don't bump flatline count,
                     don't reset it either.

The agent is **advisory, not blocking**: any agent-side failure (timeout,
parse error, transient container glitch) collapses to `uncertain`, never
propagates as an exception. Worse than a missing signal is a crashed
worker.

PR-B.2: prose parsing (heuristic JSON extraction) is gone. The
agent runs through `make_agent("progress", cfg)`, which validates the
JSON envelope against the `ProgressVerdict` pydantic schema in the
JsonAgent layer. A schema-validation failure (`parse_errors` non-empty)
collapses to `uncertain` — preserving the existing fallback semantic at
the worker layer without re-introducing heuristics here.

The worker-facing public surface (`ProgressAgent.check(...)`,
`build_progress_agent(...)`, `ProgressAttempt`, `ProgressVerdict`) is
preserved verbatim so callers in `quikode.workers.task_worker` /
`quikode.workers.subtask_progress` need no change. The bridge between
the new pydantic `ProgressVerdict` (closed enum: `flatline`) and the
worker-facing dataclass (closed enum: `flatlined`) lives in `_to_worker_dataclass`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .. import prompts
from ..agent_registry import make_agent
from ..agent_schemas import ProgressVerdict as _PydanticProgressVerdict
from ..config import Config
from ..execution import ExecutionSandbox
from ..subtask_schema import Subtask

log = logging.getLogger("quikode.agents.progress")


@dataclass(frozen=True)
class ProgressAttempt:
    """One row in the attempt-history view fed to the progress agent."""

    attempt_no: int
    checker_root_cause: str
    triage_notes: str
    commit_sha_before: str | None = None


@dataclass(frozen=True)
class ProgressVerdict:
    """Worker-facing progress-check verdict.

    Distinct from `quikode.agent_schemas.ProgressVerdict` (the wire-level
    pydantic schema). The worker-facing dataclass keeps the closed-enum value
    `"flatlined"` because `quikode.workers.subtasks._maybe_record_progress_block`
    branches on that exact string. Plan 38's pydantic schema normalizes
    to `"flatline"`; `_to_worker_dataclass` bridges the two so the worker sees the
    worker-facing spelling without leaking the pydantic enum upward. Plan 38
    PR-B.5 will rewrite `prompts/progress.md` and reconcile the worker's
    consumer with the pydantic spelling — until then, the bridge is the
    single conversion site.
    """

    verdict: Literal["progressing", "flatlined", "uncertain"]
    rationale: str = ""


def _to_worker_dataclass(pyd: _PydanticProgressVerdict) -> ProgressVerdict:
    """Convert the pydantic schema instance to the worker-facing dataclass.

    The two are field-compatible except for the `"flatline"` ↔ `"flatlined"`
    spelling. `"progressing"` and `"uncertain"` map straight through. Any
    other value would have failed pydantic validation upstream, so this
    function only sees the closed enum.
    """
    if pyd.verdict == "flatline":
        return ProgressVerdict(verdict="flatlined", rationale=pyd.rationale)
    # "progressing" | "uncertain"
    return ProgressVerdict(verdict=pyd.verdict, rationale=pyd.rationale)


class ProgressAgent:
    """Wraps the JsonAgent layer for progress-check invocations.

    Built lazily on each `check()` (cheap; the JsonAgent layer also caches
    nothing), so swapping `cfg.progress_model` between calls is honored.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def check(
        self,
        *,
        subtask: Subtask,
        attempts: list[ProgressAttempt],
        acceptance: tuple[str, ...],
        handle: ExecutionSandbox,
        log_path: Path | None = None,
        timeout: int | None = None,
    ) -> ProgressVerdict:
        """Run the progress agent and return a parsed verdict.

        Failures (agent rc != 0, timeout, schema-validation parse error)
        collapse to `uncertain` so the worker can keep going. The progress
        check is advisory — a crash here must not break the subtask loop.

        `timeout` defaults to `cfg.progress_timeout_s`; callers may
        override (existing worker call passes `timeout=180`, which is
        also the cfg default).
        """
        try:
            prompt = prompts.progress_prompt(
                self.cfg,
                subtask,
                attempts=attempts,
                acceptance=acceptance,
            )
        except Exception as e:  # defensive; render shouldn't fail but if it does don't crash worker
            log.warning("progress agent prompt render failed: %s", e)
            return ProgressVerdict(
                verdict="uncertain",
                rationale=f"prompt render failed: {str(e)[:200]}",
            )

        effective_timeout = timeout if timeout is not None else self.cfg.progress_timeout_s
        try:
            agent = make_agent("progress", self.cfg)
            result = agent.invoke(
                prompt,
                handle=handle,
                log_path=log_path,
                timeout=effective_timeout,
            )
        except Exception as e:  # agent transient (docker died, etc.) → don't propagate
            log.warning("progress agent invocation raised: %s", e)
            return ProgressVerdict(
                verdict="uncertain",
                rationale=f"agent transient failure: {str(e)[:200]}",
            )

        if result.rc != 0:
            return ProgressVerdict(
                verdict="uncertain",
                rationale=f"agent rc={result.rc}; treating as uncertain",
            )

        if result.parse_errors or result.structured is None:
            rationale = (
                "; ".join(result.parse_errors)[:500]
                if result.parse_errors
                else "agent returned no structured output"
            )
            return ProgressVerdict(verdict="uncertain", rationale=rationale)

        if not isinstance(result.structured, _PydanticProgressVerdict):
            # Defensive: registry binds "progress" to ProgressVerdict, so this
            # branch only fires if the registry is misconfigured. Treat as
            # uncertain; surface enough context for an operator to debug.
            return ProgressVerdict(
                verdict="uncertain",
                rationale=(
                    "progress agent returned unexpected schema "
                    f"{type(result.structured).__name__}; expected ProgressVerdict"
                ),
            )

        return _to_worker_dataclass(result.structured)


def build_progress_agent(cfg: Config) -> ProgressAgent:
    """Factory mirror of `build_agent(role)` for the other phases."""
    return ProgressAgent(cfg)
