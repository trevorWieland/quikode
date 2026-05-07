"""v3 Phase A: progress-check agent.

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

Defaults to codex gpt-5.4-mini (configurable via `cfg.progress.cli/model`).
A lightweight verdict role — the input is short (last few attempts'
checker root-causes + triage notes) and the decision is shallow.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from .. import prompts
from ..config import AgentRole, Config
from ..execution import ExecutionSandbox
from ..subtask_schema import Subtask
from . import build_agent

log = logging.getLogger("quikode.agents.progress")


@dataclass(frozen=True)
class ProgressAttempt:
    """One row in the attempt-history view fed to the progress agent."""

    attempt_no: int
    checker_root_cause: str
    triage_notes: str
    commit_sha_before: str | None = None


class ProgressVerdict(BaseModel):
    """Structured output of one progress-check invocation."""

    model_config = ConfigDict(frozen=True)

    verdict: Literal["progressing", "flatlined", "uncertain"] = Field(
        description="Whether the subtask is converging, repeating, or unclear."
    )
    rationale: str = Field(
        default="",
        description="One-line explanation; surfaces in the progress_checks audit row.",
    )


class ProgressAgent:
    """Wraps the configured agent CLI for progress-check invocations.

    The agent is built lazily on each `check()` so swapping
    `cfg.progress` between calls is honored. Cheap models, short inputs.
    """

    def __init__(self, cfg: Config, role: AgentRole | None = None):
        self.cfg = cfg
        self.role = role or cfg.progress

    def check(
        self,
        *,
        subtask: Subtask,
        attempts: list[ProgressAttempt],
        acceptance: tuple[str, ...],
        handle: ExecutionSandbox,
        log_path: Path | None = None,
        timeout: int = 180,
    ) -> ProgressVerdict:
        """Run the agent and return a parsed verdict.

        Failures (agent rc != 0, timeout, parse failure) collapse to
        `uncertain` so the worker can keep going. The progress check is
        advisory — a crash here must not break the subtask loop.
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

        try:
            agent = build_agent(self.role)
            result = agent.run(prompt, handle=handle, log_path=log_path, timeout=timeout)
        except Exception as e:  # agent transient (docker died, etc.) → don't propagate
            log.warning("progress agent invocation raised: %s", e)
            return ProgressVerdict(
                verdict="uncertain",
                rationale=f"agent transient failure: {str(e)[:200]}",
            )

        if not result.ok:
            return ProgressVerdict(
                verdict="uncertain",
                rationale=f"agent rc={result.rc}; treating as uncertain",
            )

        return _parse_progress_output(result.stdout)


def build_progress_agent(cfg: Config) -> ProgressAgent:
    """Factory mirror of `build_agent(role)` for the other phases."""
    return ProgressAgent(cfg)


# JSON object regex — claude-code envelopes occasionally include leading
# preamble lines despite the prompt; we extract the first balanced JSON
# object found in the output.
_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\"verdict\"[^{}]*\}", re.DOTALL)


def _parse_progress_output(text: str) -> ProgressVerdict:
    """Parse the agent's JSON envelope into a typed verdict.

    On any parse failure, fall back to `uncertain` with a rationale citing
    the first 200 chars of the raw output. This includes the case where
    the agent ignored the JSON instruction and emitted prose, or
    truncated mid-token.
    """
    if not text or not text.strip():
        return ProgressVerdict(verdict="uncertain", rationale="failed to parse: empty output")

    snippet = text.strip()
    # Try direct parse first (cheapest path).
    try:
        data = json.loads(snippet)
    except json.JSONDecodeError:
        m = _JSON_OBJECT_RE.search(snippet)
        if not m:
            return ProgressVerdict(
                verdict="uncertain",
                rationale=f"failed to parse: {snippet[:200]}",
            )
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return ProgressVerdict(
                verdict="uncertain",
                rationale=f"failed to parse: {snippet[:200]}",
            )

    if not isinstance(data, dict):
        return ProgressVerdict(
            verdict="uncertain",
            rationale=f"failed to parse: not an object: {snippet[:200]}",
        )

    verdict_raw = str(data.get("verdict", "")).strip().lower()
    if verdict_raw not in {"progressing", "flatlined", "uncertain"}:
        return ProgressVerdict(
            verdict="uncertain",
            rationale=f"unknown verdict {verdict_raw!r}; raw: {snippet[:200]}",
        )

    rationale = str(data.get("rationale") or "")[:500]
    verdict = cast(Literal["progressing", "flatlined", "uncertain"], verdict_raw)
    return ProgressVerdict(verdict=verdict, rationale=rationale)
