"""Schema + validator + extraction for the v2 structured planner output.

The planner agent is asked to return a JSON block describing the implementation
as a directed acyclic graph of *subtasks* — small, independently verifiable
slices of the spec. The orchestrator then drives a per-subtask doer/checker
loop instead of a monolithic do-the-whole-thing pass. See `docs/design-v2.md`
Phase 0 for the rationale.

This module owns the *shape* of the planner contract — a single source of
truth for both the prompt (which describes the schema to the agent) and the
worker (which validates + consumes the output). Implemented as Pydantic
models so validation errors are clean and field-typed; pydantic v2's native
JSON parsing is used in `parse_planner_output`.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from quikode.json_extract import first_balanced_object


class PlanValidationError(ValueError):
    """Raised when planner output doesn't conform to the v2 schema.

    Wraps Pydantic ValidationErrors with a flatter message so we can feed
    the error back to the planner for a re-prompt without overwhelming it.
    """


class RubricTarget(BaseModel):
    """Plan 33: one rubric category this subtask claims to advance.

    `category` must be a member of the contract's rubric category list
    (validated post-construction by `validate_rubric_coverage`).
    `predicted_score` is the planner's projection of where the audit
    grader will land this category after this subtask's diff merges; the
    doer treats it as a self-audit threshold (see Plan 33 §4 + §6.4).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: str = Field(min_length=1)
    predicted_score: int = Field(ge=1, le=10)


class StandardsRef(BaseModel):
    """Plan 33: one pinned standards-doc passage governing this subtask.

    `doc_path` is repo-relative and validated to exist at planning time
    by `validate_standards_paths`. `section` is free-form (heading,
    anchor, paragraph cite). The per-subtask checker reads the cited
    passage and verifies the diff aligns with it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    doc_path: str = Field(min_length=1)
    section: str = Field(min_length=1)


class Subtask(BaseModel):
    """One independently-verifiable slice of a node's implementation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, description="Unique within the plan, e.g. 'S-01-domain'.")
    title: str = Field(default="", description="One-line human description.")
    depends_on: tuple[str, ...] = Field(
        default=(),
        description="Other subtask ids this one needs done first.",
    )
    files_to_touch: tuple[str, ...] = Field(
        default=(),
        description=(
            "Plan 33: advisory metadata only. Surfaced in the doer prompt and "
            "in `qk show` rendering, but NO commit-time enforcement. The "
            "audit gauntlet is the truth — `files_to_touch` is just a "
            "scoping hint to anchor the doer's investigation."
        ),
    )
    boundary: str = Field(default="", description="What the doer must NOT touch.")
    acceptance: tuple[str, ...] = Field(
        ...,
        min_length=1,
        description="Concrete, independently verifiable acceptance criteria.",
    )
    notes: str = Field(default="")
    interfaces: tuple[str, ...] | list[str] = Field(
        default=(),
        description=(
            "Surfaces this subtask covers. For BDD subtasks driven by tanren's "
            "behavior-proof convention, populate with the behavior's interfaces "
            "(e.g. ['web', 'api', 'mcp']) so the doer knows which @web/@api/... "
            "tags to write. Empty for non-BDD subtasks."
        ),
    )
    kind: str = Field(
        default="spec",
        description=(
            "Subtask category. 'spec' for original planner output. "
            "'fixup-final' / 'fixup-ci' / 'fixup-review' for slices added by "
            "the fixup planner when the corresponding gate fails. Used by "
            "the worker to pick the right doer prompt and by `quikode show` "
            "to render fixup rounds distinctly."
        ),
    )
    rubric_targets: tuple[RubricTarget, ...] = Field(
        default=(),
        description=(
            "Plan 33: rubric categories this subtask claims to materially "
            "advance, with predicted scores. Empty allowed only on "
            "kind='fixup-*' subtasks where the fix is purely transport/CI; "
            "validate_rubric_coverage enforces every category in the "
            "contract appears in at least one subtask."
        ),
    )
    standards_referenced: tuple[StandardsRef, ...] = Field(
        default=(),
        description=(
            "Plan 33: standards-doc passages the planner has pinned for this "
            "subtask. Each `doc_path` must exist at planning time "
            "(validate_standards_paths)."
        ),
    )
    behavior_evidence_advanced: tuple[str, ...] = Field(
        default=(),
        description=(
            "Plan 33: canonical ids of `node.expected_evidence` items this "
            "subtask delivers a witness for. Each id appears in EXACTLY ONE "
            "subtask across the plan (partition, not cover) — enforced by "
            "validate_evidence_partition."
        ),
    )

    @field_validator(
        "acceptance",
        "depends_on",
        "files_to_touch",
        "interfaces",
        "rubric_targets",
        "standards_referenced",
        "behavior_evidence_advanced",
        mode="before",
    )
    @classmethod
    def _coerce_tuple(cls, v: Any) -> Any:
        # Accept lists (most common from JSON) and convert to tuple for hashability.
        if isinstance(v, list):
            return tuple(v)
        return v


