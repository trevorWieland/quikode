"""Plan 33: the single shared `EvaluationContract` for a task.

Every upstream agent in the pipeline (planner, doer, checker, triage,
fixup-planner, merge-planner) needs to see — verbatim — the audit
gauntlet they will be graded against. Plan 35 widens this from four
stages to FIVE: `local_ci`, `rubric`, `standards`, `architecture`,
`behavior`. The architecture stage grades the diff against this
project's documented subsystem contracts (parallel to standards which
grades against language/framework profile docs).

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

The five `StageRubric` instances carry the verbatim grading templates
lifted from `prompts/pre-pr-{rubric,standards,architecture,behavior}.md`
so a single copy ships into every upstream context window.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from . import evaluation_contract_serde as serde
from .architecture_docs import ArchitectureCorpus, load_architecture, render_architecture_toc
from .standards_profiles import StandardsProfile, load_profiles, render_profile_catalog

if TYPE_CHECKING:
    from .config import Config
    from .dag import Node

log = logging.getLogger("quikode.evaluation_contract")

StageName = Literal["local_ci", "rubric", "standards", "architecture", "behavior"]


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
  "profile_doc_ref": "<doc + section>",
  "description": "...",
  "concrete_fix": "..."
}
```

The gate fails when ANY finding has severity >= medium. Every diff hunk
is checked against every relevant standards profile section —
implementation that matches the canonical text wins; implementation
that drifts generates a finding. Pin standards refs in your subtask
declarations (`standards_referenced`) so the per-subtask checker can
verify alignment against the same passages the audit will read.
Standards refs cite ONLY profile docs (under standards_profiles_dir);
project-architecture concerns belong on the architecture stage.
"""

