"""JSON-mode agent protocol, schema validation, and retry/quota helpers.

Plan 59 fix E': the in-transport quota retry loop is gone. Quota
detection now returns immediately with `category="quota_exhausted"` on
the outcome so the chain walker (`QuotaFallbackJsonAgent`) can cascade
to the next provider in seconds, and the worker layer
(`workers/subtasks.py._record_transient_subtask_failure`) handles the
cross-attempt cadence via `cfg.transient_retry_delays_s`. The
container-vanished and auth-refresh retries remain in-transport (both
brief; both legitimately transport-internal).

Plan 59 fix B: while sleeping inside the auth-refresh / container
retry loops, the helper notifies the worker layer via the
`agent_call_status_callback` ContextVar so `agent_calls.status`
flips to `backoff_auth` / `backoff_container` and back to `running`.
The TUI surfaces this immediately so the operator sees a worker is
actively waiting on auth refresh, not silently stalled.
"""

from __future__ import annotations

import contextvars
import logging
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..execution import exec_in
from .json_protocol_types import JsonAgentResult, JsonAgentTransport, RawTransportResult
from .json_validation import build_reprompt, codex_output_schema, invoke_with_validation
from .transient_quota import (
    _is_provider_unavailable,
    _is_quota_exhausted,
    _is_transient_agent_auth_failure,
    _is_transient_container_failure,
)

log = logging.getLogger("quikode.agents.json")

# Plan 59 fix B: workers set this contextvar before invoking an agent so
# `_run_with_retry` can flip the `agent_calls.status` column to
# `backoff_auth` / `backoff_container` (and back to `running`) without
# threading a callback through every transport method's signature. The
# callback is `Callable[[str], None]`; `None` means no worker is
# listening (test/standalone use) and status updates are silently
# skipped.
agent_call_status_callback: contextvars.ContextVar[Callable[[str], None] | None] = contextvars.ContextVar(
    "agent_call_status_callback", default=None
)


class agent_call_status_scope:
    """Plan 59 fix B: context manager binding a per-call status callback.

    Workers wrap their `agent.invoke(...)` call in this scope so the
    `_run_with_retry` loop can emit `backoff_auth` / `backoff_container`
    transitions on `agent_calls.status` for the worker's most recent
    start-marker row. Outside the scope the contextvar resets to its
    prior value (defaults to `None` — no listener; updates silently
    dropped).
    """

    def __init__(self, callback: Callable[[str], None] | None):
        self._callback = callback
        self._token: contextvars.Token[Callable[[str], None] | None] | None = None

    def __enter__(self) -> agent_call_status_scope:
        self._token = agent_call_status_callback.set(self._callback)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._token is not None:
            agent_call_status_callback.reset(self._token)
            self._token = None


# ---------- shared retry/quota helper ----------


def _auth_backoff_initial_s() -> int:
    return int(os.environ.get("QUIKODE_AUTH_BACKOFF_INITIAL_S", "15"))


def _auth_backoff_max_s() -> int:
    return int(os.environ.get("QUIKODE_AUTH_BACKOFF_MAX_S", "120"))


def _auth_max_total_wait_s() -> int:
    return int(os.environ.get("QUIKODE_AUTH_MAX_TOTAL_WAIT_S", "900"))


def _emit_status(status: str) -> None:
    """Plan 59 fix B: fire the contextvar-bound status callback if set.

    Errors in the callback are logged but never propagate — the
    backoff loop must continue even if the status update fails.
    """
    cb = agent_call_status_callback.get()
    if cb is None:
        return
    try:
        cb(status)
    except Exception as exc:
        log.warning("agent_call_status_callback raised %s — ignoring", exc)


# Plan 59 fix E': outcome categories carried to the worker layer.
# `quota_exhausted` is the new fast-fail signal from the transport;
# `container_vanished` / `auth_refresh` mirror the existing transient
# classifiers; `none` is the default (no transient category applies).
_CATEGORY_QUOTA_EXHAUSTED = "quota_exhausted"
_CATEGORY_CONTAINER_VANISHED = "container_vanished"
_CATEGORY_AUTH_REFRESH = "auth_refresh"
_CATEGORY_NONE = "none"


@dataclass(frozen=True)
class _ExecOutcome:
    """Internal result of one wrapped `exec_in` call inside `_run_with_retry`.

    Plan 59 fix E' adds `category` so the worker layer can pick a
    category-specific sleep (e.g. 600s for quota, 60s for auth refresh)
    from `cfg.transient_retry_delays_s` instead of a generic
    `time.sleep(15)`. `none` means no transient category applies.
    """

    rc: int
    stdout: str
    stderr: str
    timed_out: bool
    category: str = _CATEGORY_NONE