class Plan(BaseModel):
    """Structured output from the v2 planner. Top-level shape."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str = Field(min_length=1)
    summary: str = Field(default="")
    subtasks: tuple[Subtask, ...] = Field(min_length=1)
    final_acceptance: tuple[str, ...] = Field(min_length=1)
    gauntlet_strategy: str = Field(
        default="",
        description=(
            "Plan 33: prose section explaining how this plan is positioned "
            "to pass the four-stage audit on cycle 1. The 200-2000 char "
            "length bound is enforced by `planner_validators."
            "validate_gauntlet_strategy` (separate from pydantic-level "
            "constraints) so unit-tests that construct minimal Plan "
            "objects don't have to ship 200 chars of prose. Real planner "
            "output IS validated."
        ),
    )

    @field_validator("subtasks", "final_acceptance", mode="before")
    @classmethod
    def _coerce_tuple(cls, v: Any) -> Any:
        if isinstance(v, list):
            return tuple(v)
        return v

    @model_validator(mode="after")
    def _check_unique_and_acyclic(self) -> Plan:
        ids: set[str] = set()
        for s in self.subtasks:
            if s.id in ids:
                raise ValueError(f"duplicate subtask id: {s.id!r}")
            ids.add(s.id)
        for s in self.subtasks:
            for d in s.depends_on:
                if d not in ids:
                    raise ValueError(f"subtask {s.id} depends_on unknown id {d!r}")
        # cycle detection via topo
        self._topo_order_raise_on_cycle()
        return self

    def _topo_order_raise_on_cycle(self) -> list[Subtask]:
        by_id = {s.id: s for s in self.subtasks}
        indeg = {s.id: 0 for s in self.subtasks}
        children: dict[str, list[str]] = defaultdict(list)
        for s in self.subtasks:
            for d in s.depends_on:
                indeg[s.id] += 1
                children[d].append(s.id)
        ready = [sid for sid, d in indeg.items() if d == 0]
        out: list[Subtask] = []
        while ready:
            ready.sort()  # deterministic
            cur = ready.pop(0)
            out.append(by_id[cur])
            for c in children[cur]:
                indeg[c] -= 1
                if indeg[c] == 0:
                    ready.append(c)
        if len(out) != len(self.subtasks):
            cyclic = [s.id for s in self.subtasks if s.id not in {x.id for x in out}]
            raise ValueError(f"subtask cycle detected, involving: {cyclic}")
        return out

    def topo_order(self) -> list[Subtask]:
        """Subtasks in dependency-respecting order. Validated at construction."""
        return self._topo_order_raise_on_cycle()


# ---------- JSON extraction (pre-validation) ----------

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of an agent's response.

    The planner is instructed to wrap its output in ```json...``` fences. We try
    that first; failing that, we look for the first balanced { ... } block.
    """
    if not text or not text.strip():
        raise PlanValidationError("planner returned empty output")

    m = _FENCED_JSON_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError as e:
            raise PlanValidationError(f"fenced block was not valid JSON: {e}") from e

    blob = first_balanced_object(text)
    if blob is None:
        raise PlanValidationError("no JSON object found in planner output")
    try:
        return json.loads(blob)
    except json.JSONDecodeError as e:
        raise PlanValidationError(f"unfenced JSON not parseable: {e}") from e


