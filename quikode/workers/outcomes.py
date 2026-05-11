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
    """Structured result from the checker path.

    Plan 59 fix E': `category` carries the transient-failure
    classification from the agent transport layer (`none`,
    `quota_exhausted`, `container_vanished`, `auth_refresh`) so the
    worker's `_record_transient_subtask_failure` can pick the
    category-aware sleep from `cfg.transient_retry_delays_s` instead
    of a one-size-fits-all `time.sleep(15)`.
    """

    verdict: Verdict
    checker_text: str
    transient: bool
    rc: int | None
    stderr: str
    category: str = "none"


@dataclass
class SubtaskPassOutcome:
    """Result of running the per-subtask commit and push gate."""

    kind: Literal["settled", "transient_retry", "fail"]
    synthesized_checker_text: str = ""
