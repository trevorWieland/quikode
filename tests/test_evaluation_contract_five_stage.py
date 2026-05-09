"""Plan 35 PR-A: tests for the five-stage EvaluationContract round-trip.

Plan 35 widens the contract from four stages to five — adds the
`architecture` stage alongside `standards`. This test pins the build →
persist → load cycle preserves all five stages exactly (including the
loaded standards profiles and architecture corpus).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from quikode.architecture_docs import ArchitectureCorpus
from quikode.config import Config
from quikode.dag import Node
from quikode.evaluation_contract import (
    ArchitectureStageRubric,
    EvaluationContract,
    StandardsStageRubric,
    audit_corpora_need_refresh,
    build_for,
)

_PROFILE_FIX = Path(__file__).resolve().parent / "fixtures" / "standards_profiles"
_ARCH_FIX = Path(__file__).resolve().parent / "fixtures" / "architecture_docs"


def _node(node_id: str = "R-FIVE") -> Node:
    return Node(
        id=node_id,
        kind="behavior",
        milestone="M-1",
        title="five-stage smoke",
        scope="exercise the five-stage contract",
        depends_on=(),
        completes_behaviors=(),
        supports_behaviors=(),
        boundary_with_neighbors="",
        expected_evidence=(),
        playbook=(),
        rationale="",
        risks=(),
        raw={},
    )


def _populated_cfg(tmp_path: Path) -> Config:
    repo = tmp_path
    profile_root = repo / "profiles"
    profile_root.mkdir()
    shutil.copytree(_PROFILE_FIX / "rust-cargo", profile_root / "rust-cargo")
    arch_root = repo / "docs" / "architecture"
    arch_root.mkdir(parents=True)
    shutil.copytree(_ARCH_FIX / "subsystems", arch_root / "subsystems")
    return Config(
        repo_path=repo,
        dag_path=repo / "dag.json",
        state_dir=repo / ".quikode",
        prompts_dir=repo / "prompts-missing",
        worktree_root=repo / ".quikode" / "worktrees",
        log_dir=repo / ".quikode" / "logs",
        sccache_dir=repo / ".quikode" / "sccache",
        pre_pr_rubric_categories=["security", "maintainability"],
        pre_pr_rubric_min_score=7,
        standards_profiles_dir=profile_root,
        standards_profiles=["rust-cargo"],
        architecture_docs_dir=arch_root,
        architecture_doc_globs=["**/*.md"],
    )


def test_build_for_emits_all_five_stages(tmp_path: Path):
    cfg = _populated_cfg(tmp_path)
    contract = build_for(_node(), cfg)
    assert contract.local_ci.name == "local_ci"
    assert contract.rubric.name == "rubric"
    assert isinstance(contract.standards, StandardsStageRubric)
    assert isinstance(contract.architecture, ArchitectureStageRubric)
    assert contract.behavior.name == "behavior"
    assert len(contract.standards.profiles) == 1
    assert contract.standards.profiles[0].name == "rust-cargo"
    assert isinstance(contract.architecture.corpus, ArchitectureCorpus)
    assert len(contract.architecture.corpus.docs) == 1


def test_persist_load_round_trip_preserves_five_stages(tmp_path: Path):
    cfg = _populated_cfg(tmp_path)
    contract = build_for(_node(), cfg)
    contract.persist(cfg.state_dir, "R-FIVE")
    loaded = EvaluationContract.load(cfg.state_dir, "R-FIVE")
    assert loaded == contract
    # And the corpora survive serialization.
    assert loaded.standards.profiles[0].docs[0].name == (contract.standards.profiles[0].docs[0].name)
    assert loaded.architecture.corpus.docs[0].title == (contract.architecture.corpus.docs[0].title)


def test_build_for_with_no_profiles_still_round_trips(tmp_path: Path):
    repo = tmp_path
    cfg = Config(
        repo_path=repo,
        dag_path=repo / "dag.json",
        state_dir=repo / ".quikode",
        prompts_dir=repo / "prompts-missing",
        worktree_root=repo / ".quikode" / "worktrees",
        log_dir=repo / ".quikode" / "logs",
        sccache_dir=repo / ".quikode" / "sccache",
        pre_pr_rubric_categories=["security"],
        pre_pr_rubric_min_score=7,
        standards_profiles_dir=repo / "profiles",
        standards_profiles=[],
        architecture_docs_dir=repo / "docs" / "architecture",
    )
    contract = build_for(_node(), cfg)
    assert contract.standards.profiles == ()
    assert contract.architecture.corpus.docs == ()
    contract.persist(cfg.state_dir, "R-FIVE")
    loaded = EvaluationContract.load(cfg.state_dir, "R-FIVE")
    assert loaded == contract


def test_persisted_empty_audit_corpora_refresh_when_config_now_loads_docs(tmp_path: Path):
    stale_cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        state_dir=tmp_path / ".quikode",
        prompts_dir=tmp_path / "prompts-missing",
        worktree_root=tmp_path / ".quikode" / "worktrees",
        log_dir=tmp_path / ".quikode" / "logs",
        sccache_dir=tmp_path / ".quikode" / "sccache",
        pre_pr_rubric_categories=["security"],
        pre_pr_rubric_min_score=7,
        standards_profiles_dir=tmp_path / "profiles",
        standards_profiles=[],
        architecture_docs_dir=tmp_path / "docs" / "architecture",
    )
    stale = build_for(_node(), stale_cfg)

    fresh_cfg = _populated_cfg(tmp_path)

    assert audit_corpora_need_refresh(stale, fresh_cfg)
    refreshed = build_for(_node(), fresh_cfg)
    assert not audit_corpora_need_refresh(refreshed, fresh_cfg)
