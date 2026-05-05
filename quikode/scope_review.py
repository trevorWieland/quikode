"""Semantic scope review of doer commit drift.

When the doer's actual diff doesn't match the planner's declared
`files_to_touch`, the worker calls this module to judge whether the
drift is legitimate (auto-gen outputs, refactor splits, companion
tests) or overreach (doer wandered into unrelated modules).

Why semantic, not strict: the strict-list approach (`git add -- <list>`,
fail on missing files) burned ten retries on R-0002/S-09-web because
the planner declared `messages.ts` but Paraglide auto-generated
`messages.js`. The doer kept saying "everything's already done", the
strict gate kept refusing to commit, and the cycle ran the retry
budget. A fast LLM judge breaks that loop without giving up boundary
discipline — out-of-lane drift still surfaces, just as a warning the
audit can pick up rather than as a commit-time refusal.

Default-LEGIT on agent failure / parse error: the reviewer is an
advisory layer, not a gatekeeper. If the agent infra is down we'd
rather commit (the audit pipeline catches genuine quality issues) than
block the entire run on the reviewer's availability.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import prompts as prompts_mod
from .agents import build_agent
from .config import AgentRole, Config
from .docker_env import TaskContainer
from .subtask_schema import Subtask

log = logging.getLogger("quikode.scope_review")

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


@dataclass
class ScopeReviewResult:
    """Outcome of a scope-review agent call.

    `legitimate` drives whether the worker proceeds with the commit
    (`True` → commit lands) or rolls it back and feeds the reason to
    triage (`False` → re-prompt the doer with the reviewer's reasoning).
    `accepted_files` records the effective lane for legitimacy cases —
    surfaced via `quikode show` so the operator can see how a subtask's
    scope evolved across attempts.
    """

    legitimate: bool
    reason: str
    accepted_files: list[str] = field(default_factory=list)


def review_scope_drift(
    *,
    cfg: Config,
    handle: TaskContainer,
    subtask: Subtask,
    declared: list[str],
    actually_touched: list[str],
    role: AgentRole | None = None,
    log_path: Path | None = None,
    timeout: int = 180,
) -> ScopeReviewResult:
    """Decide whether the doer's commit drift is legitimate.

    Skip the agent call entirely when `actually_touched ⊆ declared`
    (no out-of-lane files) — that's strictly within the lane, no
    reasoning needed. Cheap path for the common case.

    On agent infra failure (rc != 0, output unparseable), default
    LEGITIMATE with a note. Better to ship a borderline commit than
    block the whole run on reviewer availability.
    """
    declared_set = set(declared)
    actual_set = set(actually_touched)
    if actual_set <= declared_set:
        return ScopeReviewResult(
            legitimate=True,
            reason="actual diff is a subset of declared lane",
            accepted_files=sorted(actual_set),
        )

    out_of_lane = sorted(actual_set - declared_set)
    missing = sorted(declared_set - actual_set)
    role = role or cfg.progress  # fast/cheap reviewer (codex gpt-5.4-mini)

    try:
        prompt = prompts_mod.render(
            cfg,
            "scope-review.md",
            subtask=subtask,
            declared=list(declared),
            actually_touched=list(actually_touched),
            out_of_lane=out_of_lane,
            missing=missing,
        )
    except Exception as e:
        log.warning("scope-review prompt render failed: %s; defaulting to LEGITIMATE", e)
        return ScopeReviewResult(
            legitimate=True,
            reason=f"scope-review prompt render error: {e}; defaulted LEGITIMATE",
            accepted_files=list(actually_touched),
        )

    agent = build_agent(role)
    result = agent.run(prompt, handle=handle, log_path=log_path, timeout=timeout)
    if not result.ok:
        log.warning(
            "scope-review agent rc=%s; defaulting to LEGITIMATE for subtask %s",
            result.rc,
            subtask.id,
        )
        return ScopeReviewResult(
            legitimate=True,
            reason=f"scope-review agent rc={result.rc}; defaulted LEGITIMATE",
            accepted_files=list(actually_touched),
        )

    parsed = _parse_envelope(result.stdout)
    if parsed is None:
        log.warning(
            "scope-review output unparseable for subtask %s; defaulting to LEGITIMATE",
            subtask.id,
        )
        return ScopeReviewResult(
            legitimate=True,
            reason="scope-review output unparseable; defaulted LEGITIMATE",
            accepted_files=list(actually_touched),
        )

    legitimate = bool(parsed.get("legitimate", True))
    reason = str(parsed.get("reason", "")).strip() or "(no reason given)"
    raw_accepted = parsed.get("accepted_files", actually_touched)
    if not isinstance(raw_accepted, list):
        raw_accepted = list(actually_touched)
    accepted = [str(p) for p in raw_accepted if isinstance(p, (str, int, float))]
    return ScopeReviewResult(
        legitimate=legitimate,
        reason=reason,
        accepted_files=accepted if legitimate else list(declared),
    )


def _parse_envelope(text: str) -> dict | None:
    """Pull the first JSON object out of the agent's response."""
    if not text or not text.strip():
        return None
    m = _FENCED_JSON_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Fallback: first balanced { ... } block.
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None
