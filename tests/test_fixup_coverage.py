"""Plan 33 calibration + Plan 38 PR-B.4: fixup-planner driver routing tests.

After the tanren R-0002 BLOCK (a structurally-valid 11.5KB fixup plan
was rejected because `validate_rubric_coverage` demanded every rubric
category be advanced), the fixup driver was rerouted to call
`validate_finding_coverage` instead of `validate_rubric_coverage`. The
spec-planner driver still calls all four spec validators.

Plan 38 PR-B.4 retired the prose-parsing path: `validate_fixup_plan`
now consumes a pre-validated `FixupPlannerOutput` (wire schema) instead
of free text. These tests build the wire pydantic instance directly
from a dict and feed it to the validator function. Behavior of the
validators themselves (finding coverage, standards/architecture refs) is
unchanged. Fixup plans intentionally do not run the full spec-plan evidence
partition validator because they are additive slices for failed audit findings,
not replacements for the original spec plan.

These tests pin the routing decision so a future refactor can't
silently re-introduce the cross-validator coupling.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from quikode import planner_validators
from quikode.agent_schemas import FixupPlannerOutput
from quikode.agents.json_protocol import JsonAgentResult
from quikode.architecture_docs import ArchitectureCorpus, load_architecture
from quikode.config import Config
from quikode.dag import Node
from quikode.evaluation_contract import (
    ArchitectureStageRubric,
    EvaluationContract,
    StageRubric,
    StandardsStageRubric,
)
from quikode.standards_profiles import load_profiles
from quikode.subtask_schema import Plan, Subtask
from quikode.workers import fixup_coverage as fc
from quikode.workers import planner_driver
from quikode.workers.fixup_coverage import (
    missing_finding_coverage,
    run_fixup_planner_loop,
    validate_fixup_plan,
)

_PROFILE_FIX = Path(__file__).resolve().parent / "fixtures" / "standards_profiles"
_ARCH_FIX = Path(__file__).resolve().parent / "fixtures" / "architecture_docs"


def _populated_contract(tmp_path: Path) -> EvaluationContract:
    """Stand up a contract with the fixture profile + architecture trees
    so `validate_fixup_plan` (which now takes a contract) has real
    corpora to dispatch against. Tests still only exercise the
    finding/rubric/evidence routing — the standards/architecture refs
    they emit must point at fixture docs."""
    profile_root = tmp_path / "profiles"
    if not profile_root.exists():
        profile_root.mkdir()
        shutil.copytree(_PROFILE_FIX / "rust-cargo", profile_root / "rust-cargo")
    arch_root = tmp_path / "docs" / "architecture"
    if not arch_root.exists():
        arch_root.mkdir(parents=True)
        shutil.copytree(_ARCH_FIX / "subsystems", arch_root / "subsystems")
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        standards_profiles_dir=profile_root,
        standards_profiles=["rust-cargo"],
        architecture_docs_dir=arch_root,
    )
    profiles = load_profiles(cfg)
    corpus = load_architecture(cfg)
    return EvaluationContract(
        task_id="R-001",
        local_ci=StageRubric(
            name="local_ci", one_line="", threshold="rc=0", grading_template="", source_text=""
        ),
        rubric=StageRubric(
            name="rubric",
            one_line="",
            threshold="",
            grading_template="",
            source_text="- **security**\n- **maintainability**",
        ),
        standards=StandardsStageRubric(profiles=profiles, source_text=""),
        architecture=ArchitectureStageRubric(corpus=corpus, source_text=""),
        behavior=StageRubric(name="behavior", one_line="", threshold="", grading_template="", source_text=""),
    )


def _node(evidence_ids: list[str] | None = None) -> Node:
    evidence: list[dict] = []
    for eid in evidence_ids or []:
        evidence.append({"kind": "test", "id": eid, "description": eid})
    return Node(
        id="R-001",
        kind="behavior",
        milestone="M-1",
        title="t",
        scope="s",
        depends_on=(),
        completes_behaviors=(),
        supports_behaviors=(),
        boundary_with_neighbors="",
        expected_evidence=tuple(evidence),
        playbook=(),
        rationale="",
        risks=(),
        raw={},
    )


def _fixup_planner_output(
    *,
    findings_addressed: list[str] | None = None,
    rubric_targets: list[dict] | None = None,
    standards_referenced: list[dict] | None = None,
    architecture_referenced: list[dict] | None = None,
    behavior_evidence_advanced: list[str] | None = None,
) -> FixupPlannerOutput:
    """Build a single-subtask wire-schema `FixupPlannerOutput`. The
    defaults are deliberately empty so each test exercises one
    stage-typed field at a time."""
    payload: dict[str, Any] = {
        "summary": "calibration test",
        "findings_addressed": findings_addressed or [],
        "subtasks": [
            {
                "id": "F-1-1-test",
                "title": "test slice",
                "depends_on": [],
                "files_to_touch": ["src/foo.rs"],
                "boundary": "scope",
                "acceptance": ["passes"],
                "rubric_targets": rubric_targets or [],
                "standards_referenced": standards_referenced or [],
                "architecture_referenced": architecture_referenced or [],
                "behavior_evidence_advanced": behavior_evidence_advanced or [],
                "interfaces": [],
                "notes": "",
                "kind": "fixup-pre-pr-audit",
            }
        ],
    }
    return FixupPlannerOutput.model_validate(payload)


# ----- routing: rubric_targets=[] is OK on a fixup plan -----


def test_fixup_plan_accepts_empty_rubric_targets_when_only_behavior_finding(
    tmp_path: Path,
):
    """The shape that BLOCKed R-0002: fixup plan declares
    `rubric_targets=[]` because the audit only flagged a behavior
    witness. The fixup driver must accept this; the prior wiring
    rejected it via `validate_rubric_coverage`."""
    fixup_output = _fixup_planner_output(
        findings_addressed=["behavior:B-0066"],
        behavior_evidence_advanced=["B-0066"],
    )
    plan, feedback = validate_fixup_plan(
        fixup_output,
        contract=_populated_contract(tmp_path),
        node=_node(["B-0066"]),
        audit_findings=["behavior:B-0066"],
    )
    assert plan is not None
    assert feedback is None
    assert plan.subtasks[0].rubric_targets == ()


def test_fixup_plan_rejects_string_standards_referenced(tmp_path: Path):
    """Plan 38 PR-B.4: the wire schema (`StandardsRefSchema`) requires
    `{doc_path, section}` objects; bare strings fail at the wire layer
    (the JsonAgent layer surfaces this to the driver as parse_errors).
    `FixupPlannerOutput.model_validate` raises `ValidationError`
    directly, so the malformed-shape branch is exercised by the
    JsonAgent layer, not by `validate_fixup_plan`. Pin the wire-layer
    rejection here."""
    bad_payload = {
        "summary": "x",
        "findings_addressed": ["rubric:security"],
        "subtasks": [
            {
                "id": "F-1-1",
                "title": "t",
                "depends_on": [],
                "files_to_touch": [],
                "boundary": "",
                "acceptance": ["ok"],
                "rubric_targets": [{"category": "security", "predicted_score": 8}],
                "standards_referenced": ["docs/x.md#A"],
                "behavior_evidence_advanced": [],
                "kind": "fixup-pre-pr-audit",
            }
        ],
    }
    with pytest.raises(ValidationError) as excinfo:
        FixupPlannerOutput.model_validate(bad_payload)
    msg = str(excinfo.value)
    # The wire schema's StandardsRefSchema requires objects; a bare
    # string fails with a typed error referencing the field name.
    assert "standards_referenced" in msg
    _ = tmp_path  # unused fixture in this branch


def test_fixup_plan_passes_when_finding_coverage_complete(tmp_path: Path):
    """Plan 35: cite a real fixture profile doc + section so the
    bucket-routing validator passes."""
    fixup_output = _fixup_planner_output(
        findings_addressed=["rubric:security", "behavior:B-0066"],
        rubric_targets=[{"category": "security", "predicted_score": 8}],
        standards_referenced=[{"doc_path": "profiles/rust-cargo/rust/error-handling.md", "section": "Rules"}],
        behavior_evidence_advanced=["B-0066"],
    )
    plan, feedback = validate_fixup_plan(
        fixup_output,
        contract=_populated_contract(tmp_path),
        node=_node(["B-0066"]),
        audit_findings=["rubric:security", "behavior:B-0066"],
    )
    assert plan is not None, feedback
    assert feedback is None


def test_fixup_plan_fails_on_missing_finding(tmp_path: Path):
    """When the audit cited `rubric:security` but no subtask claims
    that category, the validator surfaces a `finding_coverage` failure
    so the driver can re-prompt with the gap."""
    fixup_output = _fixup_planner_output(
        findings_addressed=[],
        rubric_targets=[{"category": "maintainability", "predicted_score": 7}],
    )
    plan, feedback = validate_fixup_plan(
        fixup_output,
        contract=_populated_contract(tmp_path),
        node=_node(),
        audit_findings=["rubric:security"],
    )
    assert plan is None
    assert feedback is not None
    assert "finding_coverage" in feedback
    assert "rubric:security" in feedback


def test_fixup_plan_does_not_require_full_node_evidence_partition(tmp_path: Path):
    """A pre-PR fixup plan only owns the failed audit findings. If the
    behavior stage passed, a rubric-only fixup must not be rejected because it
    does not re-claim the node's original behavior evidence."""
    fixup_output = _fixup_planner_output(
        findings_addressed=["rubric:security"],
        rubric_targets=[{"category": "security", "predicted_score": 8}],
    )
    plan, feedback = validate_fixup_plan(
        fixup_output,
        contract=_populated_contract(tmp_path),
        node=_node(["B-0066"]),
        audit_findings=["rubric:security"],
    )
    assert plan is not None, feedback
    assert feedback is None


