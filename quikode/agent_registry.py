"""Plan 38 PR-A: role → schema → default-model registry.

Roles bind to MODELS, never to CLI names. The role declares its
pydantic schema, whether it writes files, the default model name, and
which `cfg.<role>_timeout_s` field controls its per-call timeout.

`make_agent(role_name, cfg)` walks `cfg.<role>_model` (falling back to
the role's `default_model`), looks up the model in
`quikode.model_registry.MODELS`, picks the right transport shim
(`CodexDirectJsonAgent` / `CodexLitellmJsonAgent` / `ClaudeJsonAgent`),
and wraps it in a `JsonOutputAgent` (non-writes-files roles) or
`WritesFilesAgent` (doer / conflict-resolver). The role layer never
sees a CLI name.

Operator surface: `cfg.planner_model = "GLM-5.1-zai"` resolves to a
`JsonOutputAgent` wrapping `CodexLitellmJsonAgent(profile="glm-zai")`
with `PlannerOutput` as the output schema. No `cfg.<role>_cli` knob
exists by design — the CLI is derived from the model.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from .agent_schemas import (
    ConflictResolverEnvelope,
    FixupPlannerOutput,
    IntentReviewVerdict,
    MergePlannerOutput,
    PlannerOutput,
    PrePRArchitectureAuditOutput,
    PrePRBehaviorAuditOutput,
    PrePRRubricAuditOutput,
    PrePRStandardsAuditOutput,
    ProgressVerdict,
    SubtaskCheckerOutput,
    SubtaskTriageOutput,
)
from .agents.json_claude import ClaudeJsonAgent
from .agents.json_codex_direct import CodexDirectJsonAgent
from .agents.json_codex_litellm import CodexLitellmJsonAgent
from .agents.json_fallback import QuotaFallbackJsonAgent
from .agents.json_protocol import (
    JsonAgentTransport,
    JsonOutputAgent,
    WritesFilesAgent,
)
from .config import Config
from .model_registry import MODELS, ModelSpec


@dataclass(frozen=True)
class RoleSpec:
    """One row in the role registry.

    `output_schema` is the pydantic model the role's agent emits, or
    `None` for writes-files roles that have no bookkeeping envelope at
    all (the doer post-plan-47: the diff is the only evidence and the
    transport runs in plain text mode).

    `writes_files=True` selects the `WritesFilesAgent` wrapper;
    `False` selects `JsonOutputAgent`.

    `default_model` is the model name (in `MODELS`) used when no
    `cfg.<role>_model` override is set. `timeout_s_field` is the
    `cfg` attribute carrying the per-call timeout.
    """

    name: str
    output_schema: type[BaseModel] | None
    writes_files: bool
    default_model: str
    timeout_s_field: str


ROLES: dict[str, RoleSpec] = {
    "planner": RoleSpec(
        name="planner",
        output_schema=PlannerOutput,
        writes_files=False,
        default_model="gpt-5.5",
        timeout_s_field="planner_timeout_s",
    ),
    "subtask_doer": RoleSpec(
        # Plan 47: doer no longer emits a bookkeeping envelope. The diff is
        # the sole evidence; the transport runs in plain-text mode (no
        # --output-schema / --json-schema, no pydantic re-prompt loop).
        name="subtask_doer",
        output_schema=None,
        writes_files=True,
        default_model="GLM-5.1-zai",
        timeout_s_field="subtask_doer_timeout_s",
    ),
    "subtask_checker": RoleSpec(
        name="subtask_checker",
        output_schema=SubtaskCheckerOutput,
        writes_files=False,
        default_model="gpt-5.5",
        timeout_s_field="subtask_checker_timeout_s",
    ),
    "subtask_triage": RoleSpec(
        name="subtask_triage",
        output_schema=SubtaskTriageOutput,
        writes_files=False,
        default_model="gpt-5.5",
        timeout_s_field="subtask_triage_timeout_s",
    ),
    "pre_pr_rubric": RoleSpec(
        name="pre_pr_rubric",
        output_schema=PrePRRubricAuditOutput,
        writes_files=False,
        default_model="gpt-5.5",
        timeout_s_field="pre_pr_audit_timeout_s",
    ),
    "pre_pr_standards": RoleSpec(
        name="pre_pr_standards",
        output_schema=PrePRStandardsAuditOutput,
        writes_files=False,
        default_model="gpt-5.5",
        timeout_s_field="pre_pr_audit_timeout_s",
    ),
    "pre_pr_architecture": RoleSpec(
        # Plan 35 PR-B: 5th gauntlet stage — grades the diff against the
        # project's documented subsystem contracts. Same shape + same
        # default model as `pre_pr_standards` (claude-class structural
        # reasoning); separate role so the operator can point it at a
        # different model without forcing standards onto the same one.
        name="pre_pr_architecture",
        output_schema=PrePRArchitectureAuditOutput,
        writes_files=False,
        default_model="gpt-5.5",
        timeout_s_field="pre_pr_audit_timeout_s",
    ),
    "pre_pr_behavior": RoleSpec(
        name="pre_pr_behavior",
        output_schema=PrePRBehaviorAuditOutput,
        writes_files=False,
        default_model="gpt-5.5",
        timeout_s_field="pre_pr_audit_timeout_s",
    ),
    "fixup_planner": RoleSpec(
        name="fixup_planner",
        output_schema=FixupPlannerOutput,
        writes_files=False,
        default_model="gpt-5.5",
        timeout_s_field="fixup_planner_timeout_s",
    ),
    "merge_planner": RoleSpec(
        name="merge_planner",
        output_schema=MergePlannerOutput,
        writes_files=False,
        default_model="gpt-5.5",
        timeout_s_field="merge_planner_timeout_s",
    ),
    "conflict_resolver": RoleSpec(
        name="conflict_resolver",
        output_schema=ConflictResolverEnvelope,
        writes_files=True,
        default_model="GLM-5.1-zai",
        timeout_s_field="conflict_resolver_timeout_s",
    ),
    "intent_reviewer": RoleSpec(
        name="intent_reviewer",
        output_schema=IntentReviewVerdict,
        writes_files=False,
        default_model="gpt-5.5",
        timeout_s_field="intent_reviewer_timeout_s",
    ),
    "replan_planner": RoleSpec(
        # Plan 38 PR-B.7: separate role from `planner` so the operator can
        # point post-PR replans at a different model than the spec planner.
        # Same `PlannerOutput` schema and same wire→runtime translation;
        # the cfg knob is `cfg.replan_planner_model` with the same default
        # as `planner`.
        name="replan_planner",
        output_schema=PlannerOutput,
        writes_files=False,
        default_model="gpt-5.5",
        timeout_s_field="replan_planner_timeout_s",
    ),
    "progress": RoleSpec(
        name="progress",
        output_schema=ProgressVerdict,
        writes_files=False,
        default_model="gpt-5.5",
        timeout_s_field="progress_timeout_s",
    ),
}


def _build_base_transport(
    spec: ModelSpec, *, quota_max_total_wait_s: int | None = None
) -> JsonAgentTransport:
    """Construct the right transport shim for a model spec."""
    if spec.transport == "codex_direct":
        if spec.codex_profile is None:  # pragma: no cover — registry validates this
            raise ValueError(f"model {spec.name!r}: codex_direct missing codex_profile")
        return CodexDirectJsonAgent(profile=spec.codex_profile)
    if spec.transport == "codex_litellm":
        if spec.codex_profile is None:  # pragma: no cover
            raise ValueError(f"model {spec.name!r}: codex_litellm missing codex_profile")
        return CodexLitellmJsonAgent(
            profile=spec.codex_profile,
            quota_max_total_wait_s=quota_max_total_wait_s,
        )
    if spec.transport == "claude":
        if spec.claude_model_id is None:  # pragma: no cover
            raise ValueError(f"model {spec.name!r}: claude missing claude_model_id")
        return ClaudeJsonAgent(model_id=spec.claude_model_id)
    raise ValueError(f"model {spec.name!r}: unknown transport {spec.transport!r}")


def _build_transport(spec: ModelSpec) -> JsonAgentTransport:
    """Construct transport, including configured quota fallbacks."""
    if not spec.quota_fallbacks:
        return _build_base_transport(spec)
    primary = _build_base_transport(spec, quota_max_total_wait_s=0)
    fallbacks = tuple(_build_base_transport(MODELS[name]) for name in spec.quota_fallbacks)
    return QuotaFallbackJsonAgent(primary=primary, fallbacks=fallbacks)


def make_agent(role: str, cfg: Config) -> JsonOutputAgent | WritesFilesAgent:
    """Construct the agent for a role, given current cfg.

    Picks `cfg.<role>_model` (the operator override), falling back to
    `RoleSpec.default_model`. Looks up the model in `MODELS`. Picks the
    right transport shim. Wraps in `WritesFilesAgent` (writes-files
    roles) or `JsonOutputAgent` (everything else).

    Raises `KeyError` when `role` isn't registered, or when the
    resolved model name isn't in `MODELS` (e.g. operator typo in the
    cfg override).
    """
    role_spec = ROLES.get(role)
    if role_spec is None:
        known = ", ".join(sorted(ROLES.keys()))
        raise KeyError(f"unknown role {role!r}; known roles: {known}")
    model_attr = f"{role}_model"
    model_name = getattr(cfg, model_attr, None) or role_spec.default_model
    if model_name not in MODELS:
        known = ", ".join(sorted(MODELS.keys()))
        raise KeyError(
            f"role {role!r}: cfg.{model_attr}={model_name!r} not in model registry; known models: {known}"
        )
    spec = MODELS[model_name]
    transport = _build_transport(spec)
    if role_spec.writes_files:
        return WritesFilesAgent(transport=transport, envelope_schema=role_spec.output_schema)
    if role_spec.output_schema is None:
        raise ValueError(f"role {role!r}: non-writes-files roles must declare output_schema; got None")
    return JsonOutputAgent(transport=transport, output_schema=role_spec.output_schema)


__all__ = [
    "ROLES",
    "RoleSpec",
    "make_agent",
]
