"""Plan 33: SELF_AUDIT block parser + deterministic short-circuit.

The doer prompt mandates that every subtask attempt emit a SELF_AUDIT
block in the exact format below. The orchestrator parses that block
deterministically (no YAML — `<...>` placeholder text in literal output
breaks YAML; this is a hand-rolled line-oriented parser) and applies
fast-fail short-circuits before paying for an LLM checker invocation.

## Canonical format (Plan 33 §6.1)

```
SELF_AUDIT:
  gate_local_ci: rc=<n> (cmd: <command>)
  gate_rubric:
    <category>: predicted_score=<n>  rationale: <one line>  evidence: <file:line>
    ...
  gate_standards:
    <doc§section>: aligned (cite paragraph) | drifted (and why fixed)
    ...
  gate_behavior:
    <evidence_id>: witnessed_by=<command run>  output_excerpt=<...>
    ...
  diff_reconcile:
    <file>: in_lane | gate_fix(<gate>) | <fixed_in_place>
```

## Tolerance grammar (what the parser accepts)

* Trailing whitespace anywhere — stripped before matching.
* Two-space OR four-space indentation under section headers — both work.
* Missing optional sub-rows under a section header (`gate_standards:` followed by
  no rows) — accepted; the section dict is just empty.
* Blank lines between sections — accepted.
* Free-form prose BEFORE the `SELF_AUDIT:` anchor — ignored.
* Free-form prose AFTER the block (i.e. after a non-indented non-blank
  line that is NOT a known section header) — terminates the block.

## Hard requirements (parse_errors emitted on violation)

* `SELF_AUDIT:` anchor must appear at the start of a line.
* `gate_local_ci:` must follow with a parseable `rc=<int>` token.
* Each of the four sections (`gate_local_ci`, `gate_rubric`,
  `gate_standards`, `gate_behavior`, `diff_reconcile`) must appear at
  least once. Missing any of them → parse error.
* Per-category rubric rows must include the `predicted_score=<int>`
  token. Missing → parse error.

## Short-circuit (Plan 33 §6.4)

After a clean parse, `short_circuit_decision` runs:
1. `gate_local_ci_rc != 0` → FAIL_FAST(local_ci).
2. Any `gate_rubric[cat].predicted_score < cfg.pre_pr_rubric_min_score`
   → FAIL_FAST(rubric).
3. Any RISK / STUB / TODO / FIXME / XXX token (case-insensitive,
   word-boundary regex) in any rubric / standards / behavior row →
   FAIL_FAST(self_audit_mismatch).
4. Else PROCEED to LLM checker.

The parser is structural only — it doesn't verify that
`evidence: <file:line>` cites refer to actual diff lines (the LLM
checker catches that), and it doesn't validate the standards-section
text against the cited doc.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .evaluation_contract import EvaluationContract
    from .subtask_schema import Subtask


# ---------- structured row dataclasses ----------


@dataclass(frozen=True)
class RubricRow:
    """One per-category row under `gate_rubric:`."""

    category: str
    predicted_score: int | None
    rationale: str
    evidence: str


@dataclass(frozen=True)
class StandardsRow:
    """One row under `gate_standards:`. `aligned` is True iff the row's
    body starts with the literal token `aligned`; otherwise the row is
    treated as drifted (with the body explaining the drift / fix)."""

    doc_section: str
    aligned: bool
    body: str


@dataclass(frozen=True)
class BehaviorRow:
    """One row under `gate_behavior:`."""

    evidence_id: str
    witnessed_by: str
    output_excerpt: str


@dataclass(frozen=True)
class ParsedSelfAudit:
    """Structured view of one parsed SELF_AUDIT block.

    `parse_errors` is non-empty iff parsing failed. A non-empty list
    triggers the re-prompt loop in `subtask_execution.py`; if the second
    attempt also fails, the subtask fails with
    `failure_layer="self_audit_mismatch"`.

    Fields default to safe empties so a degraded parse still produces
    a usable object (the worker iterates `parse_errors` first).
    """

    gate_local_ci_rc: int | None = None
    gate_local_ci_cmd: str = ""
    gate_rubric: dict[str, RubricRow] = field(default_factory=dict)
    gate_standards: dict[str, StandardsRow] = field(default_factory=dict)
    gate_behavior: dict[str, BehaviorRow] = field(default_factory=dict)
    diff_reconcile: dict[str, str] = field(default_factory=dict)
    raw: str = ""
    parse_errors: tuple[str, ...] = ()


# ---------- short-circuit enum + result ----------


class ShortCircuit(Enum):
    """Outcome of the deterministic post-parse short-circuit decision.

    `PROCEED` advances to the LLM checker; `FAIL_FAST` skips directly
    to triage with the embedded `failure_layer` label."""

    PROCEED = "proceed"
    FAIL_FAST = "fail_fast"


@dataclass(frozen=True)
class ShortCircuitResult:
    """Decision returned by `short_circuit_decision`."""

    decision: ShortCircuit
    failure_layer: str | None  # one of local_ci|rubric|self_audit_mismatch on FAIL_FAST
    reason: str  # human-readable detail used in triage prompt


# ---------- parser ----------

_ANCHOR_RE = re.compile(r"^\s*SELF_AUDIT:\s*$")
_KNOWN_SECTIONS = (
    "gate_local_ci",
    "gate_rubric",
    "gate_standards",
    "gate_behavior",
    "diff_reconcile",
)
_SECTION_HEADER_RE = re.compile(
    r"^(?P<indent>\s*)(?P<name>" + "|".join(_KNOWN_SECTIONS) + r")\s*:\s*(?P<rest>.*?)\s*$"
)
_LOCAL_CI_RE = re.compile(r"rc\s*=\s*(?P<rc>-?\d+)\s*\(\s*cmd\s*:\s*(?P<cmd>[^)]*)\)")
_PREDICTED_SCORE_RE = re.compile(r"predicted_score\s*=\s*(?P<score>-?\d+)")
_RATIONALE_RE = re.compile(r"rationale\s*:\s*(?P<rationale>.*?)(?=\s+evidence\s*:|$)", re.DOTALL)
_EVIDENCE_RE = re.compile(r"evidence\s*:\s*(?P<evidence>.*)$", re.DOTALL)
_WITNESSED_BY_RE = re.compile(r"witnessed_by\s*=\s*(?P<cmd>.*?)(?=\s+output_excerpt\s*=|$)", re.DOTALL)
_OUTPUT_EXCERPT_RE = re.compile(r"output_excerpt\s*=\s*(?P<exc>.*)$", re.DOTALL)
_RISK_TOKEN_RE = re.compile(r"\b(RISK|STUB|TODO|FIXME|XXX)\b", re.IGNORECASE)


def _strip_trailing_ws(line: str) -> str:
    return line.rstrip()


def _is_blank(line: str) -> bool:
    return not line.strip()


def _detect_indent(line: str) -> int:
    """Return the count of leading spaces; tabs are forbidden by spec
    (tolerance is 2/4-space only) but we count them as 4 for resilience."""
    n = 0
    for ch in line:
        if ch == " ":
            n += 1
        elif ch == "\t":
            n += 4
        else:
            break
    return n


def _find_anchor(lines: list[str]) -> int:
    """Locate the `SELF_AUDIT:` anchor line, or -1 if not found."""
    for i, line in enumerate(lines):
        if _ANCHOR_RE.match(line):
            return i
    return -1


def _parse_section_header(line: str) -> tuple[str, str, int] | None:
    """Return (section_name, after_colon_text, indent) when the line is
    a known section header; else None."""
    m = _SECTION_HEADER_RE.match(line)
    if not m:
        return None
    return m.group("name"), m.group("rest"), _detect_indent(line)


def _parse_local_ci(rest: str, errors: list[str]) -> tuple[int | None, str]:
    """Parse `rc=<n> (cmd: <command>)` from a `gate_local_ci:` rest payload.

    Tolerant of whitespace; rejects a missing `rc=<n>` token. Returns
    (rc, cmd)."""
    m = _LOCAL_CI_RE.search(rest)
    if not m:
        errors.append(f"gate_local_ci: missing or malformed `rc=<n> (cmd: <command>)` payload (got {rest!r})")
        return None, rest.strip()
    try:
        rc = int(m.group("rc"))
    except ValueError:
        errors.append(f"gate_local_ci: rc value not parseable as int (got {m.group('rc')!r})")
        return None, m.group("cmd").strip()
    return rc, m.group("cmd").strip()


def _parse_rubric_row(line: str, errors: list[str]) -> tuple[str, RubricRow] | None:
    """Parse one `<category>: predicted_score=<n>  rationale: <...>  evidence: <...>` row."""
    stripped = line.strip()
    if ":" not in stripped:
        errors.append(f"gate_rubric: row missing colon between category and body (got {stripped!r})")
        return None
    category, _, body = stripped.partition(":")
    category = category.strip()
    body = body.strip()
    if not category:
        errors.append(f"gate_rubric: row has empty category name (got {stripped!r})")
        return None
    score_m = _PREDICTED_SCORE_RE.search(body)
    score: int | None
    if score_m:
        try:
            score = int(score_m.group("score"))
        except ValueError:
            errors.append(f"gate_rubric/{category}: predicted_score not parseable as int")
            score = None
    else:
        errors.append(f"gate_rubric/{category}: missing `predicted_score=<n>` token in body")
        score = None
    rationale_m = _RATIONALE_RE.search(body)
    rationale = rationale_m.group("rationale").strip() if rationale_m else ""
    evidence_m = _EVIDENCE_RE.search(body)
    evidence = evidence_m.group("evidence").strip() if evidence_m else ""
    return category, RubricRow(
        category=category,
        predicted_score=score,
        rationale=rationale,
        evidence=evidence,
    )


def _parse_standards_row(line: str) -> tuple[str, StandardsRow] | None:
    stripped = line.strip()
    if ":" not in stripped:
        return None
    doc_section, _, body = stripped.partition(":")
    doc_section = doc_section.strip()
    body = body.strip()
    if not doc_section:
        return None
    aligned = body.lower().startswith("aligned")
    return doc_section, StandardsRow(doc_section=doc_section, aligned=aligned, body=body)


def _parse_behavior_row(line: str) -> tuple[str, BehaviorRow] | None:
    stripped = line.strip()
    if ":" not in stripped:
        return None
    evidence_id, _, body = stripped.partition(":")
    evidence_id = evidence_id.strip()
    body = body.strip()
    if not evidence_id:
        return None
    witn_m = _WITNESSED_BY_RE.search(body)
    witnessed_by = witn_m.group("cmd").strip() if witn_m else ""
    excerpt_m = _OUTPUT_EXCERPT_RE.search(body)
    output_excerpt = excerpt_m.group("exc").strip() if excerpt_m else ""
    return evidence_id, BehaviorRow(
        evidence_id=evidence_id,
        witnessed_by=witnessed_by,
        output_excerpt=output_excerpt,
    )


def _parse_diff_reconcile_row(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if ":" not in stripped:
        return None
    file_path, _, body = stripped.partition(":")
    file_path = file_path.strip()
    body = body.strip()
    if not file_path:
        return None
    return file_path, body


def _collect_section_rows(lines: list[str], i: int, base_indent: int) -> tuple[list[str], int]:
    """Collect rows belonging to a section header starting at index `i`.

    A row belongs to the section when its indent is strictly greater
    than `base_indent` (the header's own indent) AND the line is not
    another known section header at the same or shallower indent. Stops
    at end-of-block (a non-indented, non-blank line that isn't a known
    section header — terminates the SELF_AUDIT block entirely).

    Returns (rows, next_index_after_section).
    """
    rows: list[str] = []
    j = i
    while j < len(lines):
        line = lines[j]
        if _is_blank(line):
            j += 1
            continue
        ind = _detect_indent(line)
        # Another section header at shallow / same indent ⇒ end of this section.
        header = _parse_section_header(line)
        if header is not None and header[2] <= base_indent:
            break
        # Non-indented prose that isn't a known header ⇒ end of block.
        if ind <= base_indent and header is None:
            break
        rows.append(line)
        j += 1
    return rows, j


@dataclass
class _ParseState:
    """Mutable accumulator for `parse_self_audit`. Kept private so the
    public `ParsedSelfAudit` stays frozen."""

    gate_local_ci_rc: int | None = None
    gate_local_ci_cmd: str = ""
    gate_rubric: dict[str, RubricRow] = field(default_factory=dict)
    gate_standards: dict[str, StandardsRow] = field(default_factory=dict)
    gate_behavior: dict[str, BehaviorRow] = field(default_factory=dict)
    diff_reconcile: dict[str, str] = field(default_factory=dict)
    seen_sections: set[str] = field(default_factory=set)


def _process_section(
    name: str,
    rows: list[str],
    state: _ParseState,
    errors: list[str],
) -> None:
    """Dispatch one section's rows into the corresponding state dict."""
    if name == "gate_rubric":
        for row in rows:
            parsed = _parse_rubric_row(row, errors)
            if parsed is not None:
                cat, rrow = parsed
                state.gate_rubric[cat] = rrow
    elif name == "gate_standards":
        for row in rows:
            pr = _parse_standards_row(row)
            if pr is not None:
                key, srow = pr
                state.gate_standards[key] = srow
    elif name == "gate_behavior":
        for row in rows:
            pr = _parse_behavior_row(row)
            if pr is not None:
                key, brow = pr
                state.gate_behavior[key] = brow
    elif name == "diff_reconcile":
        for row in rows:
            pr = _parse_diff_reconcile_row(row)
            if pr is not None:
                fpath, status = pr
                state.diff_reconcile[fpath] = status


def parse_self_audit(text: str) -> ParsedSelfAudit:
    """Parse a doer-emitted SELF_AUDIT block. Returns a `ParsedSelfAudit`
    whose `parse_errors` is empty iff every required section was present
    and parseable. See module docstring for the tolerance grammar.
    """
    lines = [_strip_trailing_ws(line) for line in (text or "").splitlines()]
    errors: list[str] = []
    anchor_idx = _find_anchor(lines)
    if anchor_idx < 0:
        return ParsedSelfAudit(
            raw=text or "",
            parse_errors=("SELF_AUDIT: anchor line not found in output",),
        )
    state = _ParseState()
    i = anchor_idx + 1
    while i < len(lines):
        line = lines[i]
        if _is_blank(line):
            i += 1
            continue
        header = _parse_section_header(line)
        if header is None:
            # End of block (prose terminator) per the tolerance grammar.
            break
        name, rest, indent = header
        state.seen_sections.add(name)
        if name == "gate_local_ci":
            state.gate_local_ci_rc, state.gate_local_ci_cmd = _parse_local_ci(rest, errors)
            i += 1
            continue
        rows, i = _collect_section_rows(lines, i + 1, base_indent=indent)
        _process_section(name, rows, state, errors)
    required = set(_KNOWN_SECTIONS)
    missing = sorted(required - state.seen_sections)
    for missing_name in missing:
        errors.append(f"missing required section header `{missing_name}:`")
    return ParsedSelfAudit(
        gate_local_ci_rc=state.gate_local_ci_rc,
        gate_local_ci_cmd=state.gate_local_ci_cmd,
        gate_rubric=state.gate_rubric,
        gate_standards=state.gate_standards,
        gate_behavior=state.gate_behavior,
        diff_reconcile=state.diff_reconcile,
        raw=text or "",
        parse_errors=tuple(errors),
    )


# ---------- short-circuit decision ----------


def _scan_risk_tokens(parsed: ParsedSelfAudit) -> str | None:
    """Return the first row text containing a RISK/STUB/TODO/FIXME/XXX
    token, or None when no such token is present. Word-boundary,
    case-insensitive."""
    for cat, row in parsed.gate_rubric.items():
        for blob in (row.rationale, row.evidence):
            if blob and _RISK_TOKEN_RE.search(blob):
                return f"gate_rubric[{cat}]: {blob[:120]}"
    for key, srow in parsed.gate_standards.items():
        if srow.body and _RISK_TOKEN_RE.search(srow.body):
            return f"gate_standards[{key}]: {srow.body[:120]}"
    for key, brow in parsed.gate_behavior.items():
        for blob in (brow.witnessed_by, brow.output_excerpt):
            if blob and _RISK_TOKEN_RE.search(blob):
                return f"gate_behavior[{key}]: {blob[:120]}"
    return None


def short_circuit_decision(
    parsed: ParsedSelfAudit,
    *,
    contract: EvaluationContract,
    subtask: Subtask,
    rubric_min_score: int,
) -> ShortCircuitResult:
    """Decide whether to skip the LLM checker and run triage immediately.

    Plan 33 §6.4: severity ordering is local_ci > rubric > self_audit_mismatch.
    `rubric_min_score` is the per-category threshold (cfg.pre_pr_rubric_min_score).
    `subtask` is currently unused but accepted so future per-subtask thresholds
    can land without a signature change.
    """
    _ = contract  # parameter reserved for future per-stage threshold lookups
    _ = subtask  # parameter reserved for per-subtask threshold overrides
    if parsed.gate_local_ci_rc is None:
        # Couldn't parse rc — treat as a structural mismatch, not local-CI red.
        return ShortCircuitResult(
            decision=ShortCircuit.FAIL_FAST,
            failure_layer="self_audit_mismatch",
            reason="gate_local_ci: rc could not be parsed; doer must re-emit a clean SELF_AUDIT",
        )
    if parsed.gate_local_ci_rc != 0:
        return ShortCircuitResult(
            decision=ShortCircuit.FAIL_FAST,
            failure_layer="local_ci",
            reason=(
                f"gate_local_ci: rc={parsed.gate_local_ci_rc} (cmd: "
                f"{parsed.gate_local_ci_cmd!r}) — doer reports local CI did not pass"
            ),
        )
    below: list[tuple[str, int]] = []
    for cat, row in parsed.gate_rubric.items():
        if row.predicted_score is None:
            continue
        if row.predicted_score < rubric_min_score:
            below.append((cat, row.predicted_score))
    if below:
        bullets = ", ".join(f"{cat}={score}" for cat, score in below)
        return ShortCircuitResult(
            decision=ShortCircuit.FAIL_FAST,
            failure_layer="rubric",
            reason=(
                f"gate_rubric: {len(below)} category/categories below threshold "
                f"{rubric_min_score} ({bullets})"
            ),
        )
    risk = _scan_risk_tokens(parsed)
    if risk is not None:
        return ShortCircuitResult(
            decision=ShortCircuit.FAIL_FAST,
            failure_layer="self_audit_mismatch",
            reason=f"RISK/STUB/TODO/FIXME/XXX token detected: {risk}",
        )
    return ShortCircuitResult(
        decision=ShortCircuit.PROCEED,
        failure_layer=None,
        reason="self-audit clean: all scores meet threshold, no risk tokens",
    )


__all__ = [
    "BehaviorRow",
    "ParsedSelfAudit",
    "RubricRow",
    "ShortCircuit",
    "ShortCircuitResult",
    "StandardsRow",
    "parse_self_audit",
    "short_circuit_decision",
]