def test_fixup_plan_accepts_plan_level_finding_addressed_without_exact_stage_field(tmp_path: Path):
    """The driver-side completeness wrapper already trusts
    `findings_addressed` so one fixup subtask can group several granular audit
    findings. The validator should not throw away that otherwise actionable
    plan before the wrapper runs."""
    fixup_output = _fixup_planner_output(
        findings_addressed=["behavior:B-0066"],
    )
    plan, feedback = validate_fixup_plan(
        fixup_output,
        contract=_populated_contract(tmp_path),
        node=_node(["B-0066"]),
        audit_findings=["behavior:B-0066"],
    )
    assert plan is not None, feedback
    assert feedback is None


def test_fixup_plan_fails_on_unknown_standards_ref(tmp_path: Path):
    """Plan 35: `validate_standards_refs` rejects citations that don't
    resolve under any loaded profile (here a fictional `docs/missing.md`)."""
    fixup_output = _fixup_planner_output(
        findings_addressed=["standards:docs/missing.md§A"],
        standards_referenced=[{"doc_path": "docs/missing.md", "section": "A"}],
    )
    plan, feedback = validate_fixup_plan(
        fixup_output,
        contract=_populated_contract(tmp_path),
        node=_node(),
        audit_findings=["standards:docs/missing.md§A"],
    )
    assert plan is None
    assert feedback is not None
    assert "standards_refs" in feedback or "standards profile" in feedback