@dataclass
class _BackoffState:
    next_s: int
    max_s: int
    waited_s: int
    max_total_s: int
    retries: int = 0


def _append_agent_log(log_path: Path | None, message: str) -> None:
    if log_path is None:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(message + "\n")


def _handle_agent_auth_retry(
    *,
    rc: int,
    out: str,
    err: str,
    state: _BackoffState,
    log_path: Path | None,
) -> tuple[bool, _ExecOutcome | None]:
    if not _is_transient_agent_auth_failure(rc, out, err):
        return False, None
    if state.waited_s >= state.max_total_s:
        give_up = (
            f"\n[quikode] agent auth refresh retry exceeded {state.max_total_s}s "
            f"after {state.retries} retries; surfacing as transient transport failure"
        )
        log.warning(give_up.strip())
        _append_agent_log(log_path, give_up)
        return True, _ExecOutcome(
            rc=124,
            stdout=out,
            stderr=(err or "") + give_up,
            timed_out=True,
            category=_CATEGORY_AUTH_REFRESH,
        )
    sleep_s = state.next_s
    wait_msg = (
        f"\n[quikode] agent auth refresh failure (rc={rc}); retry {state.retries + 1}, "
        f"sleeping {sleep_s}s (cumulative {state.waited_s}s of {state.max_total_s}s cap)"
    )
    log.warning(wait_msg.strip())
    _append_agent_log(log_path, wait_msg)
    # Plan 59 fix B: flip the agent_call status to backoff_auth so the
    # TUI shows "subtask_doer backoff_auth 45s" while we sleep, then
    # back to running before the retry fires.
    _emit_status("backoff_auth")
    time.sleep(sleep_s)
    _emit_status("running")
    state.waited_s += sleep_s
    state.next_s = min(state.next_s * 2, state.max_s)
    state.retries += 1
    return True, None


def _run_with_retry(
    handle: Any,
    cmd: list[str],
    *,
    stdin: str | None,
    log_path: Path | None,
    timeout: int,
) -> _ExecOutcome:
    """Shared retry/transient-detection loop for all JSON shims.

    Semantics (post plan 59 fix E'):

    - per-call `timeout` enforced via `subprocess.TimeoutExpired` →
      rc=124, transient=True.
    - container-infra glitches (`_is_transient_container_failure`) →
      rc=124, transient=True, `category=container_vanished`. The
      worker layer picks the category-aware sleep.
    - subscription quota exhaustion (`_is_quota_exhausted`) →
      returns IMMEDIATELY with the original rc + stderr and
      `category=quota_exhausted`. No in-transport sleep + retry — the
      fallback chain (`QuotaFallbackJsonAgent`) cascades in seconds,
      and the worker layer (plan 59 fix E') sleeps the configured
      `cfg.transient_retry_delays_s["quota_exhausted"]` between
      attempts.
    - agent auth refresh races → short exponential backoff + retry
      in-transport. If the race never clears, surface as transient
      rc=124 with `category=auth_refresh` so the worker classifies it
      consistently.

    Returns `_ExecOutcome` carrying rc + stdout + stderr + category;
    the caller's transport shim translates into `RawTransportResult`.
    """
    auth_backoff = _BackoffState(
        next_s=_auth_backoff_initial_s(),
        max_s=_auth_backoff_max_s(),
        waited_s=0,
        max_total_s=_auth_max_total_wait_s(),
    )
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
            return _ExecOutcome(
                rc=124,
                stdout=partial_out,
                stderr=(partial_err + msg).strip(),
                timed_out=True,
            )
        if _is_transient_container_failure(rc, err):
            annotation = (
                f"\n[quikode] container-level transient failure detected: rc={rc}; "
                f"treating as transient retry"
            )
            _append_agent_log(log_path, annotation)
            return _ExecOutcome(
                rc=124,
                stdout=out,
                stderr=(err or "") + annotation,
                timed_out=True,
                category=_CATEGORY_CONTAINER_VANISHED,
            )
        if _is_quota_exhausted(rc, out, err) or _is_provider_unavailable(rc, out, err):
            # Plan 59 fix E' + Plan 60 fix 2: fast-fail. No in-transport
            # sleep. The fallback chain cascades to the next provider in
            # seconds; if every provider returns quota / provider-
            # unavailable, the worker layer handles the cross-attempt
            # cadence (default 600s) via
            # `cfg.transient_retry_delays_s["quota_exhausted"]`.
            # Provider-unavailable shares the same downstream path
            # (chain-walk + worker-layer sleep) as quota since the
            # operator intent is identical: don't burn retry budget on a
            # provider that can't answer.
            kind = "quota exhausted" if _is_quota_exhausted(rc, out, err) else "provider unavailable"
            annotation = (
                f"\n[quikode] {kind} (rc={rc}); fast-fail to chain walker / "
                f"worker-layer retry (no in-transport sleep)"
            )
            log.info(annotation.strip())
            _append_agent_log(log_path, annotation)
            return _ExecOutcome(
                rc=rc,
                stdout=out,
                stderr=(err or "") + annotation,
                timed_out=False,
                category=_CATEGORY_QUOTA_EXHAUSTED,
            )
        auth_handled, auth_outcome = _handle_agent_auth_retry(
            rc=rc,
            out=out,
            err=err,
            state=auth_backoff,
            log_path=log_path,
        )
        if auth_outcome is not None:
            return auth_outcome
        if auth_handled:
            continue
        return _ExecOutcome(rc=rc, stdout=out, stderr=err, timed_out=False)


