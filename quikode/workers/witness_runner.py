"""Plan 33: scoped per-subtask behavior witness runner.

Before invoking the LLM checker, the per-subtask worker runs the
witness commands declared in `subtask.behavior_evidence_advanced` —
only the evidence ids THIS subtask claims to advance, not the parent
task's full set. The audit gauntlet still runs the broader behavior
audit later; this is a fast, scoped pre-check that catches stub-shaped
diffs that look right to a code reader but produce empty/error output
when the witness actually executes.

## Runtime caps (Plan 33 §7.2 + open question 2)

* Per-witness wall-clock cap: `cfg.subtask_witness_timeout_seconds`
  (default 15s).
* Per-subtask total cap: `2 * len(behavior_evidence_advanced) *
  per_witness_cap`. This formula accommodates worst-case suites where
  every witness takes near the full per-witness cap (e.g. BDD-shaped
  scenarios) without leaving the worker spinning indefinitely.
* On per-witness timeout: the row is classified as `TIMEOUT` and the
  worker continues to the next witness; the partial accumulation is
  passed to the LLM checker so it can read what actually happened.

## Output shape

```python
{
    "<evidence_id>": {
        "rc": int | None,            # None when classified as TIMEOUT or NO_COMMAND
        "stdout_excerpt": str,       # first 4 KB of stdout
        "stderr_excerpt": str,       # first 4 KB of stderr
        "runtime_ms": int,
        "classification": str,       # OK | NONZERO_RC | TIMEOUT | NO_COMMAND | ERROR
        "note": str,                 # human-readable detail (used by checker prompt)
    },
    ...
}
```

The `classification` field gives the LLM checker an unambiguous
signal: a TIMEOUT classification means "the witness didn't finish in
the cap; runtime caps may need tuning" — not "the test failed". The
checker prompt is wired to surface TIMEOUT as a soft signal.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..evaluation_contract import _evidence_canonical_id

log = logging.getLogger("quikode.workers.witness_runner")


_OUTPUT_EXCERPT_BYTES = 4096


ExecFn = Callable[..., tuple[int, str, str]]
"""Signature: exec_in(handle, cmd, *, log_path=None, stdin=None, timeout=None)
   -> (rc, stdout, stderr). Matches `quikode.execution.exec_in`."""


def _truncate(text: str | None, cap: int = _OUTPUT_EXCERPT_BYTES) -> str:
    if not text:
        return ""
    if len(text) <= cap:
        return text
    return text[:cap] + "\n... [truncated to first 4 KB]"


def _extract_witness_command(ev: dict[str, Any]) -> str | None:
    """Pull the runnable witness command out of one expected_evidence
    item. Order: `command` (manual-probe convention), `witness_command`
    (planner-emitted forward-compat), `witnesses[0]` if it looks like a
    shell command (starts with `bash`/`just`/`npm`/...). Returns None
    when no command is recoverable.
    """
    cmd = ev.get("command")
    if isinstance(cmd, str) and cmd.strip():
        return cmd.strip()
    cmd = ev.get("witness_command")
    if isinstance(cmd, str) and cmd.strip():
        return cmd.strip()
    witnesses = ev.get("witnesses") or ()
    if isinstance(witnesses, list | tuple) and witnesses:
        first = witnesses[0]
        if isinstance(first, str) and any(
            first.lstrip().startswith(prefix)
            for prefix in (
                "bash ",
                "sh ",
                "just ",
                "npm ",
                "yarn ",
                "pnpm ",
                "cargo ",
                "pytest",
                "uv ",
                "python ",
                "make ",
                "go ",
                "tsc ",
                "deno ",
                "./",
            )
        ):
            return first.strip()
    return None


def _evidence_id_to_command(
    expected_evidence: list[dict] | tuple[dict, ...] | None,
    evidence_id: str,
) -> tuple[str | None, dict[str, Any] | None]:
    """Look up the expected_evidence row whose canonical id matches
    `evidence_id`. Returns (witness_command, raw_row). Both None when
    the id isn't found on the node."""
    if not expected_evidence:
        return None, None
    for ev in expected_evidence:
        if not isinstance(ev, dict):
            continue
        if _evidence_canonical_id(ev) == evidence_id:
            return _extract_witness_command(ev), ev
    return None, None


def run_scoped_witnesses(
    *,
    handle: Any,
    expected_evidence: list[dict] | tuple[dict, ...] | None,
    evidence_ids: list[str] | tuple[str, ...],
    per_witness_timeout_s: int,
    exec_in: ExecFn,
    log_path: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Run witness commands for the listed evidence ids inside the
    container. See module docstring for cap formulas + result shape.

    `expected_evidence` is `node.expected_evidence` from the DAG; the
    function walks it to find the witness command for each id. When a
    requested id isn't on the node OR has no recoverable command, the
    row is recorded as `NO_COMMAND` and skipped (no rc, no runtime).
    """
    if per_witness_timeout_s <= 0:
        per_witness_timeout_s = 15
    total_budget_s = max(2 * len(evidence_ids) * per_witness_timeout_s, per_witness_timeout_s)
    deadline = time.monotonic() + total_budget_s
    results: dict[str, dict[str, Any]] = {}
    for evidence_id in evidence_ids:
        cmd, _row = _evidence_id_to_command(expected_evidence, evidence_id)
        if cmd is None:
            results[evidence_id] = {
                "rc": None,
                "stdout_excerpt": "",
                "stderr_excerpt": "",
                "runtime_ms": 0,
                "classification": "NO_COMMAND",
                "note": (
                    f"no runnable command for evidence id {evidence_id!r} on the node "
                    "(checker must read the diff to verify this witness)"
                ),
            }
            continue
        remaining = max(int(deadline - time.monotonic()), 1)
        cap = min(per_witness_timeout_s, remaining)
        if remaining <= 0:
            results[evidence_id] = {
                "rc": None,
                "stdout_excerpt": "",
                "stderr_excerpt": "",
                "runtime_ms": 0,
                "classification": "TIMEOUT",
                "note": (
                    "per-subtask total witness budget exhausted before this "
                    f"witness could run (budget={total_budget_s}s)"
                ),
            }
            continue
        start = time.monotonic()
        try:
            rc, stdout, stderr = exec_in(
                handle,
                ["bash", "-lc", f"cd /workspace && {cmd}"],
                log_path=log_path,
                timeout=cap,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            note = "per-witness timeout" if isinstance(e, subprocess.TimeoutExpired) else f"exec error: {e}"
            classification = "TIMEOUT" if isinstance(e, subprocess.TimeoutExpired) else "ERROR"
            results[evidence_id] = {
                "rc": None,
                "stdout_excerpt": "",
                "stderr_excerpt": str(e)[:_OUTPUT_EXCERPT_BYTES],
                "runtime_ms": elapsed_ms,
                "classification": classification,
                "note": (f"witness `{cmd[:160]}` did not finish within {cap}s cap; NOTE: {note}"),
            }
            continue
        elapsed_ms = int((time.monotonic() - start) * 1000)
        classification = "OK" if rc == 0 else "NONZERO_RC"
        results[evidence_id] = {
            "rc": rc,
            "stdout_excerpt": _truncate(stdout),
            "stderr_excerpt": _truncate(stderr),
            "runtime_ms": elapsed_ms,
            "classification": classification,
            "note": (
                f"witness command: `{cmd[:160]}` "
                f"(rc={rc}, runtime={elapsed_ms}ms, classification={classification})"
            ),
        }
    return results


__all__ = ["run_scoped_witnesses"]