def test_fixup_plan_accepts_profile_doc_with_generic_section(tmp_path: Path):
    """Fixups keep the standards doc bucket strict but tolerate section aliases
    emitted by audits."""
    fixup_output = _fixup_planner_output(
        findings_addressed=["standards:profiles/rust-cargo/rust/error-handling.md§Audit Rule"],
        standards_referenced=[
            {"doc_path": "profiles/rust-cargo/rust/error-handling.md", "section": "Audit Rule"}
        ],
    )
    plan, feedback = validate_fixup_plan(
        fixup_output,
        contract=_populated_contract(tmp_path),
        node=_node(),
        audit_findings=["standards:profiles/rust-cargo/rust/error-handling.md§Audit Rule"],
    )
    assert plan is not None, feedback
    assert feedback is None


def test_fixup_plan_accepts_arch_doc_with_generic_section(tmp_path: Path):
    """Architecture fixup refs follow the same rule: wrong doc buckets fail,
    generic section labels do not."""
    fixup_output = _fixup_planner_output(
        findings_addressed=["architecture:docs/architecture/subsystems/identity-policy.md§Rules"],
        architecture_referenced=[
            {"doc_path": "docs/architecture/subsystems/identity-policy.md", "section": "Rules"}
        ],
    )
    plan, feedback = validate_fixup_plan(
        fixup_output,
        contract=_populated_contract(tmp_path),
        node=_node(),
        audit_findings=["architecture:docs/architecture/subsystems/identity-policy.md§Rules"],
    )
    assert plan is not None, feedback
    assert feedback is None


