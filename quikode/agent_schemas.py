"""Plan 38 PR-A: per-role pydantic output schemas for the JSON-mode agent layer.

Each schema mirrors the JSON contract today's prompts already describe — see
`prompts/planner.md`, `prompts/subtask-checker.md`, `prompts/subtask-triage.md`,
`prompts/pre-pr-rubric.md`, `prompts/pre-pr-standards.md`,
`prompts/pre-pr-behavior.md`, `prompts/fixup-planner.md`,
`prompts/merge-planner.md`, and `prompts/progress.md`.

All models are `frozen=True, extra="forbid"`. Closed-string fields use
`Literal[...]` (typed enums) so a typo'd verdict / failure_layer / severity
fails schema validation rather than slipping through as free text.

These types are the contract the role/agent layer (PR-A's `JsonOutputAgent`
and `WritesFilesAgent`) hands back to the worker. PR-B replaces the existing
heuristic JSON-extract paths in workers, `pre_pr_audit`, `progress`, and
`subtask_schema` with consumption of these typed instances.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------- shared sub-models ----------


class RubricTargetSchema(BaseModel):
    """One rubric category a subtask claims to advance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: str = Field(min_length=1)
    predicted_score: int = Field(ge=1, le=10)


class StandardsRefSchema(BaseModel):
    """One pinned standards-doc passage for a subtask."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    doc_path: str = Field(min_length=1)
    section: str = Field(min_length=1)


class ArchitectureRefSchema(BaseModel):
    """One pinned project-architecture-doc passage for a subtask.

    Plan 35: architecture refs cite docs under `cfg.architecture_docs_dir`,
    distinct from standards-profile docs. The planner declares which
    architecture passages a subtask aligns with; `validate_architecture_refs`
    rejects standards-profile citations with a bucket-correction re-prompt.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    doc_path: str = Field(min_length=1)
    section: str = Field(min_length=1)


class SubtaskSpec(BaseModel):
    """One independently-verifiable slice of a node's implementation.

    Mirrors `quikode.subtask_schema.Subtask`. Kept as a separate JSON-layer
    type so the wire schema is stable while the runtime `Subtask` model
    can evolve (e.g. add tuple-coercion validators, runtime-only fields)
    without changing the JSON contract roles emit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    title: str = Field(default="")
    depends_on: list[str] = Field(default_factory=list)
    files_to_touch: list[str] = Field(default_factory=list)
    boundary: str = Field(default="")
    acceptance: list[str] = Field(min_length=1)
    notes: str = Field(default="")
    interfaces: list[str] = Field(default_factory=list)
    kind: str = Field(default="spec")
    rubric_targets: list[RubricTargetSchema] = Field(default_factory=list)
    standards_referenced: list[StandardsRefSchema] = Field(default_factory=list)
    architecture_referenced: list[ArchitectureRefSchema] = Field(default_factory=list)
    behavior_evidence_advanced: list[str] = Field(default_factory=list)


# ---------- planner / fixup-planner / merge-planner ----------


class PlannerOutput(BaseModel):
    """Top-level shape from the spec planner.

    Mirrors `prompts/planner.md` §7 and the existing `subtask_schema.Plan`.
    The orchestrator's Z-99 stabilization injection runs AFTER this
    layer; the planner schema does NOT pre-include Z-99.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str = Field(min_length=1)
    summary: str = Field(default="")
    gauntlet_strategy: str = Field(default="")
    subtasks: list[SubtaskSpec] = Field(min_length=1)
    final_acceptance: list[str] = Field(min_length=1)


class FixupPlannerOutput(BaseModel):
    """Output from the fixup planner — additive subtask slices.

    Mirrors `prompts/fixup-planner.md`. `findings_addressed` lists every
    audit-finding id covered by this round's subtasks (Plan 33 §5.5
    coverage union check).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(default="")
    subtasks: list[SubtaskSpec] = Field(min_length=1)
    findings_addressed: list[str] = Field(default_factory=list)


class MergePlannerOutput(BaseModel):
    """Output from the merge-planner.

    Mirrors `prompts/merge-planner.md`. Same shape as `PlannerOutput`
    plus an explicit `merge_context_summary` capturing the cross-parent
    conflict context the planner saw.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str = Field(min_length=1)
    summary: str = Field(default="")
    gauntlet_strategy: str = Field(default="")
    subtasks: list[SubtaskSpec] = Field(min_length=1)
    final_acceptance: list[str] = Field(min_length=1)
    merge_context_summary: str = Field(default="")


