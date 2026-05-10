"""Retry-cause classification.

When a subtask retries, we want to know *why* — was it a real checker
failure (the doer produced bad code), an infra wobble (container vanished,
codex CLI timeout), a rate-limit, a pre-commit lint catch, or a network
flake on `git push`?

Without this signal an operator looking at "R-0019 F-1-1-install-actionlint
retried 17 times" can't tell whether the system is making progress against
a hard problem or burning cycles against a transient infra issue. Both
shapes have the same *count*; only the *cause* distinguishes them.

The classifier is heuristic — patterns over (rc, stderr, stdout) — and
deliberately conservative: an unknown failure shape lands in `other` rather
than guessing a category. Better to under-classify than mislead the
operator.

Categories — keep this list stable; the audit table stores raw strings:

  doer_output_invalid     — doer ran, output didn't satisfy checker
                             (the most common "real" retry)
  checker_fail            — checker emitted a structured FAIL verdict
                             (semantically the same as doer_output_invalid;
                             distinct because it's the explicit "checker
                             ran, voted FAIL" path)
  checker_timeout         — codex/cli timed out before emitting a verdict
  container_oom           — docker exit 137 / OOMKilled
  container_vanished      — `docker exec` against a stopped container
  agent_cli_rate_limit    — rate-limit signature in agent stderr
  pre_commit_hook_fail    — lefthook returned non-zero
  network_timeout         — git push / gh CLI hit network error
  other                   — unclassified
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final, Literal

from quikode.types import Verdict

RetryCategory = Literal[
    "doer_output_invalid",
    "checker_fail",
    "checker_timeout",
    "container_oom",
    "container_vanished",
    "agent_cli_rate_limit",
    "pre_commit_hook_fail",
    "network_timeout",
    "other",
]

# All-caps tuple for `quikode show` rendering + the audit-summary helper.
ALL_CATEGORIES: Final[tuple[RetryCategory, ...]] = (
    "doer_output_invalid",
    "checker_fail",
    "checker_timeout",
    "container_oom",
    "container_vanished",
    "agent_cli_rate_limit",
    "pre_commit_hook_fail",
    "network_timeout",
    "other",
)


# Pattern dictionary. Order matters — earliest match wins. Keep the most
# specific patterns above the generic ones.
#
# Each entry: (compiled regex, category, signature_extractor)
# `signature_extractor` reads a match object and returns a one-line
# fingerprint that's useful in `quikode show` ("rate-limit: 429 too many
# requests on /v1/messages"). Falls back to a generic "<category>: rc=N" if
# nothing pattern-specific is captured.
_PATTERNS: list[tuple[re.Pattern[str], RetryCategory, str | None]] = [
    # Anthropic / Claude rate limit
    (
        re.compile(r"\b429\b.*\b(rate.?limit|too.?many.?requests)\b", re.IGNORECASE),
        "agent_cli_rate_limit",
        None,
    ),
    (
        re.compile(r"\brate.?limit(?:ed|ing)?\b.*\b(retry.?after|reset.?at)\b", re.IGNORECASE),
        "agent_cli_rate_limit",
        None,
    ),
    (re.compile(r"\b(quota|usage limit) exceeded\b", re.IGNORECASE), "agent_cli_rate_limit", None),
    # Container OOM kill (docker)
    (re.compile(r"OOMKilled|out of memory|exit code 137|exit status 137"), "container_oom", None),
    # Container vanished (docker exec on dead container)
    (
        re.compile(
            r"(no such container|container .* is not running|Error: No such container)", re.IGNORECASE
        ),
        "container_vanished",
        None,
    ),
    # Network errors on git/gh
    (
        re.compile(
            r"(could not resolve host|connection (timed out|refused)|temporary failure in name resolution|network is unreachable|tls handshake timeout)",
            re.IGNORECASE,
        ),
        "network_timeout",
        None,
    ),
    # Lefthook / pre-commit hook
    (
        re.compile(r"(lefthook .*FAIL|pre-commit hook .*failed|hook .*returned non-zero)", re.IGNORECASE),
        "pre_commit_hook_fail",
        None,
    ),
    # Codex/agent timeout
    (
        re.compile(r"(timed out after \d+ ?s|timeout.*exceeded|context deadline exceeded)", re.IGNORECASE),
        "checker_timeout",
        None,
    ),
]


def classify_retry(
    *,
    rc: int | None,
    stderr: str = "",
    stdout: str = "",
    hint: str | None = None,
    verdict: Verdict | str | None = None,
    failure_layer: str | None = None,
) -> tuple[RetryCategory, str]:
    """Classify a single retry. Returns (category, short_signature).

    `hint` is an optional caller-supplied tag — e.g. "checker" / "doer" /
    "pre_commit" — used to disambiguate when patterns aren't decisive.

    `verdict` is the structured checker verdict (`Verdict.FAIL`, `"FAIL"`,
    `"PASS"`, or `None`). When `hint="checker"` and `verdict` is provided
    the classifier no longer scrapes rendered text — it goes straight to
    `("checker_fail", "verdict=FAIL")`. Plan 47 retired the doer envelope
    so all checker FAILs share a structurally identical artifact body;
    plan 48 layers the structured verdict on top so the signature carries
    real information.

    `failure_layer` is the structured triage failure layer
    (`local_ci`, `rubric`, `standards`, `architecture`, `behavior`,
    `parse_failure`, `transport`) when triage produced a
    `SubtaskTriageOutput`, else `None`. When provided AND the resulting
    category is a work-content failure (`checker_fail` /
    `doer_output_invalid`), the layer is embedded in the signature so the
    plan-23 same-signature stop-loss compares attempts at the layer
    granularity rather than treating every checker FAIL as identical.
    """
    blob = "\n".join(s for s in (stderr or "", stdout or "") if s)
    sig_default = f"rc={rc if rc is not None else '?'}"

    if rc == 137:
        category: RetryCategory = "container_oom"
        signature = "rc=137 (OOMKilled)"
    else:
        category, signature = _classify_retry_blob(blob, sig_default, hint, rc, verdict)
    if category in ("checker_fail", "doer_output_invalid") and failure_layer:
        signature = f"{signature},layer={failure_layer}"
    return (category, signature)


def _classify_retry_blob(
    blob: str,
    sig_default: str,
    hint: str | None,
    rc: int | None,
    verdict: Verdict | str | None,
) -> tuple[RetryCategory, str]:
    for pat, category, _sig_ex in _PATTERNS:
        m = pat.search(blob)
        if m:
            snippet = blob[max(0, m.start() - 15) : m.end() + 30].replace("\n", " ").strip()
            return (category, snippet[:120] or sig_default)
    hint_result = _classify_retry_hint(sig_default, hint, verdict)
    if hint_result is not None:
        return hint_result
    if rc is None:
        return ("other", "rc=None")
    return ("other", sig_default)


def _classify_retry_hint(
    sig_default: str, hint: str | None, verdict: Verdict | str | None
) -> tuple[RetryCategory, str] | None:
    if hint == "pre_commit":
        return ("pre_commit_hook_fail", sig_default)
    if hint == "network":
        return ("network_timeout", sig_default)
    if hint != "checker":
        return None
    verdict_name = _verdict_name(verdict)
    if verdict_name == "FAIL":
        return ("checker_fail", "verdict=FAIL")
    return ("doer_output_invalid", sig_default)


def _verdict_name(verdict: Verdict | str | None) -> str | None:
    if verdict is None:
        return None
    if isinstance(verdict, Verdict):
        return verdict.name
    return str(verdict).upper()


# ----- summary helpers used by `quikode show` -----


def histogram(reasons: list[dict]) -> dict[RetryCategory, int]:
    """Count category occurrences across a list of `retry_reasons` rows."""
    counts: dict[RetryCategory, int] = {}
    for r in reasons:
        cat = r.get("category", "other")
        if cat in ALL_CATEGORIES:
            counts[cat] = counts.get(cat, 0) + 1
        else:
            counts["other"] = counts.get("other", 0) + 1
    return counts


def format_histogram(counts: Mapping[Any, int]) -> str:
    """Render a category histogram as `category=N category=N` for quikode show."""
    if not counts:
        return ""
    return " ".join(f"{cat}={n}" for cat, n in sorted(counts.items(), key=lambda kv: -kv[1]))