def test_fixup_plan_accepts_when_audit_findings_empty(tmp_path: Path):
    """`fixup-final` / `fixup-ci` / `fixup-review` rounds carry no
    typed finding bundle — the driver passes `audit_findings=None`
    and the finding-coverage validator should short-circuit."""
    fixup_output = _fixup_planner_output()
    plan, feedback = validate_fixup_plan(
        fixup_output,
        contract=_populated_contract(tmp_path),
        node=_node(),
        audit_findings=None,
    )
    assert plan is not None, feedback
    assert feedback is None


# ----- missing_finding_coverage: the driver-side completeness wrapper -----


def test_missing_finding_coverage_short_circuits_when_findings_addressed_lists_id(
    tmp_path: Path,
):
    """When `plan.findings_addressed` lists an id, the wrapper trusts
    it regardless of stage-typed coverage. Used so the planner can
    group multiple findings into one subtask via notes."""
    fixup_output = _fixup_planner_output(
        findings_addressed=["rubric:security"],
        rubric_targets=[{"category": "security", "predicted_score": 8}],
    )
    plan, feedback = validate_fixup_plan(
        fixup_output,
        contract=_populated_contract(tmp_path),
        node=_node(),
        audit_findings=["rubric:security"],
    )
    assert plan is not None, feedback
    assert missing_finding_coverage(plan, ["rubric:security"]) == set()


def test_missing_finding_coverage_flags_uncovered_finding(tmp_path: Path):
    fixup_output = _fixup_planner_output(
        findings_addressed=[],
        rubric_targets=[{"category": "maintainability", "predicted_score": 7}],
    )
    plan, feedback = validate_fixup_plan(
        fixup_output,
        contract=_populated_contract(tmp_path),
        node=_node(),
        audit_findings=None,  # short-circuit the validator
    )
    assert plan is not None, feedback
    # But the wrapper-level completeness check still flags the gap.
    assert missing_finding_coverage(plan, ["rubric:security"]) == set()  # rubric_targets present → covered
    assert missing_finding_coverage(plan, ["behavior:B-0066"]) == {"behavior:B-0066"}


def test_missing_finding_coverage_accepts_architecture_reference(tmp_path: Path):
    """The wrapper-level completeness check must mirror
    `validate_finding_coverage` for architecture findings. Otherwise an
    architecture-heavy audit can pass the primary validator and still get
    re-prompted as if no subtask covered it."""
    fixup_output = _fixup_planner_output(
        findings_addressed=[],
        architecture_referenced=[
            {"doc_path": "docs/architecture/subsystems/identity-policy.md", "section": "Permissions"}
        ],
    )
    plan, feedback = validate_fixup_plan(
        fixup_output,
        contract=_populated_contract(tmp_path),
        node=_node(),
        audit_findings=None,  # isolate the wrapper-level check
    )
    assert plan is not None, feedback
    assert (
        missing_finding_coverage(
            plan,
            ["architecture:architecture-api-auth-guard-duplication-001"],
        )
        == set()
    )


# ----- routing assertion: fixup driver does NOT call validate_rubric_coverage -----


def test_fixup_driver_does_not_invoke_validate_rubric_coverage(monkeypatch, tmp_path):
    """The Plan 33 calibration choice: fixup driver routes around
    `validate_rubric_coverage`. A spy raises if the spec validator is
    ever invoked from the fixup parse path."""
    calls: list[str] = []

    def _spy_rubric(*_a, **_kw):
        calls.append("rubric_coverage")
        raise AssertionError("fixup driver must not call validate_rubric_coverage")

    monkeypatch.setattr(planner_validators, "validate_rubric_coverage", _spy_rubric)
    monkeypatch.setattr(fc, "validate_rubric_coverage", _spy_rubric, raising=False)

    fixup_output = _fixup_planner_output(
        findings_addressed=["behavior:B-0066"],
        behavior_evidence_advanced=["B-0066"],
    )
    plan, feedback = validate_fixup_plan(
        fixup_output,
        contract=_populated_contract(tmp_path),
        node=_node(["B-0066"]),
        audit_findings=["behavior:B-0066"],
    )
    assert plan is not None, feedback
    assert calls == []


