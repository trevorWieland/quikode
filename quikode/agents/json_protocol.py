"""JSON-mode agent protocol, schema validation, and retry/quota helpers."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..execution import exec_in
from .json_protocol_types import JsonAgentResult, JsonAgentTransport, RawTransportResult
from .json_validation import build_reprompt, codex_output_schema, invoke_with_validation
from .transient_quota import (
    _is_quota_exhausted,
    _is_transient_agent_auth_failure,
    _is_transient_container_failure,
)

log = logging.getLogger("quikode.agents.json")


# ---------- shared retry/quota helper ----------


def _quota_backoff_initial_s() -> int:
    return int(os.environ.get("QUIKODE_QUOTA_BACKOFF_INITIAL_S", "300"))


def _quota_backoff_max_s() -> int:
    return int(os.environ.get("QUIKODE_QUOTA_BACKOFF_MAX_S", "1800"))


def _quota_max_total_wait_s() -> int:
    return int(os.environ.get("QUIKODE_QUOTA_MAX_TOTAL_WAIT_S", "28800"))


def _auth_backoff_initial_s() -> int:
    return int(os.environ.get("QUIKODE_AUTH_BACKOFF_INITIAL_S", "15"))


def _auth_backoff_max_s() -> int:
    return int(os.environ.get("QUIKODE_AUTH_BACKOFF_MAX_S", "120"))


def _auth_max_total_wait_s() -> int:
    return int(os.environ.get("QUIKODE_AUTH_MAX_TOTAL_WAIT_S", "900"))


@dataclass(frozen=True)
class _ExecOutcome:
    """Internal result of one wrapped `exec_in` call inside `_run_with_retry`."""

    rc: int
    stdout: str
    stderr: str
    timed_out: bool


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
        )
    sleep_s = state.next_s
    wait_msg = (
        f"\n[quikode] agent auth refresh failure (rc={rc}); retry {state.retries + 1}, "
        f"sleeping {sleep_s}s (cumulative {state.waited_s}s of {state.max_total_s}s cap)"
    )
    log.warning(wait_msg.strip())
    _append_agent_log(log_path, wait_msg)
    time.sleep(sleep_s)
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
    quota_max_total_wait_s: int | None = None,
) -> _ExecOutcome:
    """Shared retry/quota/transient-detection loop for all JSON shims.

    Mirrors `agents.base._exec` semantics exactly:
    - per-call `timeout` enforced via `subprocess.TimeoutExpired` → rc=124,
      transient=True.
    - container-infra glitches (`_is_transient_container_failure`) → rc=124,
      transient=True (free retry by the worker).
    - subscription quota exhaustion (`_is_quota_exhausted`) → sleep with
      exponential backoff (5min → 10 → 20 → 30min cap) and retry the same
      call up to `_quota_max_total_wait_s`. The FSM never sees a
      quota-exhausted result.
    - agent auth refresh races (`token_revoked` / `refresh_token_reused`) →
      short exponential backoff and retry. If the auth race never clears,
      return a transient rc=124 outcome so worker-level transient handling
      can avoid misclassifying it as a task/content failure.

    Returns `_ExecOutcome` carrying rc + stdout + stderr; the caller's
    transport shim translates into `RawTransportResult` (parsing structured
    output, computing duration, etc.).
    """
    backoff_s = _quota_backoff_initial_s()
    backoff_max_s = _quota_backoff_max_s()
    max_total_wait_s = _quota_max_total_wait_s() if quota_max_total_wait_s is None else quota_max_total_wait_s
    waited_s = 0
    quota_retries = 0
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
                return _ExecOutcome(
                    rc=rc,
                    stdout=out,
                    stderr=(err or "") + give_up,
                    timed_out=False,
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
            time.sleep(sleep_s)
            waited_s += sleep_s
            backoff_s = min(backoff_s * 2, backoff_max_s)
            quota_retries += 1
            continue
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
    """Wraps a `JsonAgentTransport` for writes-files roles."""

    def __init__(
        self,
        transport: JsonAgentTransport,
        envelope_schema: type[BaseModel],
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
        return invoke_with_validation(
            self.transport,
            prompt,
            output_schema=self.envelope_schema,
            handle=handle,
            log_path=log_path,
            timeout=timeout,
        )


__all__ = [
    "JsonAgentResult",
    "JsonAgentTransport",
    "JsonOutputAgent",
    "RawTransportResult",
    "WritesFilesAgent",
    "build_reprompt",
    "codex_output_schema",
]
