"""Agent interface — common shape across claude-code, codex, opencode."""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Protocol

from ..docker_env import TaskContainer, exec_in
from ..types import AgentResult

# Re-export so existing `from .base import AgentResult` still works.
__all__ = ["Agent", "AgentResult", "_is_transient_container_failure", "parse_tokens"]


# Phrases that indicate a docker/container-level failure rather than a real
# agent-CLI failure. Anything in stderr matching these means "the box died,
# not the agent" → free retry.
_TRANSIENT_STDERR_MARKERS: tuple[str, ...] = (
    "Error response from daemon",
    "Cannot connect to the Docker daemon",
    "container not running",
    "context deadline exceeded",
)


def _is_transient_container_failure(rc: int, stderr: str) -> bool:
    """Decide whether a non-zero exit looks like a container-infra glitch.

    Conservative call on rc=137: SIGKILL inside a container almost always
    means the OOM-killer reaped us mid-exec (the dominant cause in our
    workload). The alternative — an agent CLI that legitimately exits 137
    on its own — is rare; if it does happen, the next attempt's checker
    will catch the lack of forward progress and the progress-check agent
    (Phase A.3) will gate further retries. So we err on the side of
    "transient" for 137 even without a daemon-error stderr hint.
    """
    if rc == 0:
        return False
    if rc == 137:
        return True
    if not stderr:
        return False
    return any(marker in stderr for marker in _TRANSIENT_STDERR_MARKERS)


_CODEX_TOKENS_RE = re.compile(r"^\s*tokens used\s*\n\s*(\d[\d,]*)\s*$", re.MULTILINE)
_GENERIC_TOKENS_RE = re.compile(r"\b(?:total[_ ]?)?tokens?\b[^0-9]{0,20}(\d[\d,]*)", re.IGNORECASE)


def parse_tokens(stdout: str, stderr: str) -> int | None:
    """Best-effort token-count extraction across the three agents' output.

    Codex prints `tokens used\\n<N>` to stderr (we redirect codex's verbose
    stream to stderr in the wrapper). Claude/opencode don't emit reliably in
    text mode — we'll catch any obvious "tokens: N" pattern but otherwise
    return None.
    """
    for blob in (stderr, stdout):
        if not blob:
            continue
        m = _CODEX_TOKENS_RE.search(blob)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                pass
        m = _GENERIC_TOKENS_RE.search(blob)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


class Agent(Protocol):
    name: str

    def run(
        self,
        prompt: str,
        *,
        handle: TaskContainer,
        log_path: Path | None = None,
        timeout: int | None = None,
    ) -> AgentResult: ...


def _exec(
    handle: TaskContainer,
    cmd: list[str],
    stdin: str | None = None,
    log_path: Path | None = None,
    timeout: int | None = None,
) -> AgentResult:
    """Run an agent command, returning a structured result.

    Timeouts are converted into a synthetic AgentResult with rc=124 (the
    standard "timed out" exit code) instead of raising. This means the
    worker treats a hung agent as a failed attempt — triage runs, the
    subtask retry loop continues — rather than crashing the whole task.
    The subprocess.run call already SIGKILLs the docker exec; modern
    docker propagates that to the in-container process so the orphan
    risk is small.
    """
    t0 = time.time()
    try:
        rc, out, err = exec_in(handle, cmd, log_path=log_path, stdin=stdin, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        partial_out = (
            (e.stdout.decode("utf-8", errors="replace") if e.stdout else "")
            if isinstance(e.stdout, bytes)
            else (e.stdout or "")
        )
        partial_err = (
            (e.stderr.decode("utf-8", errors="replace") if e.stderr else "")
            if isinstance(e.stderr, bytes)
            else (e.stderr or "")
        )
        msg = f"\n[quikode] agent timed out after {timeout}s; treating as failed attempt"
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a") as f:
                f.write(msg + "\n")
        return AgentResult(
            rc=124,
            stdout=partial_out,
            stderr=(partial_err + msg).strip(),
            tokens_used=parse_tokens(partial_out, partial_err),
            duration_s=time.time() - t0,
            transient=True,
        )
    if _is_transient_container_failure(rc, err):
        annotation = (
            f"\n[quikode] container-level transient failure detected: rc={rc}; treating as transient retry"
        )
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a") as f:
                f.write(annotation + "\n")
        return AgentResult(
            rc=124,
            stdout=out,
            stderr=(err or "") + annotation,
            tokens_used=parse_tokens(out, err),
            duration_s=time.time() - t0,
            transient=True,
        )
    return AgentResult(
        rc=rc,
        stdout=out,
        stderr=err,
        tokens_used=parse_tokens(out, err),
        duration_s=time.time() - t0,
    )