STABILIZATION_SUBTASK_ID = "Z-99-stabilize-spec-gate"


def _build_stabilization_subtask(
    *,
    prior_ids: list[str],
    spec_gate_command: str,
    rubric_categories: list[str] | None = None,
    rubric_min_score: int | None = None,
) -> dict[str, Any]:
    """Plan 24 + Plan 33 D4: deterministic final subtask appended to every
    spec plan with `rubric_targets` covering every rubric category at the
    minimum score.

    Sits after every planner-emitted subtask and depends on all of them.
    Its job is to run the spec gate and fix any breakage the prior
    subtasks left, AND to act as the holistic-pass guardian: by claiming
    every rubric category at `cfg.pre_pr_rubric_min_score`, the planner
    is structurally guaranteed to clear `validate_rubric_coverage` even
    when a per-subtask plan happens to omit a category. (Plan 33 D4.)
    """
    rubric_targets: list[dict[str, Any]] = []
    if rubric_categories and rubric_min_score is not None:
        for cat in rubric_categories:
            rubric_targets.append({"category": cat, "predicted_score": rubric_min_score})
    return {
        "id": STABILIZATION_SUBTASK_ID,
        "title": "Stabilize spec gate — ensure all gate checks pass cleanly",
        "depends_on": list(prior_ids),
        "files_to_touch": [],
        "boundary": (
            f"Make the spec gate (`{spec_gate_command}`) pass cleanly. May "
            f"edit any file required to fix gate failures. Plan 33: "
            f"`files_to_touch` is advisory — the audit gauntlet is the "
            f"truth; gate-keeping cross-file fixes are always legitimate."
        ),
        "acceptance": [
            f"Spec gate (`{spec_gate_command}`) passes with rc=0",
            "All committed changes from prior subtasks remain functional",
        ],
        "notes": (
            "System-injected stabilization subtask (plan 24, plan 33 D4). "
            "Every prior subtask has already landed and been committed. "
            "Your job is to run the gate, fix anything that fails, run it "
            "again, and repeat until rc=0. As the holistic-pass guardian, "
            "your `rubric_targets` cover every category at the minimum "
            "score — the audit must clear that bar by the time you commit."
        ),
        "kind": "spec",
        "rubric_targets": rubric_targets,
        "standards_referenced": [],
        "behavior_evidence_advanced": [],
    }


def _maybe_inject_stabilization(
    raw: dict[str, Any],
    *,
    spec_gate_command: str | None,
    rubric_categories: list[str] | None = None,
    rubric_min_score: int | None = None,
) -> dict[str, Any]:
    """Append the stabilization subtask to a planner-emitted dict if not
    already present and the caller supplied a gate command. Idempotent —
    safe to re-call across resumes / re-parses (`plan_text` is persisted
    once so this fires on the first parse and short-circuits on every
    subsequent re-parse).
    """
    if not spec_gate_command:
        return raw
    subtasks = raw.get("subtasks") or []
    if not isinstance(subtasks, list):
        return raw
    for s in subtasks:
        if isinstance(s, dict) and s.get("id") == STABILIZATION_SUBTASK_ID:
            return raw
    prior_ids = [s.get("id", "") for s in subtasks if isinstance(s, dict) and s.get("id")]
    new_raw = dict(raw)
    new_raw["subtasks"] = [
        *subtasks,
        _build_stabilization_subtask(
            prior_ids=prior_ids,
            spec_gate_command=spec_gate_command,
            rubric_categories=rubric_categories,
            rubric_min_score=rubric_min_score,
        ),
    ]
    return new_raw


