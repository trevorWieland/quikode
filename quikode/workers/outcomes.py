"""Shared worker result types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from quikode.state import State
from quikode.types import Verdict


@dataclass
class WorkerOutcome:
    final_state: State
    note: str = ""


@dataclass
class CheckerOutcome:
    """Structured result from the checker path."""

    verdict: Verdict
    checker_text: str
    transient: bool
    rc: int | None
    stderr: str


@dataclass
class SubtaskPassOutcome:
    """Result of running the per-subtask commit and push gate."""

    kind: Literal["settled", "transient_retry", "fail"]
    synthesized_checker_text: str = ""
