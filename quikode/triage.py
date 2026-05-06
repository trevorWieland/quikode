"""v3.5 Phase B: Python-deterministic triage for post-PR feedback.

Two complementary surfaces:

1. **CI log parser** (`parse_ci_failure`). Pure-python pattern matching for
   `cargo check`, `cargo test`, `clippy`, `ruff`, `pytest`, and a generic
   `path:line: error: ...` fallback. Output is a list of `CIFailure` —
   structured (file, line, kind, message, excerpt) — that the fixup planner
   can scope subtasks against, instead of staring at 80 lines of raw log.

2. **Review-thread classifier** (`classify_review_thread`). One sonnet call
   per thread; returns `{correct, incorrect, needs_discussion}`. INCORRECT
   threads carry a polite auto-reply text the orchestrator posts via `gh`
   before resolving the thread. Only CORRECT threads ever reach the
   ADDRESSING_FEEDBACK worker — the planner gets a clean queue.

Invoked from the orchestrator's review-watcher, in-process, no Docker
container needed (host-side `claude -p`). Bounded in seconds; replaces the
"30 minutes stuck in feedback handling" failure mode.

Returns `TriageOutcome` aggregating all the categorized items so the
orchestrator can persist counts + dispatch only what needs work.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from . import prompts as prompts_mod
from .config import AgentRole, Config
from .github_graphql import ReviewThread

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
    # Walk backward N newlines.
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

    # Cap the result to avoid sending hundreds of duplicates downstream.
    return out[:25]


# ----- Review-thread classifier -----


@dataclass(frozen=True)
class ReviewVerdict:
    """One classifier output per review thread."""

    thread_id: str
    verdict: Literal["correct", "incorrect", "needs_discussion"]
    rationale: str
    reply: str  # polite auto-reply text for INCORRECT, else empty


@dataclass
class TriageOutcome:
    """Aggregate of triage decisions across all threads + CI failures."""

    actionable_threads: list[ReviewThread] = field(default_factory=list)
    auto_resolved: list[tuple[ReviewThread, ReviewVerdict]] = field(default_factory=list)
    deferred: list[tuple[ReviewThread, ReviewVerdict]] = field(default_factory=list)
    ci_failures: list[CIFailure] = field(default_factory=list)
    # If the classifier itself failed (timeout, parse error), the thread
    # falls through to actionable so we don't silently drop work.
    classifier_errors: int = 0


_JSON_OBJECT_RE = re.compile(r"\{.*?\}", re.DOTALL)


def _parse_classifier_envelope(stdout: str) -> ReviewVerdict | None:
    """Best-effort parse of the classifier's JSON envelope. None on failure."""
    if not stdout or not stdout.strip():
        return None
    snippet = stdout.strip()
    # claude -p --output-format json wraps the assistant text under "result".
    try:
        outer = json.loads(snippet)
        inner_text = outer.get("result") if isinstance(outer, dict) else None
        if inner_text is None:
            inner_text = snippet
    except json.JSONDecodeError:
        inner_text = snippet
    # Now find the verdict JSON inside.
    candidates = [inner_text.strip()]
    m = _JSON_OBJECT_RE.search(inner_text)
    if m:
        candidates.append(m.group(0))
    for cand in candidates:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        verdict = data.get("verdict")
        if verdict not in ("correct", "incorrect", "needs_discussion"):
            continue
        # thread_id is filled by caller — classifier output uses placeholder
        return ReviewVerdict(
            thread_id="",
            verdict=verdict,
            rationale=str(data.get("rationale", ""))[:500],
            reply=str(data.get("reply", ""))[:1500],
        )
    return None


def _invoke_classifier_host(prompt: str, role: AgentRole, *, timeout: int = 60) -> str | None:
    """Run `claude -p --output-format json` on the host (no container). Returns
    raw stdout or None on failure. Sonnet-class invocations don't need the
    workspace mount — they're pure prompt → JSON.
    """
    cmd = ["claude", "-p", "--output-format", "json"]
    if role.model:
        cmd += ["--model", role.model]
    cmd += list(role.extra_args)
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("review classifier subprocess raised: %s", e)
        return None
    if proc.returncode != 0:
        log.warning(
            "review classifier rc=%d stderr=%s",
            proc.returncode,
            (proc.stderr or "")[:200],
        )
        return None
    return proc.stdout


