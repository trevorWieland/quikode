"""Exponential backoff for `gh` / `git` / `gh api graphql` subprocess calls.

GitHub's secondary rate limits and transient network errors look identical to
"real" failures from `subprocess.run(check=False)` — both come back as rc != 0
with stderr text. Without a backoff layer the orchestrator's polling loops
hammer the same failing endpoint at the polling cadence, burning the rate
budget faster than it refills.

This helper:
  - Runs the subprocess.
  - Classifies the result as `ok`, `transient` (retry with backoff), or `hard`
    (don't retry — surface the failure).
  - On `transient`, sleeps base_delay × 2^attempt and tries again, up to
    `retries` retries (so 4 attempts total by default).
  - Returns the final CompletedProcess so call sites get the same shape they
    had before — no behavior change on success or hard failure.

Default `gh_classifier` covers GitHub rate-limit signatures and common
network errors. `git_classifier` covers git-protocol transients without
treating non-fast-forward / lease-stale as transient.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger("quikode.net_retry")

ClassifyVerdict = Literal["ok", "transient", "hard"]
Classifier = Callable[[subprocess.CompletedProcess], ClassifyVerdict]


# 429 / "secondary rate limit" / abuse detection — gh CLI surfaces these in
# stderr. Network errors land as rc != 0 with curl-style messages.
_GH_TRANSIENT_RE = re.compile(
    r"(429|secondary rate limit|api rate limit exceeded|"
    r"abuse detection|server error \(5\d\d\)|"
    r"could not resolve host|connection refused|connection timed out|"
    r"operation timed out|tls handshake timeout|temporary failure in name resolution|"
    r"i/o timeout|network is unreachable|gateway timeout)",
    re.IGNORECASE,
)

# Hard errors we should NOT retry. These are real not-here / not-allowed
# answers; retrying just produces the same hard answer.
_GH_HARD_RE = re.compile(
    r"(not found|resource not accessible|requires authentication|"
    r"403 forbidden|401 unauthorized|404|"
    r"validation failed|already exists)",
    re.IGNORECASE,
)

_GIT_TRANSIENT_RE = re.compile(
    r"(could not resolve host|connection (refused|timed out|reset)|"
    r"operation timed out|early eof|rpc failed|tls connection|"
    r"failed to connect to|remote end hung up unexpectedly|"
    r"unable to access)",
    re.IGNORECASE,
)

# Hard git failures: lease stale (someone else pushed), non-fast-forward,
# auth rejected. Retrying does not help.
_GIT_HARD_RE = re.compile(
    r"(stale info|non-fast-forward|rejected\b|"
    r"permission denied|authentication failed|"
    r"could not read from remote repository|repository not found)",
    re.IGNORECASE,
)


def _stderr_blob(proc: subprocess.CompletedProcess) -> str:
    parts: list[str] = []
    if proc.stderr:
        parts.append(proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode("utf-8", "replace"))
    if proc.stdout:
        parts.append(proc.stdout if isinstance(proc.stdout, str) else proc.stdout.decode("utf-8", "replace"))
    return "\n".join(parts)


def gh_classifier(proc: subprocess.CompletedProcess) -> ClassifyVerdict:
    if proc.returncode == 0:
        return "ok"
    blob = _stderr_blob(proc)
    if _GH_HARD_RE.search(blob):
        return "hard"
    if _GH_TRANSIENT_RE.search(blob):
        return "transient"
    return "hard"  # unknown failure → don't loop


def git_classifier(proc: subprocess.CompletedProcess) -> ClassifyVerdict:
    if proc.returncode == 0:
        return "ok"
    blob = _stderr_blob(proc)
    if _GIT_HARD_RE.search(blob):
        return "hard"
    if _GIT_TRANSIENT_RE.search(blob):
        return "transient"
    return "hard"


def run_with_backoff(
    cmd: list[str],
    *,
    retries: int = 3,
    base_delay_s: float = 2.0,
    classifier: Classifier = gh_classifier,
    cwd: Path | str | None = None,
    timeout: int | None = 60,
    input: str | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    **subprocess_kwargs: Any,
) -> subprocess.CompletedProcess:
    """Run `cmd` with exponential backoff on transient classification.

    Retries `retries` times (so up to `retries + 1` total attempts) with
    delays base_delay × 2^i seconds. `classifier` decides ok/transient/hard
    from the CompletedProcess.

    Pass `cwd`, `timeout`, `input` directly. Other subprocess keyword args
    flow through to `subprocess.run`.
    """
    delay = base_delay_s
    last: subprocess.CompletedProcess | None = None
    attempts = retries + 1
    for attempt in range(attempts):
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input,
            check=False,
            **subprocess_kwargs,
        )
        verdict = classifier(proc)
        if verdict == "ok":
            return proc
        if verdict == "hard":
            return proc
        last = proc
        if attempt < attempts - 1:
            log.warning(
                "transient subprocess failure (attempt %d/%d); backing off %.1fs. cmd=%s, stderr=%s",
                attempt + 1,
                attempts,
                delay,
                cmd[0:3],
                (proc.stderr or "")[:200] if isinstance(proc.stderr, str) else "(bytes)",
            )
            sleep_fn(delay)
            delay *= 2
    assert last is not None
    return last
