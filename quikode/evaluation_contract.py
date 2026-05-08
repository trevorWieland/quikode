"""Plan 33: the single shared `EvaluationContract` for a task.

Every upstream agent in the pipeline (planner, doer, checker, triage,
fixup-planner, merge-planner) needs to see — verbatim — the four-stage
audit gauntlet they will be graded against. Today they each operate on
a degraded shadow: they know "there is an audit" but they don't see the
rubric. This module is the fix.

The contract is built once at `PROVISIONING → PLANNING`, persisted to
the per-task state directory, and loaded by every prompt-render entry
point. Pure dataclasses + frozen=True so once persisted, the contract
is stable across reads (deterministic JSON shape).

Lifecycle:
  - `build_for(node, cfg) -> EvaluationContract` — pure constructor.
  - `EvaluationContract.persist(state_dir, task_id) -> None` — writes
    `<state_dir>/<task_id>/evaluation_contract.json`.
  - `EvaluationContract.load(state_dir, task_id) -> EvaluationContract`
    — reads the JSON back.

The four `StageRubric` instances carry the verbatim grading templates
lifted from `prompts/pre-pr-{rubric,standards,behavior}.md` so a single
copy ships into every upstream context window.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .config import Config
    from .dag import Node

log = logging.getLogger("quikode.evaluation_contract")

StageName = Literal["local_ci", "rubric", "standards", "behavior"]

_STANDARDS_CHAR_CAP = 60_000
_TRUNCATION_MARKER_FMT = "\n[STANDARDS DOC TRUNCATED at line {n} of {m}]\n"

# Verbatim grading-template fragments. Each is the JSON-schema portion
# of the corresponding `prompts/pre-pr-*.md` agent prompt — the bar an
# upstream agent must steer their work against. Lifted into the contract
# so the planner and doer see the *same* template the audit grader does.

_RUBRIC_GRADING_TEMPLATE = """\
For each rubric category, the audit grader emits:

```json
{
  "name": "<category>",
  "score": <integer 1-10>,
  "rationale": "<one to three sentences>",
  "gaps_to_reach_ten": [
    {"id": "<kebab-case>", "description": "...", "concrete_fix": "...",
     "files": ["<repo-relative path>"]}
  ]
}
```

The gate fails when ANY category scores below the threshold. The grader's
job is exhaustive enumeration of every gap (even on a passing category) —
not pass/fail triage. Plan to make every category clear the threshold
on cycle 1; supply concrete code/tests/docs that make each rubric
dimension defensible at the threshold-or-better level.
"""

_STANDARDS_GRADING_TEMPLATE = """\
For each standards alignment finding, the audit grader emits:

```json
{
  "id": "<kebab-case>",
  "file": "<repo-relative path>",
  "line": <integer or null>,
  "severity": "low" | "medium" | "high" | "critical",
  "standards_doc_ref": "<doc + section>",
  "description": "...",
  "concrete_fix": "..."
}
```

The gate fails when ANY finding has severity >= medium. Every diff hunk
is checked against every relevant standards section — implementation
that matches the canonical text wins; implementation that drifts
generates a finding. Pin standards refs in your subtask declarations
(`standards_referenced`) so the per-subtask checker can verify alignment
against the same passages the audit will read.
"""

_BEHAVIOR_GRADING_TEMPLATE = """\
For each `expected_evidence` id, the audit grader emits:

```json
{
  "behavior_id": "<id>",
  "verified": true | false,
  "evidence_seen": "<concrete observation>",
  "gap_explanation": "<for verified=false: what's missing>",
  "concrete_fix": "<for verified=false: file/test/assertion>",
  "completeness_gaps": [{"id": "...", "description": "...", "concrete_fix": "..."}]
}
```

The gate fails when ANY behavior is `verified=false`. The grader runs
the witness command empirically — it doesn't read the diff and reason
about whether the code "looks right". Every claimed witness must
*actually run* and produce substantive output for the corresponding
behavior to clear the gate.
"""

_LOCAL_CI_GRADING_TEMPLATE = """\
The local-CI gate runs:

```
{cmd}
```

