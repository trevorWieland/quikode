"""Compatibility alias for the orchestration runner implementation."""

from __future__ import annotations

import sys

from quikode.orchestration import runner as _runner
from quikode.orchestration.runner import Orchestrator, _worktree_mtime

__all__ = ["Orchestrator", "_worktree_mtime"]

sys.modules[__name__] = _runner
