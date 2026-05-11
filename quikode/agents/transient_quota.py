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

# Codex direct can trip a short-lived OAuth refresh race when several
# concurrent `codex exec` calls try to refresh the same token. Treat the
# recognizable signatures as transport transients so one credential race does
# not become a task-level planning/checking failure.
_AGENT_AUTH_TRANSIENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\btoken_revoked\b", re.IGNORECASE),
    re.compile(r"\brefresh_token_reused\b", re.IGNORECASE),
    re.compile(r"invalidated oauth token", re.IGNORECASE),
    re.compile(r"refresh token (?:has )?already been used", re.IGNORECASE),
    re.compile(r"access token could not be refreshed", re.IGNORECASE),
)


# Plan 60 fix 2: provider-unavailable detector — distinct from
# `_is_quota_exhausted` (subscription quota) and `_is_transient_agent_auth_failure`
# (codex OAuth refresh race). This catches the class of failures where
# the provider's API itself is rejecting the call — invalid keys,
# session-expired tokens, 401/403 responses. The 2026-05-11 overnight
# Claude outage fast-failed rc=1 in 3-4 seconds with auth-shaped stderr
# that matched none of the existing patterns, so the chain walker never
# fired. Treating these as chain-walk triggers means a provider-side
# auth outage routes to the next configured fallback model instead of
# stalling the task.
_PROVIDER_UNAVAILABLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"invalid api key", re.IGNORECASE),
    re.compile(r"authentication failed", re.IGNORECASE),
    re.compile(r"authentication error", re.IGNORECASE),
    re.compile(r"session expired", re.IGNORECASE),
    re.compile(r"\b401\s+unauthorized\b", re.IGNORECASE),
    re.compile(r"\b401\b[^\n]{0,40}unauthorized", re.IGNORECASE),
    re.compile(r"\b403\s+forbidden\b", re.IGNORECASE),
    re.compile(r"\b403\b[^\n]{0,40}forbidden", re.IGNORECASE),
    re.compile(r"please run /login", re.IGNORECASE),
    re.compile(r"credit balance is too low", re.IGNORECASE),
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


def _is_provider_unavailable(rc: int, stdout: str, stderr: str) -> bool:
    """Plan 60 fix 2: True when the agent CLI failure looks like a
    provider-side auth/availability issue distinct from subscription
    quota.

    Triggers a chain walk in `QuotaFallbackJsonAgent` so today's
    overnight-Claude class of outage cascades to the next configured
    fallback model instead of stalling. Only fires on rc != 0; the
    patterns are common enough in agent stdout (discussing auth code)
    that requiring a non-zero exit avoids false positives.
    """
    if rc == 0:
        return False
    for blob in (stderr, stdout):
        if not blob:
            continue
        for pat in _PROVIDER_UNAVAILABLE_PATTERNS:
            if pat.search(blob):
                return True
    return False


def _is_transient_agent_auth_failure(rc: int, stdout: str, stderr: str) -> bool:
    """True when an agent CLI failure looks like a retryable auth race.

    This intentionally does not classify a bare HTTP 401 as transient. A plain
    unauthorized response can mean the operator needs to re-authenticate; the
    refresh-token race has more specific signatures that are safe to retry
    briefly.
    """
    if rc == 0:
        return False
    for blob in (stderr, stdout):
        if not blob:
            continue
        for pat in _AGENT_AUTH_TRANSIENT_PATTERNS:
            if pat.search(blob):
                return True
    return False


__all__ = [
    "_is_provider_unavailable",
    "_is_quota_exhausted",
    "_is_transient_agent_auth_failure",
    "_is_transient_container_failure",
]