# ---------- doer (writes-files agent) ----------


class DoerEnvelope(BaseModel):
    """Lightweight bookkeeping envelope emitted by the doer / conflict-resolver.

    NOT a contract for grading. The diff is the evidence; a separate
    JSON-mode judging agent reads the diff + runs witnesses + emits
    judgment. This envelope lets the worker show "here's what the doer
    claims to have touched" in the TUI / briefing while keeping the
    actual evaluation diff-driven.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(default="")
    files_touched: list[str] = Field(default_factory=list)
    witness_commands_run: list[str] = Field(default_factory=list)
    notes: str = Field(default="")


# ---------- conflict resolver ----------


class ConflictResolverEnvelope(BaseModel):
    """Bookkeeping envelope emitted by the conflict-resolver writes-files role.

    Plan 38 PR-B.7: replaces the prior `"GIVE_UP:"` substring match in
    the resolver's free-text stdout with a structured `gave_up: bool`
    flag. The diff is still the evidence — the resolver edits files in
    the worktree and the worker validates via `git -C` primitives. This
    envelope only carries the bookkeeping the worker needs to branch on
    (give-up vs. continue) plus the human-readable summary surfaced to
    the briefing / TUI.

    `give_up_reason` is required to be non-empty when `gave_up=True`
    (cross-validated by `_validate_give_up_reason`); otherwise the
    BLOCK note would be uninformative to the human triaging it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(default="")
    files_touched: list[str] = Field(default_factory=list)
    gave_up: bool = Field(
        default=False,
        description=(
            "True iff the resolver could not produce a valid resolution and "
            "is surrendering the conflict for human triage. Worker BLOCKs "
            "on this without inspecting the diff."
        ),
    )
    give_up_reason: str = Field(
        default="",
        description="Why the resolver gave up; required (non-empty) when gave_up=True.",
    )
    notes: str = Field(default="")

    @model_validator(mode="after")
    def _validate_give_up_reason(self) -> ConflictResolverEnvelope:
        if self.gave_up and not self.give_up_reason.strip():
            raise ValueError("give_up_reason must be non-empty when gave_up=True")
        return self


# ---------- intent reviewer ----------


IntentReviewVerdictValue = Literal["no_drift", "minor_drift", "intent_conflict"]


class IntentReviewVerdict(BaseModel):
    """Top-level shape from the post-PR intent reviewer.

    Plan 38 PR-B.7: replaces the prose `_parse_intent_verdict` regex with
    a closed-enum verdict. `affected_areas` is a structured `list[str]`
    of paths/symbols (the prior free-text comma-separated string split
    on the worker side); `explanation` is the human-readable rationale
    surfaced to the briefing / replan prompt context. `next_actions`
    carries optional follow-up steps the reviewer recommends — empty
    when verdict is `no_drift`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: IntentReviewVerdictValue
    affected_areas: list[str] = Field(default_factory=list)
    explanation: str = Field(default="")
    next_actions: list[str] = Field(default_factory=list)


# ---------- subtask checker ----------


SubtaskCheckerVerdict = Literal["pass", "fail"]
PerRowVerdict = Literal["pass", "fail", "unknown"]


class SubtaskCheckerFinding(BaseModel):
    """One per-row verdict from the subtask checker.

    `category` is the dimension being graded (a rubric category, a
    standards doc§section, or a behavior evidence id). `verdict` is the
    pass/fail/unknown call; `rationale` is the one-line cite.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: str = Field(min_length=1)
    verdict: PerRowVerdict
    rationale: str = Field(default="")


class SubtaskCheckerOutput(BaseModel):
    """Top-level shape from the subtask checker.

    Mirrors `prompts/subtask-checker.md` §4. PR-B will rewrite the prompt
    to drop the SELF_AUDIT framing; the schema fields here are the
    enduring contract.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: SubtaskCheckerVerdict
    findings: list[SubtaskCheckerFinding] = Field(default_factory=list)
    overall_assessment: str = Field(default="")


# ---------- subtask triage ----------


SubtaskTriageFailureLayer = Literal[
    "local_ci",
    "rubric",
    "standards",
    "behavior",
    "parse_failure",
    "transport",
]


class SubtaskTriageOutput(BaseModel):
    """Top-level shape from the subtask triage agent.

    Mirrors `prompts/subtask-triage.md` §4. Plan 38 retires
    `self_audit_mismatch` and adds `parse_failure` (covers cases where
    the checker output failed schema validation under Tier-2
    enforcement).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    failure_layer: SubtaskTriageFailureLayer
    root_cause: str = Field(min_length=1)
    file_line_cites: list[str] = Field(default_factory=list)
    teaching_narrative: str = Field(default="")


