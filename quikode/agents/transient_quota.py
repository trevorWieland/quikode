"""Plan 38 PR-B.7: shared transient + quota detection helpers.

Moved out of the retired `agents/base.py` (alongside the prior `_exec`
loop) since the JSON-mode transports are the only live consumers. The
worker-side retry classifier (`workers/subtask_execution.py`) imports
`_is_transient_container_failure` from here too so transient
classification stays consistent across the JSON wrappers and the
attempt-counter gate.
"""

from __future__ import annotations

import re

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
# — distinct from a momentary transport-level rate limit. The wrappers
# sleep with exponential backoff on detection rather than surfacing a
# quota-exhausted result up to the FSM.
_QUOTA_EXHAUSTED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"you'?ve hit your (?:session|weekly|opus|5-?hour|usage) limit", re.IGNORECASE),
    re.compile(r"\brate_limit_exceeded\b"),
    re.compile(r"\b429\b"),
    re.compile(
        r"(?:rate[ _-]?limit|quota|usage[ _-]?limit)[^\n]{0,80}?"
        r"(?:exceeded|reached|exhausted|hit)",
        re.IGNORECASE,
    ),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"insufficient[ _-]?quota", re.IGNORECASE),
)


def _is_transient_container_failure(rc: int, stderr: str) -> bool:
    """True when a non-zero exit looks like a container-infra glitch.

    Conservative call on rc=137: SIGKILL inside a container almost always
    means the OOM-killer reaped us mid-exec. The alternative — an agent
    CLI that legitimately exits 137 on its own — is rare; the worker's
    progress-check + retry classifier catches the lack of forward
    progress on the next attempt if we err on the side of "transient"
    here.
    """
    if rc == 0:
        return False
    if rc == 137:
        return True
    if not stderr:
        return False
    return any(marker in stderr for marker in _TRANSIENT_STDERR_MARKERS)


def _is_quota_exhausted(rc: int, stdout: str, stderr: str) -> bool:
    """True when the agent CLI failure looks like subscription-level quota.

    Only fires when rc != 0; patterns like `HTTP 429` can legitimately
    appear in successful agent output discussing rate-limit handling
    code. False on any zero rc.
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


__all__ = [
    "_is_quota_exhausted",
    "_is_transient_container_failure",
]
