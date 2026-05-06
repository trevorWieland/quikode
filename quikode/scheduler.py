"""Compatibility alias for scheduler helpers."""

from __future__ import annotations

import sys

from quikode.orchestration import scheduler as _scheduler
from quikode.orchestration.scheduler import (
    STACK_READY_STATES,
    collect_pick_candidates,
    is_parent_stack_ready,
    score_candidate,
)

__all__ = [
    "STACK_READY_STATES",
    "collect_pick_candidates",
    "is_parent_stack_ready",
    "score_candidate",
]

sys.modules[__name__] = _scheduler