def classify_review_thread(
    *,
    thread: ReviewThread,
    cfg: Config,
    plan_text: str,
    recent_diff_excerpt: str = "",
    role: AgentRole | None = None,
) -> ReviewVerdict | None:
    """Run the classifier for a single thread. Returns None on failure.

    Caller treats None as "fall through to ADDRESSING_FEEDBACK" — never
    silently drop a thread because the classifier hiccupped.
    """
    role = role or cfg.intent_reviewer
    try:
        prompt = prompts_mod.render(
            cfg,
            "review-classifier.md",
            plan_text=plan_text,
            thread_path=thread.path or "(no path)",
            thread_line=thread.line,
            thread_author=thread.last_comment_author or "(unknown)",
            thread_is_bot="yes" if thread.last_comment_is_bot else "no",
            thread_body=(thread.last_comment_body or "").strip(),
            recent_diff_excerpt=recent_diff_excerpt[:3000],
        )
    except Exception as e:
        log.warning("review-classifier prompt render failed for thread %s: %s", thread.thread_id, e)
        return None
    stdout = _invoke_classifier_host(prompt, role)
    if stdout is None:
        return None
    parsed = _parse_classifier_envelope(stdout)
    if parsed is None:
        return None
    return ReviewVerdict(
        thread_id=thread.thread_id,
        verdict=parsed.verdict,
        rationale=parsed.rationale,
        reply=parsed.reply,
    )


def triage_review_threads(
    *,
    cfg: Config,
    plan_text: str,
    threads: list[ReviewThread],
    recent_diff_excerpt: str = "",
    classifier_timeout_total_s: float = 120.0,
) -> TriageOutcome:
    """Classify each unresolved thread; return a structured outcome.

    `classifier_timeout_total_s` caps the total wall time spent classifying
    so the in-process triage step stays bounded even if a few calls slow
    down. Threads not classified by the deadline fall through to
    actionable (safe default).
    """
    outcome = TriageOutcome()
    deadline = time.time() + classifier_timeout_total_s
    for t in threads:
        if t.is_resolved:
            # Resolved threads never reach this code path in normal operation
            # (`_classify_threads` already filters them) — defensive skip.
            continue
        if time.time() > deadline:
            log.warning(
                "review classifier deadline exceeded — falling through %d remaining thread(s) to actionable",
                len([x for x in threads if x is t or threads.index(x) > threads.index(t)]),
            )
            outcome.actionable_threads.append(t)
            continue
        verdict = classify_review_thread(
            thread=t,
            cfg=cfg,
            plan_text=plan_text,
            recent_diff_excerpt=recent_diff_excerpt,
        )
        if verdict is None:
            outcome.classifier_errors += 1
            outcome.actionable_threads.append(t)
            continue
        if verdict.verdict == "correct":
            outcome.actionable_threads.append(t)
        elif verdict.verdict == "incorrect":
            outcome.auto_resolved.append((t, verdict))
        else:  # needs_discussion
            outcome.deferred.append((t, verdict))
    return outcome


# ----- gh helpers used by the orchestrator's auto-reply path -----


def post_thread_reply(
    *,
    repo: str,
    pr_number: int,
    review_id: str,
    body: str,
    repo_path: Path,
    timeout: int = 30,
) -> bool:
    """Post a reply to a review-thread's existing comment.

    Returns True on success, False on any failure (logged). Uses
    `gh api` via the existing CLI infrastructure to avoid pulling in a
    direct HTTP client. The `review_id` is the GitHub REST `id` of the
    head comment of the thread (caller must derive — see
    `github.py:get_review_comment_root_id` if available, otherwise
    fall back to the GraphQL thread_id which won't work for /replies).
    """
    cmd = [
        "gh",
        "api",
        "-X",
        "POST",
        f"/repos/{repo}/pulls/{pr_number}/comments/{review_id}/replies",
        "-f",
        f"body={body}",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("post_thread_reply subprocess raised: %s", e)
        return False
    if proc.returncode != 0:
        log.warning(
            "post_thread_reply rc=%d stderr=%s",
            proc.returncode,
            (proc.stderr or "")[:300],
        )
        return False
    return True
