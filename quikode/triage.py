"""CI log parser for the post-PR feedback path.

Pre-plan-28 this module also held the per-thread review classifier (`claude
-p` host-side call, sonnet-class model). Plan 28 retired that path: only
formal GitHub Reviews trigger transitions, and a CHANGES_REQUESTED review's
body + every unresolved thread + every PR comment all flow as bundled CONTEXT
into the fixup planner — no separate classifier in the loop. This module is
now CI-only.

`parse_ci_failure` extracts structured failures from a CI log so the fixup
planner can scope subtasks against (file, line, kind, message, excerpt)
records instead of a wall of raw output.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

log = logging.getLogger("quikode.triage")


# ----- CI log parser -----


@dataclass(frozen=True)
class CIFailure:
    """One actionable failure extracted from a CI log."""

    kind: Literal["compile", "test", "lint", "fmt", "other"]
    file: str | None  # repo-relative path if discoverable
    line: int | None
    message: str  # short one-liner
    excerpt: str  # ~10-line surrounding excerpt for context


# Pattern catalog. Each entry tries to pluck (kind, file, line, msg) out of
# one log "anchor" line; the surrounding ~5 lines (before + after) get
# captured into `excerpt` so the planner sees enough context.
#
# Order matters — earlier patterns win. Keep the most specific (cargo, ruff)
# above the generic catch-all.
_PATTERN_CARGO = re.compile(
    r"^error(?:\[(?P<code>E\d+)\])?: (?P<msg>.+)\n\s+--> (?P<file>[^:\n]+):(?P<line>\d+):\d+",
    re.MULTILINE,
)
_PATTERN_CLIPPY = re.compile(
    r"^(?:warning|error): (?P<msg>.+)\n\s+--> (?P<file>[^:\n]+):(?P<line>\d+):\d+",
    re.MULTILINE,
)
_PATTERN_RUFF = re.compile(
    r"^(?P<file>[^:\s]+):(?P<line>\d+):\d+: (?P<code>[A-Z]\d+) (?P<msg>.+)$",
    re.MULTILINE,
)
_PATTERN_PYTEST_FAIL = re.compile(
    r"^(?P<file>[^:\s]+):(?P<line>\d+): (?P<exc>\w+(?:Error|Exception)): (?P<msg>.+)$",
    re.MULTILINE,
)
_PATTERN_PYTEST_ASSERT = re.compile(
    r"^FAILED (?P<file>[^:\s]+)::(?P<test>\S+) - (?P<msg>.+)$",
    re.MULTILINE,
)
_PATTERN_GENERIC_ERROR = re.compile(
    r"^(?P<file>[^:\s]+):(?P<line>\d+):\s*error[:\s]+(?P<msg>.+)$",
    re.MULTILINE,
)


def _excerpt_around(log_text: str, match_start: int, match_end: int, *, ctx_lines: int = 4) -> str:
    """Pull ~ctx_lines on each side of the matched span as a readable excerpt."""
    line_start = log_text.rfind("\n", 0, match_start) + 1
    cur = line_start
    for _ in range(ctx_lines):
        prev = log_text.rfind("\n", 0, cur - 1)
        if prev < 0:
            cur = 0
            break
        cur = prev + 1
    line_end = log_text.find("\n", match_end)
    if line_end < 0:
        line_end = len(log_text)
    cur_end = line_end
    for _ in range(ctx_lines):
        nxt = log_text.find("\n", cur_end + 1)
        if nxt < 0:
            cur_end = len(log_text)
            break
        cur_end = nxt
    return log_text[cur:cur_end].strip()


def parse_ci_failure(log_text: str) -> list[CIFailure]:
    """Extract structured failures from a CI log. Empty list if nothing matches.

    The patterns are anchored on the typical compiler/linter/runner error
    headlines. Anything not matched falls through silently — caller can
    decide whether to hand the raw log to the planner as a fallback.
    """
    if not log_text:
        return []

    seen: set[tuple[str | None, int | None, str]] = set()
    out: list[CIFailure] = []

    def _push(kind: str, file: str | None, line: int | None, msg: str, span: tuple[int, int]) -> None:
        sig = (file, line, msg.strip()[:120])
        if sig in seen:
            return
        seen.add(sig)
        excerpt = _excerpt_around(log_text, span[0], span[1])
        failure_kind = cast(Literal["compile", "test", "lint", "fmt", "other"], kind)
        out.append(
            CIFailure(
                kind=failure_kind,
                file=file,
                line=line,
                message=msg.strip(),
                excerpt=excerpt,
            )
        )

    for m in _PATTERN_CARGO.finditer(log_text):
        _push("compile", m.group("file"), int(m.group("line")), m.group("msg"), m.span())
    for m in _PATTERN_CLIPPY.finditer(log_text):
        _push("lint", m.group("file"), int(m.group("line")), m.group("msg"), m.span())
    for m in _PATTERN_RUFF.finditer(log_text):
        _push("lint", m.group("file"), int(m.group("line")), f"{m.group('code')} {m.group('msg')}", m.span())
    for m in _PATTERN_PYTEST_FAIL.finditer(log_text):
        _push(
            "test",
            m.group("file"),
            int(m.group("line")),
            f"{m.group('exc')}: {m.group('msg')}",
            m.span(),
        )
    for m in _PATTERN_PYTEST_ASSERT.finditer(log_text):
        _push("test", m.group("file"), None, m.group("msg"), m.span())
    for m in _PATTERN_GENERIC_ERROR.finditer(log_text):
        _push("other", m.group("file"), int(m.group("line")), m.group("msg"), m.span())

    return out[:25]


# Kept (used by other modules) — pure path-helper, no agent invocation.
_REPO_PATH = Path  # re-export alias to suppress unused-import warnings if any