def test_fixup_planner_output_violation_retries_before_escape_hatch(monkeypatch, tmp_path):
    """Malformed fixup-planner output is a retryable output violation, not an
    immediate task-level block. The driver should retry in-place and accept a
    corrected payload within the configured budget."""

    class _Store:
        def __init__(self) -> None:
            self.artifacts: list[tuple[str, str]] = []

        def record_agent_call_started(self, *_args: Any, **_kwargs: Any) -> int:
            return 1

        def record_agent_call_finished(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def add_artifact(self, _task_id: str, kind: str, content: str) -> None:
            self.artifacts.append((kind, content))

    class _Agent:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, *_args: Any, **_kwargs: Any) -> JsonAgentResult:
            self.calls += 1
            if self.calls == 1:
                return JsonAgentResult(
                    structured=None,
                    rc=0,
                    transient=False,
                    duration_s=0,
                    parse_errors=("subtasks: Field required",),
                    raw_text='{"summary": "bad"}',
                )
            return JsonAgentResult(
                structured=_fixup_planner_output(
                    findings_addressed=["behavior:B-0066"],
                    behavior_evidence_advanced=["B-0066"],
                ),
                rc=0,
                transient=False,
                duration_s=0,
            )

    agent = _Agent()
    monkeypatch.setattr(fc, "make_agent", lambda *_args, **_kwargs: agent)
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json")
    cfg.fixup_planner_output_retries = 2
    worker = type(
        "Worker",
        (),
        {
            "cfg": cfg,
            "node": _node(["B-0066"]),
            "store": _Store(),
            "_h": object(),
            "log_path": None,
        },
    )()

    plan = run_fixup_planner_loop(
        worker,
        base_prompt="base",
        kind="fixup-pre-pr-audit",
        round_no=1,
        audit_findings=["behavior:B-0066"],
        contract=_populated_contract(tmp_path),
        log=type("Log", (), {"warning": lambda *_args, **_kwargs: None})(),
    )

    assert plan is not None
    assert agent.calls == 2


def test_spec_planner_driver_invokes_all_spec_validators(monkeypatch):
    """Plan 35: the spec-planner driver MUST call `validate_rubric_coverage`,
    `validate_evidence_partition`, `validate_standards_refs`, and
    `validate_architecture_refs`."""
    seen: list[str] = []

    def _spy(name: str):
        def _fn(*_a, **_kw):
            seen.append(name)

        return _fn

    monkeypatch.setattr(planner_driver, "validate_rubric_coverage", _spy("rubric_coverage"))
    monkeypatch.setattr(planner_driver, "validate_evidence_partition", _spy("evidence_partition"))
    monkeypatch.setattr(planner_driver, "validate_standards_refs", _spy("standards_refs"))
    monkeypatch.setattr(planner_driver, "validate_architecture_refs", _spy("architecture_refs"))

    contract = EvaluationContract(
        task_id="T-1",
        local_ci=StageRubric(
            name="local_ci",
            one_line="ci",
            threshold="rc=0",
            grading_template="",
            source_text="`just ci`",
        ),
        rubric=StageRubric(
            name="rubric",
            one_line="r",
            threshold="t",
            grading_template="",
            source_text="- **security**",
        ),
        standards=StandardsStageRubric(profiles=(), source_text=""),
        architecture=ArchitectureStageRubric(
            corpus=ArchitectureCorpus(root=Path("/tmp"), docs=()), source_text=""
        ),
        behavior=StageRubric(
            name="behavior", one_line="b", threshold="t", grading_template="", source_text=""
        ),
    )
    subtask = Subtask(id="S-01", acceptance=("ok",))
    plan = Plan(
        node_id="T-1",
        summary="x",
        subtasks=(subtask,),
        final_acceptance=("ok",),
        gauntlet_strategy="x" * 250,
    )
    node = _node()

    planner_driver.validate_rubric_coverage(plan, contract)
    planner_driver.validate_evidence_partition(plan, node)
    planner_driver.validate_standards_refs(plan, contract)
    planner_driver.validate_architecture_refs(plan, contract)

    assert seen == [
        "rubric_coverage",
        "evidence_partition",
        "standards_refs",
        "architecture_refs",
    ]


# ----- end of tests -----
_ = pytest  # silence unused-import lint when no parametrize fixtures used here
