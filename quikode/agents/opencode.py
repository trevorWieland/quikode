"""opencode headless wrapper.

Invocation:
  opencode run --dangerously-skip-permissions --dir /workspace [--model PROVIDER/MODEL]

opencode reads from stdin if no positional message is given, but `cat |` is the
documented pattern.
"""

from __future__ import annotations

from pathlib import Path

from ..docker_env import TaskContainer
from . import ccusage
from .base import AgentResult, _exec


class OpencodeAgent:
    name = "opencode"

    def __init__(self, model: str | None = None, extra_args: list[str] | None = None):
        self.model = model
        self.extra_args = list(extra_args or [])

    def run(
        self, prompt: str, *, handle: TaskContainer, log_path: Path | None = None, timeout: int | None = None
    ) -> AgentResult:
        cmd = ["bash", "-lc", self._shell_invocation()]
        # Snapshot ccusage totals before/after to attribute this call's
        # tokens + cost. Opencode emits no machine-parseable usage data
        # in headless mode, so ccusage is the only source.
        before = ccusage.fetch_session_stats("opencode", handle=handle)
        result = _exec(handle, cmd, stdin=prompt, log_path=log_path, timeout=timeout)
        after = ccusage.fetch_session_stats("opencode", handle=handle)
        delta = ccusage.snapshot_delta("opencode", before, after)
        if delta is not None and delta.total_tokens > 0:
            result = ccusage.merge_into_result(result, delta)
        return result

    def _shell_invocation(self) -> str:
        parts = ["opencode", "run", "--dangerously-skip-permissions", "--dir", "/workspace"]
        if self.model:
            parts += ["--model", self.model]
        parts += self.extra_args
        return "cat | " + " ".join(parts)
