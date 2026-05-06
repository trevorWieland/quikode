"""Compatibility alias for the task worker implementation."""

from __future__ import annotations

import sys

from quikode import github
from quikode.workers import task_worker as _task_worker
from quikode.workers.task_worker import (
    TaskWorker,
    WorkerOutcome,
    _CheckerOutcome,
    _extract_root_cause,
    _last_lines,
    _parse_intent_verdict,
    _parse_verdict,
    _SubtaskPassOutcome,
)

__all__ = [
    "TaskWorker",
    "WorkerOutcome",
    "_CheckerOutcome",
    "_SubtaskPassOutcome",
    "_extract_root_cause",
    "_last_lines",
    "_parse_intent_verdict",
    "_parse_verdict",
    "github",
]

sys.modules[__name__] = _task_worker