inside the worktree's container. The gate passes iff rc=0 — no exception,
no narrative-only soft-pass. Compile / lint / fmt / unit-test / migration
runner / line-budget / dep-boundary checks all run from this single
command; a failure anywhere is the gate's failure.
"""


@dataclass(frozen=True)
class StageRubric:
    """One stage of the four-stage audit gauntlet.

    `name` is the canonical stage id. `one_line` is a humanizing summary
    used at the top of the stage card. `threshold` declares the bar
    (verbatim — "rc=0", "every category >= 7", etc). `grading_template`
    is the JSON schema fragment the audit grader produces against;
    upstream agents read it to understand *exactly* the shape they're
    being graded into. `source_text` is the canonical source the stage
    references (rubric category list with blurbs, standards doc text,
    or expected_evidence witness list).
    """

    name: StageName
    one_line: str
    threshold: str
    grading_template: str
    source_text: str


@dataclass(frozen=True)
class EvaluationContract:
    """The full four-stage rubric for one task.

    Built exactly once at PROVISIONING → PLANNING, persisted to the
    per-task state directory, loaded fresh on every prompt-render. The
    same contract object flows through planner, doer, checker, triage,
    fixup-planner, merge-planner, and pre-PR audit — single source of
    truth.
    """

    task_id: str
    local_ci: StageRubric
    rubric: StageRubric
    standards: StageRubric
    behavior: StageRubric

    # ----- persistence -----

    def persist(self, state_dir: Path, task_id: str) -> Path:
        """Write `<state_dir>/<task_id>/evaluation_contract.json`.

        Idempotent: overwrites any prior copy. Returns the path written.
        """
        target_dir = state_dir / task_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "evaluation_contract.json"
        target.write_text(json.dumps(_to_jsonable(self), indent=2, sort_keys=True))
        return target

    @classmethod
    def load(cls, state_dir: Path, task_id: str) -> EvaluationContract:
        """Read back the persisted contract for `task_id`.

        Raises `FileNotFoundError` when the contract was never built (the
        worker should have built it at PROVISIONING → PLANNING; missing
        is a real bug, not a soft-default).
        """
        target = state_dir / task_id / "evaluation_contract.json"
        if not target.exists():
            raise FileNotFoundError(
                f"evaluation contract not found at {target}; build_for+persist must run before load"
            )
        raw = json.loads(target.read_text())
        return _from_jsonable(raw)


def _to_jsonable(contract: EvaluationContract) -> dict[str, object]:
    """Stable dict shape: explicit field ordering for deterministic JSON."""
    return {
        "task_id": contract.task_id,
        "local_ci": asdict(contract.local_ci),
        "rubric": asdict(contract.rubric),
        "standards": asdict(contract.standards),
        "behavior": asdict(contract.behavior),
    }


_VALID_STAGE_NAMES: tuple[StageName, ...] = ("local_ci", "rubric", "standards", "behavior")


def _coerce_stage_name(value: object) -> StageName:
    text = str(value)
    if text not in _VALID_STAGE_NAMES:
        raise ValueError(f"unknown stage name {text!r}; expected one of {list(_VALID_STAGE_NAMES)!r}")
    # Re-cast through the known-literal tuple so the static checker can
    # narrow the result to the StageName Literal alias without an inline
    # type-ignore.
    for known in _VALID_STAGE_NAMES:
        if known == text:
            return known
    raise ValueError(f"unreachable: stage name {text!r} passed allowlist but not matched")


def _from_jsonable(raw: dict[str, object]) -> EvaluationContract:
    def _get_str(blob: dict[str, object], key: str, default: str = "") -> str:
        v = blob.get(key)
        if v is None:
            return default
        return str(v)

    def _stage(blob: object) -> StageRubric:
        if not isinstance(blob, dict):
            raise ValueError(f"expected stage dict, got {type(blob).__name__}")
        # `blob` here came from json.loads which produces dict[str, Any];
        # the static checker narrows it to a bare `dict` without value
        # type info, so we re-typed via the `dict[str, object]` cast below.
        typed: dict[str, object] = {str(k): v for k, v in blob.items()}
        return StageRubric(
            name=_coerce_stage_name(typed.get("name")),
            one_line=_get_str(typed, "one_line"),
            threshold=_get_str(typed, "threshold"),
            grading_template=_get_str(typed, "grading_template"),
            source_text=_get_str(typed, "source_text"),
        )

    return EvaluationContract(
        task_id=str(raw["task_id"]),
        local_ci=_stage(raw["local_ci"]),
        rubric=_stage(raw["rubric"]),
        standards=_stage(raw["standards"]),
        behavior=_stage(raw["behavior"]),
    )


# ----- builder -----


def build_for(node: Node, cfg: Config) -> EvaluationContract:
    """Construct the contract for one task. Pure function: no I/O on
    cfg/node, but DOES read the standards docs from disk under
    `cfg.repo_path` matching `cfg.pre_pr_standards_profile_globs`.

    Idempotent in shape (same `(node, cfg)` → equal contract), but the
    standards source_text reflects on-disk state at build time. Per
    Plan 33 D1, this is called exactly once at PROVISIONING → PLANNING.
    """
    return EvaluationContract(
        task_id=node.id,
        local_ci=_build_local_ci(cfg),
        rubric=_build_rubric(cfg),
        standards=_build_standards(cfg),
        behavior=_build_behavior(node),
    )


def _build_local_ci(cfg: Config) -> StageRubric:
    cmd = cfg.local_ci_command or "(unset)"
    return StageRubric(
        name="local_ci",
        one_line="Build / lint / test / fmt / migration / line-budget gate. Single command.",
        threshold="rc=0",
        grading_template=_LOCAL_CI_GRADING_TEMPLATE.format(cmd=cmd),
        source_text=f"Command: `{cmd}`",
    )


def _build_rubric(cfg: Config) -> StageRubric:
    categories = list(cfg.pre_pr_rubric_categories or [])
    min_score = int(cfg.pre_pr_rubric_min_score)
    if categories:
        rendered = "\n".join(f"- **{cat}**" for cat in categories)
        source_text = (
            "Rubric categories (each scored 1-10; gate passes when every category clears "
            f"the threshold of {min_score}):\n\n{rendered}"
        )
    else:
        # Plan 33 §13: degenerate case. Don't crash; let the validators
        # detect that the planner can't possibly cover an empty rubric.
        source_text = (
            "(no rubric categories configured for this workspace; the planner cannot "
            "advance any rubric category and the validators will fail accordingly)"
        )
    return StageRubric(
        name="rubric",
        one_line="Code-quality dimensions (security, scalability, maintainability, ...). Per-category 1-10 score.",
        threshold=f"every category >= {min_score}",
        grading_template=_RUBRIC_GRADING_TEMPLATE,
        source_text=source_text,
    )


def _build_standards(cfg: Config) -> StageRubric:
    body, _truncated = _gather_standards_text(cfg)
    return StageRubric(
        name="standards",
        one_line="Repo-specific architectural alignment with the standards docs (canonical text below).",
        threshold="no drift from any cited section",
        grading_template=_STANDARDS_GRADING_TEMPLATE,
        source_text=body,
    )


def _build_behavior(node: Node) -> StageRubric:
    if node.expected_evidence:
        rendered_lines: list[str] = []
        for ev in node.expected_evidence:
            ev_id = _evidence_canonical_id(ev)
            kind = ev.get("kind", "")
            interfaces = ev.get("interfaces") or ()
            witnesses = ev.get("witnesses") or ()
            description = ev.get("description", "")
            iface_str = f" interfaces={list(interfaces)!r}" if interfaces else ""
            witn_str = f" witnesses={list(witnesses)!r}" if witnesses else ""
            rendered_lines.append(f"- `{ev_id}` ({kind}){iface_str}{witn_str}: {description}")
        source_text = (
            "Expected behavior witnesses (each id must be empirically observable;\n"
            "the audit gate runs the witness command and asserts substantive output):\n\n"
            + "\n".join(rendered_lines)
        )
    else:
        source_text = (
            "(no expected_evidence on this node; the behavior gate has nothing to verify "
            "and downstream gates carry the load — typical for non-behavior tasks)"
        )
    return StageRubric(
        name="behavior",
        one_line="Empirical witness verification: each expected_evidence id must run and produce real output.",
        threshold="every expected_evidence id verified=true with non-stub output",
        grading_template=_BEHAVIOR_GRADING_TEMPLATE,
        source_text=source_text,
    )


def _evidence_canonical_id(ev: dict) -> str:
    """The canonical id used to reference one expected_evidence entry.

    Order of precedence: `id` field if present (future-proof), else
    `behavior_id` (current convention in tanren's DAG), else a synthesized
    `ev-<kind>-<index-shaped-hash>` fallback. Stable per-(node, ev) so a
    rebuild produces the same id.
    """
    explicit = ev.get("id")
    if isinstance(explicit, str) and explicit:
        return explicit
    bid = ev.get("behavior_id")
    if isinstance(bid, str) and bid:
        kind = ev.get("kind", "evidence")
        # Avoid id collisions between two evidence rows with the same
        # behavior_id but different kinds (positive vs. falsification).
        witnesses = ev.get("witnesses") or ()
        if witnesses:
            joined = "-".join(str(w) for w in witnesses)
            return f"{bid}-{kind}-{joined}"
        return f"{bid}-{kind}"
    # Fallback: hash-free synthesized id. Caller is responsible for
    # ensuring evidence rows on a node are otherwise unique; if not, the
    # planner's `validate_evidence_partition` will surface duplicates.
    kind = ev.get("kind", "evidence")
    desc = (ev.get("description") or "")[:32].strip().replace(" ", "-").lower()
    return f"ev-{kind}-{desc}" if desc else f"ev-{kind}"


def _resolve_standards_paths(cfg: Config) -> list[Path]:
    """Resolve every standards doc matching the configured globs, in
    sorted-path order, deduped. Returns possibly-empty list of files."""
    repo = Path(cfg.repo_path)
    globs = list(cfg.pre_pr_standards_profile_globs or [])
    seen: set[Path] = set()
    paths: list[Path] = []
    for pattern in globs:
        for p in sorted(repo.glob(pattern)):
            if p.is_file() and p not in seen:
                seen.add(p)
                paths.append(p)
    return paths


def _read_path_lines(path: Path) -> tuple[str, list[str]] | None:
    """Read a file as text + per-line list. Logs and returns None on OSError."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.warning("standards: cannot read %s: %s", path, e)
        return None
    return text, text.splitlines()


def _accept_lines_under_budget(lines: list[str], budget: int) -> tuple[str, int]:
    """Greedily accept lines until adding one more would exceed `budget`.
    Returns (joined-text, lines-accepted). Newlines re-inserted between
    lines (one '\n' per joined pair)."""
    accepted: list[str] = []
    accepted_len = 0
    for line in lines:
        line_len = len(line) + 1  # newline
        if accepted_len + line_len > budget:
            break
        accepted.append(line)
        accepted_len += line_len
    return "\n".join(accepted), len(accepted)


def _gather_standards_text(cfg: Config) -> tuple[str, bool]:
    """Read every doc matching `cfg.pre_pr_standards_profile_globs`, in
    sorted-path order. Cap the combined text at 60k chars; truncate at
    line boundaries with a marker. Returns (text, truncated).
    """
    repo = Path(cfg.repo_path)
    paths = _resolve_standards_paths(cfg)
    if not paths:
        return ("(no standards documents matched the configured globs)", False)

    chunks: list[str] = []
    total_chars = 0
    truncated = False
    total_lines = 0
    captured_lines = 0
    for path in paths:
        read_result = _read_path_lines(path)
        if read_result is None:
            continue
        text, path_lines = read_result
        total_lines += len(path_lines)
        if truncated:
            continue
        rel = path.relative_to(repo) if path.is_relative_to(repo) else path
        header = f"\n\n=== {rel} ===\n\n"
        budget = _STANDARDS_CHAR_CAP - total_chars - len(header)
        if budget <= 0:
            truncated = True
            continue
        if len(text) <= budget:
            chunks.append(header)
            chunks.append(text)
            total_chars += len(header) + len(text)
            captured_lines += len(path_lines)
            continue
        accepted_text, accepted_count = _accept_lines_under_budget(path_lines, budget)
        chunks.append(header)
        chunks.append(accepted_text)
        total_chars += len(header) + len(accepted_text) + 1
        captured_lines += accepted_count
        truncated = True

    body = "".join(chunks).strip("\n")
    if truncated:
        log.warning(
            "evaluation_contract: standards corpus truncated at %d/%d lines (60k char cap)",
            captured_lines,
            total_lines,
        )
        body += _TRUNCATION_MARKER_FMT.format(n=captured_lines, m=total_lines)
    return body, truncated
