"""Agent interface — common shape across claude-code, codex, opencode."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Protocol

from ..docker_env import exec_in
from ..types import AgentResult

# Re-export so existing `from .base import AgentResult` still works.
__all__ = [
    "Agent",
    "AgentResult",
    "_is_quota_exhausted",
    "_is_transient_container_failure",
    "parse_tokens",
]

log = logging.getLogger("quikode.agents")


# Phrases that indicate a docker/container-level failure rather than a real
# agent-CLI failure. Anything in stderr matching these means "the box died,
# not the agent" → free retry.
_TRANSIENT_STDERR_MARKERS: tuple[str, ...] = (
    "Error response from daemon",
    "Cannot connect to the Docker daemon",
    "container not running",
    "context deadline exceeded",
)


# Patterns for "the agent CLI's subscription / account quota is exhausted"
# — distinct from a momentary transport-level rate limit. When detected,
# `_exec` sleeps with exponential backoff and retries the same call rather
# than surfacing the failure. This prevents the cascade where a quota'd
# doer turn still triggers checker + triage on an empty diff, burning more
# tokens (and possibly quota'ing those CLIs in turn). See plan 19A.
_QUOTA_EXHAUSTED_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Claude Code stderr: "You've hit your session limit · resets 3:45pm",
    # "weekly limit · resets Mon 12:00am", "Opus limit reached", etc.
    re.compile(r"you'?ve hit your (?:session|weekly|opus|5-?hour|usage) limit", re.IGNORECASE),
    # Codex JSONL: turn.failed / error with code: "rate_limit_exceeded"
    re.compile(r"\brate_limit_exceeded\b"),
    # opencode forwards upstream 429 from zai-coding-plan / anthropic / etc.
    re.compile(r"\b429\b"),
    # Generic "rate-limit / quota / usage-limit ... exceeded/reached/exhausted/hit"
    re.compile(
        r"(?:rate[ _-]?limit|quota|usage[ _-]?limit)[^\n]{0,80}?"
        r"(?:exceeded|reached|exhausted|hit)",
        re.IGNORECASE,
    ),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"insufficient[ _-]?quota", re.IGNORECASE),
)


def _is_quota_exhausted(rc: int, stdout: str, stderr: str) -> bool:
    """True when the agent CLI failure looks like a subscription-level quota
    hit, not a transient infra glitch. Only fires when rc != 0 — patterns
    like 'HTTP 429' can legitimately appear in successful agent output
    discussing rate-limit handling code. False on any zero rc.
    """
    if rc == 0:
        return False
    for blob in (stderr, stdout):
        if not blob:
            continue
        for pat in _QUOTA_EXHAUSTED_PATTERNS:
            if pat.search(blob):
                return True
    return False


def _quota_backoff_initial_s() -> int:
    """Initial sleep after the first quota-exhausted detection. Each subsequent
    retry doubles up to `_quota_backoff_max_s`. Tunable via env for testing.
    """
    return int(os.environ.get("QUIKODE_QUOTA_BACKOFF_INITIAL_S", "300"))


def _quota_backoff_max_s() -> int:
    """Cap on per-retry sleep. Default 30 min — fine-grained enough to catch
    a 5-hour window reset within ~30 min of it happening.
    """
    return int(os.environ.get("QUIKODE_QUOTA_BACKOFF_MAX_S", "1800"))


def _quota_max_total_wait_s() -> int:
    """Hard cap on cumulative wait inside one agent call. Default 8 h — covers
    one full claude/codex weekly bucket reset cycle plus margin. Past this
    we surface the failure rather than wait silently forever (a misconfigured
    auth token would otherwise pin the worker indefinitely).
    """
    return int(os.environ.get("QUIKODE_QUOTA_MAX_TOTAL_WAIT_S", "28800"))


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
        handle: Any,
        log_path: Path | None = None,
        timeout: int | None = None,
    ) -> AgentResult: ...


def _exec(
    handle: Any,
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

    On subscription-level quota exhaustion (5-hour bucket / weekly bucket
    / `rate_limit_exceeded`), this function does NOT return a failure —
    it sleeps with exponential backoff (5 min → 10 → 20 → 30 cap) and
    retries the same call. The FSM never sees a quota-exhausted result.
    This prevents the doer→checker→triage cascade where a quota'd doer
    turn still fires the next two roles on an empty diff. The cumulative
    wait is capped at `_quota_max_total_wait_s` (default 8 h); past that
    we surface the failure to keep a misconfigured auth from pinning the
    worker indefinitely.

    The per-call `timeout` parameter is unchanged: every `exec_in()`
    iteration inside the retry loop still gets its own fresh `timeout`,
    so a genuinely-hung agent CLI (no response in `timeout` seconds) is
    still killed via `subprocess.TimeoutExpired` and surfaces as a
    transient failure (rc=124, transient=True) — exactly as before. The
    quota-retry path and the per-call timeout watch DIFFERENT failure
    modes (slow no-response vs. fast quota error) and never overlap:
    quota detection only fires on a clean, fast `exec_in` return with
    rc != 0 and a matching pattern. A stuck agent never enters the
    quota loop.
    """
    t0 = time.time()
    backoff_s = _quota_backoff_initial_s()
    backoff_max_s = _quota_backoff_max_s()
    max_total_wait_s = _quota_max_total_wait_s()
    waited_s = 0
    quota_retries = 0
    while True:
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
                f"\n[quikode] container-level transient failure detected: rc={rc}; "
                f"treating as transient retry"
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
        if _is_quota_exhausted(rc, out, err):
            if waited_s >= max_total_wait_s:
                give_up = (
                    f"\n[quikode] quota wait exceeded {max_total_wait_s}s after "
                    f"{quota_retries} retries; surfacing as failure to release worker"
                )
                log.warning(give_up.strip())
                if log_path is not None:
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    with log_path.open("a") as f:
                        f.write(give_up + "\n")
                return AgentResult(
                    rc=rc,
                    stdout=out,
                    stderr=(err or "") + give_up,
                    tokens_used=parse_tokens(out, err),
                    duration_s=time.time() - t0,
                )
            sleep_s = backoff_s
            wait_msg = (
                f"\n[quikode] quota exhausted (rc={rc}); retry {quota_retries + 1}, "
                f"sleeping {sleep_s}s (cumulative {waited_s}s of {max_total_wait_s}s cap)"
            )
            log.warning(wait_msg.strip())
            if log_path is not None:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a") as f:
                    f.write(wait_msg + "\n")
            # The sleep below is intentionally OUTSIDE the per-call timeout.
            # `timeout` bounds individual `exec_in` calls (it kills a hung
            # agent CLI). Quota waiting is a separate concern: the agent
            # already returned quickly with an error message; we're holding
            # for the subscription bucket to refresh. Coupling the two would
            # mean a long sleep could trip the per-call timeout watchdog
            # (it can't here — `timeout` is only checked inside `exec_in`)
            # AND would lose the ability to kill genuinely-hung agents on
            # the next retry (we'd already have used the budget on sleep).
            # Keep the watchdogs orthogonal.
            time.sleep(sleep_s)
            waited_s += sleep_s
            backoff_s = min(backoff_s * 2, backoff_max_s)
            quota_retries += 1
            continue
        return AgentResult(
            rc=rc,
            stdout=out,
            stderr=err,
            tokens_used=parse_tokens(out, err),
            duration_s=time.time() - t0,
        )
