"""codex headless wrapper.

Invocation:
  codex exec --dangerously-bypass-approvals-and-sandbox --color never --cd /workspace [-m MODEL] -

CRITICAL FLAGS:
  - `--dangerously-bypass-approvals-and-sandbox` is required INSIDE a docker
    container because codex's underlying sandbox (bubblewrap / bwrap) cannot
    create user namespaces in unprivileged containers, which silently makes
    every `exec_command` fail with `bwrap: No permissions to create a new
    namespace`. Without this flag, codex falls back to a GitHub-API file fetch
    that resolves against `main`, so the checker sees stale/wrong content and
    issues incorrect FAIL verdicts. Inside an already-sandboxed dev container,
    bypassing codex's inner sandbox is safe.
  - We use `--output-last-message` to a tempfile to capture only the final
    answer (codex prints a verbose preamble + token count to stdout/stderr
    otherwise).
"""

from __future__ import annotations

import secrets
import subprocess
import time
from pathlib import Path

from ..execution import exec_in
from . import ccusage
from .base import AgentResult, _is_transient_container_failure, parse_tokens


class CodexAgent:
    name = "codex"

    def __init__(self, model: str | None = None, extra_args: list[str] | None = None):
        self.model = model
        self.extra_args = list(extra_args or [])

    def run(
        self, prompt: str, *, handle: object, log_path: Path | None = None, timeout: int | None = None
    ) -> AgentResult:
        out_file = f"/tmp/qk_codex_{secrets.token_hex(4)}.txt"
        parts = [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--color",
            "never",
            "--cd",
            "/workspace",
            "--skip-git-repo-check",
            "--output-last-message",
            out_file,
        ]
        if self.model:
            parts += ["-m", self.model]
        parts += self.extra_args
        parts += ["-"]  # read prompt from stdin
        # After codex finishes, dump the captured final message to stdout so
        # quikode's pipeline sees a clean response.
        shell_cmd = " ".join(parts) + f" >&2 ; cat {out_file} ; rm -f {out_file}"
        t0 = time.time()
        # Snapshot ccusage totals before the call; we'll diff after to get
        # this call's contribution. Codex's stderr regex still runs as a
        # belt-and-suspenders fallback when ccusage is unavailable.
        before = ccusage.fetch_session_stats("codex", handle=handle)
        try:
            rc, out, err = exec_in(
                handle, ["bash", "-lc", shell_cmd], log_path=log_path, stdin=prompt, timeout=timeout
            )
        except subprocess.TimeoutExpired as e:
            # Mirror `_exec`'s timeout handling so codex's checker / triage /
            # planner calls don't crash the worker on a hung subprocess
            # (e.g. quota wait, model API hang). Without this the
            # `TimeoutExpired` propagates uncaught through subtask_execution
            # → task_worker.run → FSM CRASH event → task FAILED. Caller
            # treats `transient=True` as a free retry (no attempt-counter bump).
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
            msg = f"\n[quikode] codex timed out after {timeout}s; treating as transient retry"
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
        # Mirror _exec's transient-failure detection so codex doesn't bypass
        # the free-retry path just because it constructs its AgentResult
        # locally (it captures `--output-last-message` and dumps it post-run).
        transient = _is_transient_container_failure(rc, err)
        if transient:
            annotation = (
                f"\n[quikode] container-level transient failure detected: "
                f"rc={rc}; treating as transient retry"
            )
            base = AgentResult(
                rc=124,
                stdout=out,
                stderr=(err or "") + annotation,
                tokens_used=parse_tokens(out, err),
                duration_s=time.time() - t0,
                transient=True,
            )
        else:
            base = AgentResult(
                rc=rc,
                stdout=out,
                stderr=err,
                tokens_used=parse_tokens(out, err),
                duration_s=time.time() - t0,
            )
        return _enrich_with_ccusage(base, handle=handle, before=before)


def _enrich_with_ccusage(
    base: AgentResult, *, handle: object, before: ccusage.CCUsageStats | None
) -> AgentResult:
    """Override token + cost fields on `base` with the ccusage delta when
    available. ccusage is the source of truth for codex (the previous
    stderr regex captured a total only — never input/output split, never
    cost). On any ccusage failure we fall back to whatever `parse_tokens`
    extracted, which is what the AgentResult already carries.
    """
    after = ccusage.fetch_session_stats("codex", handle=handle)
    delta = ccusage.snapshot_delta("codex", before, after)
    if delta is None or delta.total_tokens <= 0:
        return base
    return ccusage.merge_into_result(base, delta)
