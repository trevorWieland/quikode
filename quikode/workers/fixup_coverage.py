"""Plan 33 PR-B + Plan 38 PR-B.4: stage-typed coverage check + driver loop for the fixup planner.

Used by `quikode.workers.pre_pr.PrePrWorkerMixin._run_fixup_round` to
verify the fixup planner's `FixupPlan` covers every audit finding id.
The retired `addresses_findings` per-subtask field (Plan 33 D2) is
replaced by a union over `rubric_targets`, `standards_referenced`,
`behavior_evidence_advanced`, plus the plan-level `findings_addressed`
array, plus a `notes`-text fallback for finding ids the planner
groups into one subtask.

Lives in its own module to keep `pre_pr.py` under the 600-line budget.

Plan 33 calibration (after the tanren R-0002 BLOCK): also owns the
fixup-side validator orchestration. The fixup driver swaps
`validate_rubric_coverage` for `validate_finding_coverage` because a
fixup plan addresses specific audit gaps (per-finding), not whole
rubric categories — so empty `rubric_targets` is legitimate when a
fixup is purely transport/CI/standards. Driver-side validator routing
stays out of `pre_pr.py` to keep that file under budget.

Plan 38 PR-B.4 retired the heuristic JSON-extraction path. The fixup
planner now runs through the JsonAgent layer and returns a validated
wire-schema `FixupPlannerOutput`; `validate_fixup_plan` consumes that
pydantic instance directly. The `_wire_to_runtime_fixup_plan` helper
translates the wire schema's `list[...]` shape to the runtime
`FixupPlan` (tuple-coerced fields).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from quikode.agent_registry import make_agent
from quikode.agent_schemas import FixupPlannerOutput, SubtaskSpec
from quikode.planner_validators import (
    PlannerValidationError,
    validate_architecture_refs,
    validate_evidence_partition,
    validate_finding_coverage,
    validate_standards_refs,
)
from quikode.subtask_schema import FixupPlan, PlanValidationError

if TYPE_CHECKING:
    from quikode.dag import Node
    from quikode.evaluation_contract import EvaluationContract


def collect_stage_coverage(plan: FixupPlan) -> tuple[set[str], set[str], set[str], str]:
    """Build the (rubric_cats, standards_paths, behavior_evids, notes_text)
    coverage union over the plan's subtasks. Plan 33 PR-B: the audit-
    completeness check unions across these fields instead of the retired
    per-subtask `addresses_findings`."""
    rubric_cats: set[str] = set()
    standards_paths: set[str] = set()
    behavior_evids: set[str] = set()
    notes_blob: list[str] = []
    for s in plan.subtasks:
        for tgt in s.rubric_targets:
            rubric_cats.add(tgt.category)
        for ref in s.standards_referenced:
            standards_paths.add(ref.doc_path)
        for evid in s.behavior_evidence_advanced:
            behavior_evids.add(evid)
        if s.notes:
            notes_blob.append(s.notes)
    return rubric_cats, standards_paths, behavior_evids, "\n".join(notes_blob)


def missing_finding_coverage(plan: FixupPlan, expected_finding_ids: list[str]) -> set[str]:
    """A finding id is covered when it appears in the plan-level
    `findings_addressed` OR when it can be matched to one of the plan's
    subtasks via stage-typed coverage.

    The audit emits ids namespaced by stage (`rubric:<gap-id>`,
    `standards:<finding-id>`, `behavior:<id>`). For each id we attempt:

    * Stage prefix `rubric:` → covered when ANY subtask declares any
      `rubric_targets` (the per-subtask checker verifies the diff
      substantively advances the category later) OR cites the finding
      id in its `notes`.
    * Stage prefix `standards:` → covered when ANY subtask declares any
      `standards_referenced` OR cites the finding id in `notes`.
    * Stage prefix `behavior:` → covered when the corresponding
      evidence id appears in some subtask's
      `behavior_evidence_advanced` OR the finding id is cited in `notes`.

    Anything in `plan.findings_addressed` is covered regardless.
    """
    expected = set(expected_finding_ids)
    covered: set[str] = set(plan.findings_addressed)
    rubric_cats, standards_paths, behavior_evids, notes_text = collect_stage_coverage(plan)
    for fid in expected - covered:
        if fid in notes_text:
            covered.add(fid)
            continue
        if fid.startswith("behavior:"):
            evid = fid.split(":", 1)[1]
            if evid in behavior_evids:
                covered.add(fid)
                continue
        if fid.startswith("rubric:") and rubric_cats:
            covered.add(fid)
            continue
        if fid.startswith("standards:") and standards_paths:
            covered.add(fid)
    return expected - covered


def _wire_subtask_to_runtime_dict(spec: SubtaskSpec) -> dict[str, Any]:
    """Translate one wire `SubtaskSpec` to the runtime `Subtask` ingest dict.

    The wire schema uses plain `list[...]` for collection fields; the
    runtime `Subtask` carries `tuple[...]` (coerced via `_coerce_tuple`).
    Going through the dict shape lets the runtime validators apply
    without re-implementing them here. Mirrors the spec-planner driver's
    helper of the same name; kept duplicated rather than imported so
    a future divergence (e.g. fixup-only fields) doesn't cause a
    circular import between the two modules.
    """
    return {
        "id": spec.id,
        "title": spec.title,
        "depends_on": list(spec.depends_on),
        "files_to_touch": list(spec.files_to_touch),
        "boundary": spec.boundary,
        "acceptance": list(spec.acceptance),
        "notes": spec.notes,
        "interfaces": list(spec.interfaces),
        "kind": spec.kind,
        "rubric_targets": [
            {"category": t.category, "predicted_score": t.predicted_score} for t in spec.rubric_targets
        ],
        "standards_referenced": [
            {"doc_path": r.doc_path, "section": r.section} for r in spec.standards_referenced
        ],
        "architecture_referenced": [
            {"doc_path": r.doc_path, "section": r.section} for r in spec.architecture_referenced
        ],
        "behavior_evidence_advanced": list(spec.behavior_evidence_advanced),
    }


def _wire_to_runtime_fixup_plan(fixup_output: FixupPlannerOutput) -> FixupPlan:
    """Translate a wire `FixupPlannerOutput` into a runtime `FixupPlan`.

    Same pattern as the spec planner's `_wire_to_runtime_plan` (subtask
    schema's collection fields are plain lists on the wire and tuples
    at runtime); the fixup plan has no Z-99 injection (Z-99 is
    spec-only) so this is a straight translation.
    """
    raw_plan = {
        "summary": fixup_output.summary,
        "subtasks": [_wire_subtask_to_runtime_dict(s) for s in fixup_output.subtasks],
        "findings_addressed": list(fixup_output.findings_addressed),
    }
    try:
        return FixupPlan.model_validate(raw_plan)
    except ValidationError as e:
        msgs = [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
        raise PlanValidationError("; ".join(msgs)) from e


def validate_fixup_plan(
    fixup_output: FixupPlannerOutput,
    *,
    contract: EvaluationContract,
    node: Node,
    audit_findings: list[str] | None,
) -> tuple[FixupPlan | None, str | None]:
    """Translate the wire `FixupPlannerOutput` to runtime, then run the
    fixup-side validator suite.

    Returns `(plan, None)` on success, or `(None, error_message)` if
    the runtime translation failed (uniqueness / depends_on / cycle) or
    any validator raised. The error_message is the structured feedback
    the caller should feed into the next planner re-prompt.

    Validators run, in order:

    * `validate_finding_coverage(plan, audit_findings)` — fixup-only
      partition check (replaces `validate_rubric_coverage` per Plan 33
      calibration; a fixup addresses findings, not whole categories).
    * `validate_evidence_partition(plan, node)` — same as the spec
      side; the audit's `behavior:<id>` findings need exactly-once
      coverage so this stays universal.
    * `validate_standards_refs(plan, contract)` — Plan 35: cited
      standards refs must live under a loaded profile.
    * `validate_architecture_refs(plan, contract)` — Plan 35: cited
      architecture refs must live under `cfg.architecture_docs_dir`.

    `audit_findings` may be None or empty — both short-circuit the
    finding-coverage validator (common for `fixup-final` / `fixup-ci`
    / `fixup-review` where the trigger context replaces the typed
    finding bundle).
    """
    try:
        plan = _wire_to_runtime_fixup_plan(fixup_output)
    except PlanValidationError as e:
        return None, (
            "Your previous fixup plan was structurally valid JSON but failed "
            "the runtime fixup-plan validators:\n\n"
            f"```\n{e}\n```\n\n"
            "Re-emit the COMPLETE fixup plan correcting the issue (e.g. "
            "duplicate subtask ids or `depends_on` referencing an unknown id)."
        )
    try:
        validate_finding_coverage(plan, audit_findings or [])
        validate_evidence_partition(plan, node)
        validate_standards_refs(plan, contract)
        validate_architecture_refs(plan, contract)
    except PlannerValidationError as ve:
        return None, (
            f"Your previous fixup plan failed validator `{ve.which}`. "
            f"Re-emit the COMPLETE fixup plan correcting the following:\n\n"
            f"{ve.message}"
        )
    return plan, None


def split_subtask_rows_for_planner(rows: list[dict], done_state_value: str) -> tuple[list[dict], list[dict]]:
    """Bucket existing subtask rows for the fixup-planner prompt.

    Returns `(done_spec_subtasks, prior_fixup_subtasks)` — each a list
    of view-dicts the prompt template iterates over. Lives here so
    `pre_pr.py` stays under the architecture line-budget.
    """
    done_subtasks: list[dict] = []
    prior_fixup_subtasks: list[dict] = []
    for r in rows:
        kind_val = (r.get("kind") or "spec") if isinstance(r, dict) else "spec"
        row_view = {
            "subtask_id": r["subtask_id"],
            "title": r.get("title") or "",
            "kind": kind_val,
            "state": r["state"],
        }
        if kind_val == "spec":
            if r["state"] == done_state_value:
                done_subtasks.append(row_view)
        else:
            prior_fixup_subtasks.append(row_view)
    return done_subtasks, prior_fixup_subtasks


def build_coverage_gap_addendum(missing: set[str], triage_root_cause: str | None) -> str:
    """Render the `## Coverage gap` re-prompt prefix used by the fixup
    driver after the completeness wrapper flags missing finding ids.

    Lives in this module so `pre_pr.py` stays under the architecture
    line-budget; the rendered string is concatenated with the original
    triage_root_cause and capped at 16k chars by the caller."""
    bullets = "\n".join(f"- `{fid}`" for fid in sorted(missing))
    addendum = (
        "## Coverage gap from your previous attempt\n\n"
        "Your previous plan missed the following finding ids. "
        "Include each one in `findings_addressed` AND in at least "
        "one subtask's stage-typed coverage (`rubric_targets`, "
        "`standards_referenced`, or `behavior_evidence_advanced` "
        f"matching the finding's namespace):\n\n{bullets}\n\n"
        "Re-emit the COMPLETE plan (do not emit only the deltas)."
    )
    return (addendum + "\n\n---\n\n" + (triage_root_cause or ""))[:16000]


def run_fixup_planner_loop(
    worker: Any,
    *,
    kind: str,
    round_no: int,
    base_prompt: str,
    audit_findings: list[str] | None,
    contract: EvaluationContract,
    log: Any,
) -> FixupPlan | None:
    """Plan 38 PR-B.4: agent-invocation + retry loop for the fixup planner.

    Extracted from `pre_pr.py` to keep that module under the 600-line
    architecture budget. Owns:

    * Building the fixup-planner agent through `make_agent`.
    * The transient-retry budget (`cfg.fixup_planner_retries_on_transient`).
    * One re-prompt round on schema-validation failure (parse_errors)
      and one on runtime-validator failure — separate from each other,
      both using the same `validator_retries_left=1` budget per round.
    * Persisting the agent-call record + the per-attempt artifact.

    Returns the runtime `FixupPlan` on success, or None on agent
    rc != 0 / exhausted-retry / persistent schema or validator failure.
    """
    agent = make_agent("fixup_planner", worker.cfg)
    cfg = worker.cfg
    retries_left = cfg.fixup_planner_retries_on_transient
    validator_retries_left = 1
    attempt_no = 0
    cur_prompt = base_prompt
    while True:
        attempt_no += 1
        call_id = worker.store.record_agent_call_started(
            worker.node.id,
            phase=f"fixup_planner:{kind}",
            cli="json_agent",
            model=cfg.fixup_planner_model,
        )
        result = agent.invoke(
            cur_prompt,
            handle=worker._h,
            log_path=worker.log_path,
            timeout=cfg.fixup_planner_timeout_s,
        )
        worker.store.record_agent_call_finished(
            call_id,
            rc=result.rc,
            duration_s=result.duration_s or 0,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            cost_usd=result.cost_usd,
        )
        artifact_text = result.raw_text or (
            result.structured.model_dump_json() if result.structured is not None else ""
        )
        if artifact_text:
            worker.store.add_artifact(
                worker.node.id,
                f"fixup_planner_output:{kind}:{round_no}:attempt{attempt_no}",
                artifact_text,
            )
        if result.transient and retries_left > 0:
            retries_left -= 1
            log.warning(
                "fixup planner transient (rc=%d) for %s round %d, retrying (%d left)",
                result.rc,
                kind,
                round_no,
                retries_left,
            )
            continue
        if result.rc != 0:
            log.warning("fixup planner exited rc=%d (kind=%s round=%d)", result.rc, kind, round_no)
            return None
        if result.parse_errors or result.structured is None:
            if validator_retries_left <= 0:
                log.warning(
                    "fixup planner output failed schema validation after retry (kind=%s round=%d): %s",
                    kind,
                    round_no,
                    "; ".join(result.parse_errors)[:300] if result.parse_errors else "no output",
                )
                return None
            validator_retries_left -= 1
            log.warning(
                "fixup planner output failed schema validation (kind=%s round=%d); re-prompting once",
                kind,
                round_no,
            )
            cur_prompt = build_schema_failure_feedback(result.parse_errors) + "\n\n---\n\n" + base_prompt
            continue
        if not isinstance(result.structured, FixupPlannerOutput):
            log.warning(
                "fixup planner returned unexpected schema %s (kind=%s round=%d)",
                type(result.structured).__name__,
                kind,
                round_no,
            )
            return None
        plan, feedback = validate_fixup_plan(
            result.structured,
            contract=contract,
            node=worker.node,
            audit_findings=audit_findings,
        )
        if plan is not None:
            return plan
        if validator_retries_left <= 0:
            log.warning(
                "fixup planner output failed validators after retry (kind=%s round=%d): %s",
                kind,
                round_no,
                (feedback or "")[:300],
            )
            return None
        validator_retries_left -= 1
        log.warning(
            "fixup planner output failed validators (kind=%s round=%d); re-prompting once",
            kind,
            round_no,
        )
        cur_prompt = (feedback or "") + "\n\n---\n\n" + base_prompt


def build_schema_failure_feedback(parse_errors: tuple[str, ...]) -> str:
    """Render the re-prompt feedback when the wire schema's pydantic
    validation fails. Lives here so `pre_pr.py` stays under the 600-
    line architecture budget.

    The body cites the typed-tuple parse errors (capped at 1000 chars)
    and reminds the planner of the two most-misemitted shapes
    (`standards_referenced`, `architecture_referenced` — both must be
    `{doc_path, section}` objects per plan 35)."""
    body = "; ".join(parse_errors)[:1000] if parse_errors else "(agent returned no structured output)"
    return (
        "Your previous fixup plan failed JSON-schema validation:\n\n"
        f"```\n{body}\n```\n\n"
        "Re-emit a single fenced ```json ... ``` block that conforms "
        "strictly to the schema. Note specifically: "
        "`standards_referenced` items MUST be objects with "
        '`{"doc_path": "...", "section": "..."}`, not strings; '
        '`architecture_referenced` items MUST also be `{"doc_path": '
        '"...", "section": "..."}` objects (plan 35).'
    )


__all__ = [
    "build_coverage_gap_addendum",
    "build_schema_failure_feedback",
    "collect_stage_coverage",
    "missing_finding_coverage",
    "run_fixup_planner_loop",
    "split_subtask_rows_for_planner",
    "validate_fixup_plan",
]