# ---------- pre-PR rubric audit ----------


class RubricGap(BaseModel):
    """One gap_to_reach_ten under a rubric category."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    concrete_fix: str = Field(default="")
    files: list[str] = Field(default_factory=list)


class RubricCategoryScore(BaseModel):
    """Per-category rubric score with rationale + work-list."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    score: int = Field(ge=1, le=10)
    rationale: str = Field(default="")
    gaps_to_reach_ten: list[RubricGap] = Field(default_factory=list)


class PrePRRubricAuditOutput(BaseModel):
    """Top-level shape from the pre-PR rubric audit.

    Mirrors `prompts/pre-pr-rubric.md`. The worker reads `categories[]`
    and gates on `score >= cfg.pre_pr_rubric_min_score`; gaps are fed
    to the fixup planner verbatim.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    categories: list[RubricCategoryScore] = Field(default_factory=list)
    overall_assessment: str = Field(default="")


# ---------- pre-PR standards audit ----------


StandardsSeverity = Literal["low", "medium", "high", "critical"]


class StandardsFinding(BaseModel):
    """One standards-alignment finding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    file: str = Field(default="")
    line: int | None = Field(default=None)
    severity: StandardsSeverity
    standards_doc_ref: str = Field(min_length=1)
    description: str = Field(min_length=1)
    concrete_fix: str = Field(default="")


class PrePRStandardsAuditOutput(BaseModel):
    """Top-level shape from the pre-PR standards audit.

    Mirrors `prompts/pre-pr-standards.md`. Worker gates on `severity`
    >= medium.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: list[StandardsFinding] = Field(default_factory=list)
    overall_assessment: str = Field(default="")


# ---------- pre-PR behavior audit ----------


class BehaviorCompletenessGap(BaseModel):
    """One completeness gap on a verified behavior."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    concrete_fix: str = Field(default="")


class BehaviorVerification(BaseModel):
    """Per-behavior empirical verification result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    behavior_id: str = Field(min_length=1)
    verified: bool
    evidence_seen: str = Field(default="")
    gap_explanation: str = Field(default="")
    concrete_fix: str = Field(default="")
    completeness_gaps: list[BehaviorCompletenessGap] = Field(default_factory=list)


class PrePRBehaviorAuditOutput(BaseModel):
    """Top-level shape from the pre-PR behavior audit.

    Mirrors `prompts/pre-pr-behavior.md`. Worker gates on any
    `verified=False` entry.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    behaviors: list[BehaviorVerification] = Field(default_factory=list)
    overall_assessment: str = Field(default="")


# ---------- progress check ----------


ProgressVerdictValue = Literal["progressing", "flatline", "uncertain"]


class ProgressVerdict(BaseModel):
    """Top-level shape from the progress-check agent.

    Mirrors `prompts/progress.md`. Plan 38 normalizes `flatlined` →
    `flatline` (closed-enum tightening to match plan §3.1). PR-B will
    reconcile the worker's `subtask_flatline_block_count` consumer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: ProgressVerdictValue
    rationale: str = Field(default="")


__all__ = [
    "ArchitectureRefSchema",
    "BehaviorCompletenessGap",
    "BehaviorVerification",
    "ConflictResolverEnvelope",
    "DoerEnvelope",
    "FixupPlannerOutput",
    "IntentReviewVerdict",
    "IntentReviewVerdictValue",
    "MergePlannerOutput",
    "PerRowVerdict",
    "PlannerOutput",
    "PrePRBehaviorAuditOutput",
    "PrePRRubricAuditOutput",
    "PrePRStandardsAuditOutput",
    "ProgressVerdict",
    "ProgressVerdictValue",
    "RubricCategoryScore",
    "RubricGap",
    "RubricTargetSchema",
    "StandardsFinding",
    "StandardsRefSchema",
    "StandardsSeverity",
    "SubtaskCheckerFinding",
    "SubtaskCheckerOutput",
    "SubtaskCheckerVerdict",
    "SubtaskSpec",
    "SubtaskTriageFailureLayer",
    "SubtaskTriageOutput",
]
