"""Plan 35 PR-A: tests for the new bucket-routing validators.

`validate_standards_refs` accepts profile-doc citations in
`standards_referenced` and rejects architecture-doc citations with the
bucket-correction message. `validate_architecture_refs` mirrors the
discipline with the buckets reversed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from quikode.architecture_docs import load_architecture
from quikode.config import Config
from quikode.evaluation_contract import (
    ArchitectureStageRubric,
    EvaluationContract,
    StageRubric,
    StandardsStageRubric,
)
from quikode.planner_validators import (
    PlannerValidationError,
    validate_architecture_refs,
    validate_standards_refs,
)
from quikode.standards_profiles import load_profiles
from quikode.subtask_schema import (
    ArchitectureRef,
    Plan,
    StandardsRef,
    Subtask,
)

_PROFILE_FIX = Path(__file__).resolve().parent / "fixtures" / "standards_profiles"
_ARCH_FIX = Path(__file__).resolve().parent / "fixtures" / "architecture_docs"


def _build_contract(tmp_path: Path) -> EvaluationContract:
    """Stand up a config that points at the fixture trees."""
    repo = tmp_path
    profile_root = repo / "profiles"
    profile_root.mkdir()
    shutil.copytree(_PROFILE_FIX / "rust-cargo", profile_root / "rust-cargo")
    arch_root = repo / "docs" / "architecture"
    arch_root.mkdir(parents=True)
    shutil.copytree(_ARCH_FIX / "subsystems", arch_root / "subsystems")
    cfg = Config(
        repo_path=repo,
        dag_path=repo / "dag.json",
        standards_profiles_dir=profile_root,
        standards_profiles=["rust-cargo"],
        architecture_docs_dir=arch_root,
        architecture_doc_globs=["**/*.md"],
    )
    profiles = load_profiles(cfg)
    corpus = load_architecture(cfg)
    return EvaluationContract(
        task_id="R-T",
        local_ci=StageRubric(
            name="local_ci", one_line="", threshold="rc=0", grading_template="", source_text=""
        ),
        rubric=StageRubric(name="rubric", one_line="", threshold="", grading_template="", source_text=""),
        standards=StandardsStageRubric(profiles=profiles, source_text=""),
        architecture=ArchitectureStageRubric(corpus=corpus, source_text=""),
        behavior=StageRubric(name="behavior", one_line="", threshold="", grading_template="", source_text=""),
    )


def _subtask(
    sid: str,
    *,
    standards: tuple[StandardsRef, ...] = (),
    architecture: tuple[ArchitectureRef, ...] = (),
) -> Subtask:
    return Subtask(
        id=sid,
        title=sid,
        depends_on=(),
        files_to_touch=(),
        boundary="",
        acceptance=("ok",),
        standards_referenced=standards,
        architecture_referenced=architecture,
    )


def _plan(*subtasks: Subtask) -> Plan:
    return Plan(
        node_id="R-T",
        summary="x",
        subtasks=subtasks,
        final_acceptance=("ok",),
    )


# Standards refs in the standards bucket — accept.


def test_validate_standards_refs_accepts_profile_doc_section(tmp_path: Path):
    contract = _build_contract(tmp_path)
    cited = contract.standards.profiles[0].docs[0].repo_relative
    plan = _plan(
        _subtask(
            "S-01",
            standards=(StandardsRef(doc_path=cited, section="Rules"),),
        )
    )
    # Must not raise — the cited path lives under a loaded profile and
    # the heading exists.
    validate_standards_refs(plan, contract)


# Standards refs in the wrong bucket — reject with bucket-correction text.


def test_validate_standards_refs_rejects_architecture_doc(tmp_path: Path):
    """R-0002's exact citation pattern: an architecture doc dropped into
    `standards_referenced`. Bucket-correction message should fire.
    """
    contract = _build_contract(tmp_path)
    plan = _plan(
        _subtask(
            "S-01",
            standards=(
                StandardsRef(
                    doc_path="docs/architecture/subsystems/identity-policy.md",
                    section="Permissions",
                ),
            ),
        )
    )
    with pytest.raises(PlannerValidationError) as exc_info:
        validate_standards_refs(plan, contract)
    assert exc_info.value.which == "standards_refs"
    assert "architecture_referenced" in exc_info.value.message
    assert "S-01" in exc_info.value.message


def test_validate_standards_refs_rejects_unknown_section(tmp_path: Path):
    contract = _build_contract(tmp_path)
    cited = contract.standards.profiles[0].docs[0].repo_relative
    plan = _plan(
        _subtask(
            "S-01",
            standards=(
                StandardsRef(
                    doc_path=cited,
                    section="No Such Section",
                ),
            ),
        )
    )
    with pytest.raises(PlannerValidationError) as exc_info:
        validate_standards_refs(plan, contract)
    assert "section heading" in exc_info.value.message
    assert "No Such Section" in exc_info.value.message


# Architecture refs in the architecture bucket — accept.


def test_validate_architecture_refs_accepts_arch_doc_section(tmp_path: Path):
    contract = _build_contract(tmp_path)
    plan = _plan(
        _subtask(
            "S-01",
            architecture=(
                ArchitectureRef(
                    doc_path=contract.architecture.corpus.docs[0].repo_relative,
                    section="Permissions",
                ),
            ),
        )
    )
    validate_architecture_refs(plan, contract)


# Architecture refs in the wrong bucket — reject.


def test_validate_architecture_refs_rejects_profile_doc(tmp_path: Path):
    contract = _build_contract(tmp_path)
    cited = contract.standards.profiles[0].docs[0].repo_relative
    plan = _plan(
        _subtask(
            "S-01",
            architecture=(
                ArchitectureRef(
                    doc_path=cited,
                    section="Rules",
                ),
            ),
        )
    )
    with pytest.raises(PlannerValidationError) as exc_info:
        validate_architecture_refs(plan, contract)
    assert exc_info.value.which == "architecture_refs"
    assert "standards_referenced" in exc_info.value.message
    assert "S-01" in exc_info.value.message


def test_validate_architecture_refs_rejects_unknown_section(tmp_path: Path):
    contract = _build_contract(tmp_path)
    plan = _plan(
        _subtask(
            "S-01",
            architecture=(
                ArchitectureRef(
                    doc_path=contract.architecture.corpus.docs[0].repo_relative,
                    section="Bogus Heading",
                ),
            ),
        )
    )
    with pytest.raises(PlannerValidationError) as exc_info:
        validate_architecture_refs(plan, contract)
    assert "section heading" in exc_info.value.message


def test_validators_no_op_on_empty_refs(tmp_path: Path):
    contract = _build_contract(tmp_path)
    plan = _plan(_subtask("S-01"))
    validate_standards_refs(plan, contract)
    validate_architecture_refs(plan, contract)
