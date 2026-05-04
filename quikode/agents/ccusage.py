"""Uniform token/cost capture across agent CLIs via the `ccusage` family.

Background
----------
Each of our three agent CLIs writes session JSONL logs to a well-known
on-disk location (`~/.claude/projects/`, `~/.codex/sessions/`,
`~/.local/share/opencode/log/`). The `ccusage` family of npm packages
(`ccusage`, `@ccusage/codex`, `@ccusage/opencode`) reads those JSONL files
and reports token + cost totals in a uniform JSON shape.

Why we need this
----------------
- **claude** wrapper already extracts per-call usage from its
  `--output-format json` envelope; that path is per-call accurate and
  preferred. ccusage stays as a fallback when the envelope parse failed.
- **codex** wrapper had a fragile regex over stderr ("tokens used\\n<N>")
  and never produced cost/breakdowns.
- **opencode** wrapper produced 0 tokens entirely.

This module makes ccusage the uniform source of truth for codex/opencode and
the fallback for claude. The agent wrappers call `fetch_session_stats(...)`
inside the task container around each agent invocation; we use a
**snapshot-delta** approach (capture session totals before + after, take
the difference) because the variants don't all support timestamp-precise
`--since` filtering.

Containers ship with `ccusage`, `@ccusage/codex`, and `@ccusage/opencode`
globally installed (see `docker/Dockerfile`). `npx -y <variant>` finds the
global install first, so calls run in ~1s without any download. If the
container is built without these (older images), npx falls back to its
cache directory and the first call may take ~30s.

Failure mode
------------
Every error path here returns `None`. ccusage is **advisory** — never break
an agent run because of a token-accounting hiccup.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..docker_env import exec_in
from ..types import AgentResult

if TYPE_CHECKING:
    from ..docker_env import TaskContainer

log = logging.getLogger("quikode.ccusage")


# --- Variant registry --------------------------------------------------------
# Maps our internal CLI key to the npm package that reports its usage.
# `claude` uses the original `ccusage` package (no scoped variant on npm).
_CCUSAGE_VARIANT: dict[str, str] = {
    "claude": "ccusage@latest",
    "codex": "@ccusage/codex@latest",
    "opencode": "@ccusage/opencode@latest",
}


# Per-process probe cache. We probe `is_available` once per (cli, container)
# pair so a task with several agent calls doesn't re-shell-out to test for
# the variant repeatedly.
#
# Key: (cli, container_name) — TaskContainer instances aren't hashable but
# their `container_name` is stable for the container's lifetime.
_AVAILABILITY_CACHE: dict[tuple[str, str], bool] = {}


def _reset_availability_cache() -> None:
    """Test helper. Clears the per-process probe cache."""
    _AVAILABILITY_CACHE.clear()


@dataclass(frozen=True)
class CCUsageStats:
    """Token + cost totals normalized across the three ccusage variants."""

    tokens_input: int
    tokens_output: int
    tokens_cached_read: int
    tokens_cached_creation: int
    cost_usd: float
    raw_json: str

    @property
    def total_tokens(self) -> int:
        return self.tokens_input + self.tokens_output


def variant_for(cli: str) -> str | None:
    """Return the npm package spec for the given CLI, or None if unknown."""
    return _CCUSAGE_VARIANT.get(cli)


def is_available(cli: str, *, handle: TaskContainer | None = None) -> bool:
    """Probe whether the ccusage variant for `cli` exists and runs.

    When `handle` is provided we probe **inside the task container** (the
    real environment where we'll fetch stats). When `handle` is None we
    probe on the host (used by host-side tests or future host-side flows).

    Result is cached per (cli, container_name) for the life of the process.
    """
    variant = variant_for(cli)
    if variant is None:
        return False
    container_key = handle.container_name if handle is not None else "__host__"
    cache_key = (cli, container_key)
    if cache_key in _AVAILABILITY_CACHE:
        return _AVAILABILITY_CACHE[cache_key]

    available = _probe(variant, handle=handle)
    _AVAILABILITY_CACHE[cache_key] = available
    return available


def _probe(variant: str, *, handle: TaskContainer | None) -> bool:
    """Run `<variant> --help`. Treat exit 0 as available."""
    cmd = ["npx", "-y", variant, "--help"]
    try:
        if handle is None:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30.0)
            return proc.returncode == 0
        rc, _out, _err = exec_in(handle, cmd, timeout=30)
        return rc == 0
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return False


def fetch_session_stats(
    cli: str,
    *,
    handle: TaskContainer | None = None,
    timeout_s: float = 10.0,
) -> CCUsageStats | None:
    """Fetch aggregate token + cost totals across all sessions for `cli`.

    Sums across every session ccusage knows about. Callers that want a
    delta (only the work done inside this agent call) should snapshot
    before + after and subtract — see `snapshot_delta`.

    Default timeout (10s) is comfortable for warm invocations (~1s) on
    images that ship the variants pre-installed. Older images that need
    npx to fetch on first call will hit the timeout — bump this if you
    see ccusage failures with no other diagnosis.

    Returns None on any failure: missing variant, subprocess timeout,
    non-zero exit, JSON parse error, missing fields. Never raises.
    """
    variant = variant_for(cli)
    if variant is None:
        return None

    cmd = ["npx", "-y", variant, "session", "--json"]
    raw = _run_capture(cmd, handle=handle, timeout_s=timeout_s)
    if raw is None:
        return None
    return _parse_session_json(cli, raw)


def merge_into_result(result: object, stats: CCUsageStats) -> object:
    """Return a copy of `result` (an AgentResult) with token + cost fields
    overridden by `stats`. Preserves `rc`, `stdout`, `stderr`, `transient`,
    `duration_s`.

    Defined here (rather than each agent wrapper) so the merge policy is
    consistent across all three CLIs.
    """
    if not isinstance(result, AgentResult):
        raise TypeError("merge_into_result expects AgentResult")
    return result.model_copy(
        update={
            "tokens_used": stats.total_tokens,
            "tokens_input": stats.tokens_input,
            "tokens_output": stats.tokens_output,
            "tokens_cached_read": stats.tokens_cached_read,
            "tokens_cached_creation": stats.tokens_cached_creation,
            "cost_usd": stats.cost_usd,
        }
    )


# Sanity cap on per-call cost. ccusage's session-aggregate occasionally
# misattributes (e.g. counts sessions from outside this call's window,
# or returns a cumulative-since-install value when the snapshot's
# `before` baseline was missing). Anything above this cap is almost
# certainly a parser error; observed live: a single 86s subtask_doer
# call was reported at $292.89. We zero out the cost in that case
# rather than poison the briefing total + per-task rollup.
_MAX_PER_CALL_COST_USD = 50.0


def snapshot_delta(
    cli: str,
    before: CCUsageStats | None,
    after: CCUsageStats | None,
) -> CCUsageStats | None:
    """Compute the after-before delta. Either side can be None.

    If `after` is None we return None (no current data). If `before` is
    None we return `after` unchanged (no baseline → attribute everything
    to this call, which is correct on a fresh-container first call).

    Tokens clamp to >= 0. Cost clamps to >= 0.0. Cost values that
    exceed `_MAX_PER_CALL_COST_USD` are treated as parser errors and
    set to 0 with a warning logged.
    """
    if after is None:
        return None
    if before is None:
        candidate = after
    else:
        candidate = CCUsageStats(
            tokens_input=max(0, after.tokens_input - before.tokens_input),
            tokens_output=max(0, after.tokens_output - before.tokens_output),
            tokens_cached_read=max(0, after.tokens_cached_read - before.tokens_cached_read),
            tokens_cached_creation=max(0, after.tokens_cached_creation - before.tokens_cached_creation),
            cost_usd=max(0.0, after.cost_usd - before.cost_usd),
            raw_json=after.raw_json,
        )
    if candidate.cost_usd > _MAX_PER_CALL_COST_USD:
        log.warning(
            "ccusage snapshot_delta(%s): cost_usd=%.2f exceeds sanity cap (%.2f); "
            "treating as parser error and zeroing. before=%s after=%s",
            cli,
            candidate.cost_usd,
            _MAX_PER_CALL_COST_USD,
            "<None>" if before is None else f"${before.cost_usd:.2f}",
            f"${after.cost_usd:.2f}",
        )
        candidate = CCUsageStats(
            tokens_input=candidate.tokens_input,
            tokens_output=candidate.tokens_output,
            tokens_cached_read=candidate.tokens_cached_read,
            tokens_cached_creation=candidate.tokens_cached_creation,
            cost_usd=0.0,
            raw_json=candidate.raw_json,
        )
    return candidate


# --- internals ---------------------------------------------------------------


def _run_capture(
    cmd: list[str],
    *,
    handle: TaskContainer | None,
    timeout_s: float,
) -> str | None:
    """Run `cmd` host-side or inside container. Return stdout or None on failure."""
    try:
        if handle is None:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            if proc.returncode != 0:
                return None
            return proc.stdout
        rc, out, _err = exec_in(handle, cmd, timeout=int(timeout_s))
        if rc != 0:
            return None
        return out
    except (subprocess.SubprocessError, OSError, FileNotFoundError, ValueError):
        return None


def _parse_session_json(cli: str, raw: str) -> CCUsageStats | None:
    """Sum the session list returned by `<variant> session --json` and
    return aggregate stats. Schema differs slightly per variant, so
    we normalize here.

    All three variants return either:
      {"sessions": [{...}, ...]}    OR
      [{...}, ...]                  (older variant)
    """
    raw = raw.strip()
    if not raw:
        return None
    # The opencode variant prints non-JSON warnings to stdout before the
    # JSON envelope (e.g. "[@ccusage/opencode]  WARN  ..."). Strip any
    # leading non-JSON lines until we hit a line starting with `{` or `[`.
    data = _decode_json_with_leading_garbage(raw)
    if data is None:
        return None

    sessions = data.get("sessions") if isinstance(data, dict) else data
    if not isinstance(sessions, list):
        return None

    total_input = 0
    total_output = 0
    total_cached_read = 0
    total_cached_creation = 0
    total_cost = 0.0

    for s in sessions:
        if not isinstance(s, dict):
            continue
        total_input += _safe_int(s.get("inputTokens"))
        total_output += _safe_int(s.get("outputTokens"))
        total_cost += _safe_float(s.get("totalCost") or s.get("costUSD"))

        if cli == "codex":
            # codex variant: cachedInputTokens (read-only; no cache-creation
            # field). reasoningOutputTokens is a subset already counted in
            # totalTokens — we don't double-add it.
            total_cached_read += _safe_int(s.get("cachedInputTokens"))
        else:
            # claude / opencode: split cache fields.
            total_cached_read += _safe_int(s.get("cacheReadTokens"))
            total_cached_creation += _safe_int(s.get("cacheCreationTokens"))

    return CCUsageStats(
        tokens_input=total_input,
        tokens_output=total_output,
        tokens_cached_read=total_cached_read,
        tokens_cached_creation=total_cached_creation,
        cost_usd=total_cost,
        raw_json=raw,
    )


def _decode_json_with_leading_garbage(raw: str) -> object | None:
    """Try to parse `raw` as JSON. If it fails, drop leading lines until
    a line starts with `{` or `[`, then retry. Returns the parsed object
    or None if no parse succeeds.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(("{", "[")):
            candidate = "\n".join(lines[i:])
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    return None


def _safe_int(v: object) -> int:
    if v is None:
        return 0
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return n if n >= 0 else 0


def _safe_float(v: object) -> float:
    if v is None:
        return 0.0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f >= 0.0 else 0.0