_ARCHITECTURE_GRADING_TEMPLATE = """\
For each architecture-alignment finding, the audit grader emits:

```json
{
  "id": "<kebab-case>",
  "file": "<repo-relative path>",
  "line": <integer or null>,
  "severity": "low" | "medium" | "high" | "critical",
  "architecture_doc_ref": "<doc + section>",
  "description": "...",
  "concrete_fix": "..."
}
```

The gate fails when ANY finding has severity >= medium. The architecture
audit grades the diff against this project's documented subsystem
contracts: module/subsystem boundaries, undocumented cross-subsystem
coupling, deviations from documented interface contracts, missing
telemetry the architecture mandates. Pin architecture refs in your
subtask declarations (`architecture_referenced`) so the per-subtask
checker can verify alignment against the same passages the audit will
read. Architecture refs cite ONLY project-architecture docs (under
architecture_docs_dir); language/framework standards belong on the
standards stage.
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
    """One generic stage of the audit gauntlet (local_ci, rubric, behavior).

    `name` is the canonical stage id. `one_line` is a humanizing summary
    used at the top of the stage card. `threshold` declares the bar
    (verbatim — "rc=0", "every category >= 7", etc). `grading_template`
    is the JSON schema fragment the audit grader produces against;
    upstream agents read it to understand *exactly* the shape they're
    being graded into. `source_text` is the canonical source the stage
    references (rubric category list with blurbs, expected_evidence
    witness list).
    """

    name: StageName
    one_line: str
    threshold: str
    grading_template: str
    source_text: str


@dataclass(frozen=True)
class StandardsStageRubric:
    """The standards stage carries the loaded profile corpus alongside
    the rendered catalog. `profiles` is consulted by
    `validate_standards_refs` (membership check). `source_text` is the
    rendered catalog used in `ec_full` prompt blocks.
    """

    name: Literal["standards"] = "standards"
    one_line: str = ""
    threshold: str = ""
    grading_template: str = ""
    profiles: tuple[StandardsProfile, ...] = ()
    source_text: str = ""


@dataclass(frozen=True)
class ArchitectureStageRubric:
    """The architecture stage carries the loaded project-architecture
    corpus alongside the rendered TOC. `corpus` is consulted by
    `validate_architecture_refs` (membership check). `source_text` is
    the rendered TOC used in `ec_full` prompt blocks.
    """

    name: Literal["architecture"] = "architecture"
    one_line: str = ""
    threshold: str = ""
    grading_template: str = ""
    corpus: ArchitectureCorpus = field(default_factory=lambda: ArchitectureCorpus(root=Path(), docs=()))
    source_text: str = ""


@dataclass(frozen=True)
class EvaluationContract:
    """The full five-stage rubric for one task.

    Built exactly once at PROVISIONING → PLANNING, persisted to the
    per-task state directory, loaded fresh on every prompt-render. The
    same contract object flows through planner, doer, checker, triage,
    fixup-planner, merge-planner, and pre-PR audit — single source of
    truth.
    """

    task_id: str
    local_ci: StageRubric
    rubric: StageRubric
    standards: StandardsStageRubric
    architecture: ArchitectureStageRubric
    behavior: StageRubric

    # ----- persistence -----

    def persist(self, state_dir: Path, task_id: str) -> Path:
        """Write `<state_dir>/<task_id>/evaluation_contract.json`.

        Idempotent: overwrites any prior copy. Returns the path written.
        """
        target_dir = state_dir / task_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "evaluation_contract.json"
        target.write_text(json.dumps(serde.to_jsonable(self), indent=2, sort_keys=True))
        return target

    # The decoder helpers live as classmethod-adjacent functions below so
    # `load` can call them without a forward-reference dance.

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
        return cls(
            task_id=str(raw["task_id"]),
            local_ci=_decode_stage(raw["local_ci"]),
            rubric=_decode_stage(raw["rubric"]),
            standards=_decode_standards(raw["standards"]),
            architecture=_decode_architecture(raw["architecture"]),
            behavior=_decode_stage(raw["behavior"]),
        )


def _decode_stage(blob: object) -> StageRubric:
    kwargs = serde.stage_kwargs(blob)
    return StageRubric(
        name=_narrow_stage_name(kwargs["name"]),
        one_line=str(kwargs["one_line"]),
        threshold=str(kwargs["threshold"]),
        grading_template=str(kwargs["grading_template"]),
        source_text=str(kwargs["source_text"]),
    )


def _decode_standards(blob: object) -> StandardsStageRubric:
    kwargs = serde.standards_stage_kwargs(blob)
    profiles_raw = kwargs["profiles"]
    profiles: tuple[StandardsProfile, ...] = ()
    if isinstance(profiles_raw, tuple):
        profiles = tuple(p for p in profiles_raw if isinstance(p, StandardsProfile))
    return StandardsStageRubric(
        one_line=str(kwargs["one_line"]),
        threshold=str(kwargs["threshold"]),
        grading_template=str(kwargs["grading_template"]),
        profiles=profiles,
        source_text=str(kwargs["source_text"]),
    )


def _decode_architecture(blob: object) -> ArchitectureStageRubric:
    kwargs = serde.architecture_stage_kwargs(blob)
    corpus = kwargs["corpus"]
    return ArchitectureStageRubric(
        one_line=str(kwargs["one_line"]),
        threshold=str(kwargs["threshold"]),
        grading_template=str(kwargs["grading_template"]),
        corpus=corpus if isinstance(corpus, ArchitectureCorpus) else ArchitectureCorpus(root=Path(), docs=()),
        source_text=str(kwargs["source_text"]),
    )


def _narrow_stage_name(value: object) -> StageName:
    """Narrow a serde-decoded stage name (typed `object`) to StageName."""
    text = str(value)
    if text == "local_ci":
        return "local_ci"
    if text == "rubric":
        return "rubric"
    if text == "standards":
        return "standards"
    if text == "architecture":
        return "architecture"
    if text == "behavior":
        return "behavior"
    raise ValueError(f"unknown stage name {text!r}")


# ----- builder -----


def build_for(node: Node, cfg: Config) -> EvaluationContract:
    """Construct the contract for one task. Pure function except for
    on-disk reads of standards profiles + architecture docs under the
    configured roots.

    Idempotent in shape (same `(node, cfg)` → equal contract), but the
    standards and architecture corpora reflect on-disk state at build
    time. Per Plan 33 D1, this is called exactly once at
    PROVISIONING → PLANNING.
    """
    return EvaluationContract(
        task_id=node.id,
        local_ci=_build_local_ci(cfg),
        rubric=_build_rubric(cfg),
        standards=_build_standards(cfg),
        architecture=_build_architecture(cfg),
        behavior=_build_behavior(node),
    )


def audit_corpora_need_refresh(contract: EvaluationContract, cfg: Config) -> bool:
    """Return true when a persisted contract has empty audit corpora but
    current launch config can load them.

    Contracts are persisted at planning time so every worker phase sees a
    stable rubric. That stability is harmful after an operator fixes missing
    standards/architecture configuration: without this guard, old tasks keep
    failing audits from stale empty corpora even though daemon launch config is
    now valid.
    """
    standards_empty = not contract.standards.profiles or not any(
        profile.docs for profile in contract.standards.profiles
    )
    architecture_empty = not contract.architecture.corpus.docs
    if not standards_empty and not architecture_empty:
        return False
    if standards_empty:
        profiles = load_profiles(cfg)
        if any(profile.docs for profile in profiles):
            return True
    if architecture_empty:
        corpus = load_architecture(cfg)
        if corpus.docs:
            return True
    return False


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


def _build_standards(cfg: Config) -> StandardsStageRubric:
    profiles = load_profiles(cfg)
    source_text = render_profile_catalog(profiles)
    return StandardsStageRubric(
        one_line=(
            "Language/framework standards-profile alignment. Cite passages "
            "via `standards_referenced` (NOT `architecture_referenced`)."
        ),
        threshold="no drift from any cited profile section",
        grading_template=_STANDARDS_GRADING_TEMPLATE,
        profiles=profiles,
        source_text=source_text,
    )


def _build_architecture(cfg: Config) -> ArchitectureStageRubric:
    corpus = load_architecture(cfg)
    source_text = render_architecture_toc(corpus)
    return ArchitectureStageRubric(
        one_line=(
            "Project-architecture alignment with this repo's documented "
            "subsystem contracts. Cite via `architecture_referenced` "
            "(NOT `standards_referenced`)."
        ),
        threshold="no drift from any cited architecture section",
        grading_template=_ARCHITECTURE_GRADING_TEMPLATE,
        corpus=corpus,
        source_text=source_text,
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