def validate_and_build_plan(
    raw: dict[str, Any],
    *,
    expected_node_id: str | None = None,
    spec_gate_command: str | None = None,
    rubric_categories: list[str] | None = None,
    rubric_min_score: int | None = None,
) -> Plan:
    """Validate a parsed JSON object and return a Plan, or raise PlanValidationError.

    When `spec_gate_command` is supplied, a system-injected stabilization
    subtask (plan 24) is appended to the plan before validation. Pass
    None to skip injection (used for tests of pre-injection planner
    output). When `rubric_categories` + `rubric_min_score` are supplied,
    the injected Z-99 carries `rubric_targets` covering every category
    at the min score (plan 33 D4 holistic-pass guardian).
    """
    raw = _maybe_inject_stabilization(
        raw,
        spec_gate_command=spec_gate_command,
        rubric_categories=rubric_categories,
        rubric_min_score=rubric_min_score,
    )
    try:
        plan = Plan.model_validate(raw)
    except ValidationError as e:
        # Flatten Pydantic's structured error into a single line for prompt-feedback
        msgs = [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
        raise PlanValidationError("; ".join(msgs)) from e
    if expected_node_id and plan.node_id != expected_node_id:
        raise PlanValidationError(
            f"plan node_id={plan.node_id!r} doesn't match expected {expected_node_id!r}"
        )
    return plan


def parse_planner_output(
    text: str,
    *,
    expected_node_id: str | None = None,
    spec_gate_command: str | None = None,
    rubric_categories: list[str] | None = None,
    rubric_min_score: int | None = None,
) -> Plan:
    """Convenience: extract + validate in one step. Pass `spec_gate_command`
    to enable plan 24's stabilization-subtask injection. Pass
    `rubric_categories` + `rubric_min_score` so the Z-99 holistic guardian
    declares every rubric category at the min score (plan 33 D4)."""
    return validate_and_build_plan(
        extract_json(text),
        expected_node_id=expected_node_id,
        spec_gate_command=spec_gate_command,
        rubric_categories=rubric_categories,
        rubric_min_score=rubric_min_score,
    )


# ---------- v3 fixup decomposition ----------


class FixupPlan(BaseModel):
    """Output from the fixup planner — additive subtask slices only.

    Distinct from `Plan` because fixup is an *addition* to an existing plan,
    not a replacement: the original `final_acceptance` still governs the
    gate, and the original spec subtasks have already landed.

    For audit-driven fixup rounds (`kind="fixup-pre-pr-audit"`), the planner
    must emit `findings_addressed` listing every finding id from the audit
    bundle. Plan 33 retired `addresses_findings` per-subtask: fixup slices
    now declare which gaps they close via the stage-typed fields
    (`rubric_targets`, `standards_referenced`, `behavior_evidence_advanced`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(default="")
    subtasks: tuple[Subtask, ...] = Field(min_length=1)
    findings_addressed: tuple[str, ...] = Field(default=())

    @field_validator("subtasks", mode="before")
    @classmethod
    def _coerce_tuple(cls, v: Any) -> Any:
        if isinstance(v, list):
            return tuple(v)
        return v

    @field_validator("findings_addressed", mode="before")
    @classmethod
    def _coerce_findings_tuple(cls, v: Any) -> Any:
        if isinstance(v, list):
            return tuple(v)
        return v

    @model_validator(mode="after")
    def _check_unique_and_acyclic(self) -> FixupPlan:
        ids: set[str] = set()
        for s in self.subtasks:
            if s.id in ids:
                raise ValueError(f"duplicate fixup subtask id: {s.id!r}")
            ids.add(s.id)
        for s in self.subtasks:
            for d in s.depends_on:
                if d not in ids:
                    raise ValueError(f"fixup subtask {s.id} depends_on unknown id {d!r}")
        return self


def parse_fixup_planner_output(text: str) -> FixupPlan:
    """Extract + validate fixup planner JSON output."""
    raw = extract_json(text)
    try:
        return FixupPlan.model_validate(raw)
    except ValidationError as e:
        msgs = [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
        raise PlanValidationError("; ".join(msgs)) from e