# ---------- wrappers ----------


class JsonOutputAgent:
    """Wraps a `JsonAgentTransport` for non-writes-files roles."""

    def __init__(self, transport: JsonAgentTransport, output_schema: type[BaseModel]):
        self.transport = transport
        self.output_schema = output_schema

    def invoke(
        self,
        prompt: str,
        *,
        handle: Any,
        log_path: Path | None = None,
        timeout: int,
    ) -> JsonAgentResult:
        return invoke_with_validation(
            self.transport,
            prompt,
            output_schema=self.output_schema,
            handle=handle,
            log_path=log_path,
            timeout=timeout,
        )


class WritesFilesAgent:
    """Wraps a `JsonAgentTransport` for writes-files roles.

    Plan 47: `envelope_schema` is now optional. When it is `None` (the
    post-plan-47 doer), the wrapper invokes the transport's
    `invoke_raw` path — no `--output-schema` / `--json-schema` flags,
    no pydantic re-prompt loop. The diff in the worktree is the sole
    deliverable; the returned `JsonAgentResult` carries `structured=None`,
    `parse_errors=()`, and `raw_text` for briefing/log purposes only.

    When `envelope_schema` is set (the conflict-resolver), behavior is
    unchanged — schema-validated round-trip with re-prompt-once on
    invalid bookkeeping.
    """

    def __init__(
        self,
        transport: JsonAgentTransport,
        envelope_schema: type[BaseModel] | None,
    ):
        self.transport = transport
        self.envelope_schema = envelope_schema

    def invoke(
        self,
        prompt: str,
        *,
        handle: Any,
        log_path: Path | None = None,
        timeout: int,
    ) -> JsonAgentResult:
        if self.envelope_schema is None:
            return _invoke_without_validation(
                self.transport,
                prompt,
                handle=handle,
                log_path=log_path,
                timeout=timeout,
            )
        return invoke_with_validation(
            self.transport,
            prompt,
            output_schema=self.envelope_schema,
            handle=handle,
            log_path=log_path,
            timeout=timeout,
        )


def _invoke_without_validation(
    transport: JsonAgentTransport,
    prompt: str,
    *,
    handle: Any,
    log_path: Path | None,
    timeout: int,
) -> JsonAgentResult:
    """Plan 47: writes-files invocation without JSON-schema enforcement.

    Runs the transport once via `invoke_raw`, captures rc / duration /
    transient / tokens, and packages into a `JsonAgentResult` with
    `structured=None` and `parse_errors=()`. The diff in `/workspace`
    is what the worker grades; this result exists only so the worker
    can tell `transient` apart from `success` and record the agent
    call.
    """
    raw = transport.invoke_raw(
        prompt,
        handle=handle,
        log_path=log_path,
        timeout=timeout,
    )
    return JsonAgentResult(
        structured=None,
        rc=raw.rc,
        transient=raw.transient,
        duration_s=raw.duration_s,
        tokens_input=raw.tokens_input,
        tokens_output=raw.tokens_output,
        cost_usd=raw.cost_usd,
        parse_errors=(),
        raw_text=raw.raw_text,
        stderr_excerpt=raw.stderr_excerpt,
        category=raw.category,
    )


__all__ = [
    "JsonAgentResult",
    "JsonAgentTransport",
    "JsonOutputAgent",
    "RawTransportResult",
    "WritesFilesAgent",
    "agent_call_status_callback",
    "agent_call_status_scope",
    "build_reprompt",
    "codex_output_schema",
]
