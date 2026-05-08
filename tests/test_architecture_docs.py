"""Plan 35 PR-A: tests for the architecture-doc loader."""

from __future__ import annotations

import shutil
from pathlib import Path

from quikode.architecture_docs import (
    ArchitectureCorpus,
    ArchitectureDoc,
    find_arch_doc,
    find_arch_section,
    load_architecture,
)
from quikode.config import Config

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "architecture_docs"


def _cfg(repo_root: Path, globs: list[str] | None = None) -> Config:
    return Config(
        repo_path=repo_root,
        dag_path=repo_root / "dag.json",
        architecture_docs_dir=repo_root / "docs" / "architecture",
        architecture_doc_globs=globs or ["**/*.md"],
    )


def test_load_architecture_walks_globs(tmp_path: Path):
    repo = tmp_path
    arch_root = repo / "docs" / "architecture"
    arch_root.mkdir(parents=True)
    shutil.copytree(_FIXTURES / "subsystems", arch_root / "subsystems")
    cfg = _cfg(repo)
    corpus = load_architecture(cfg)
    assert len(corpus.docs) == 1
    doc = corpus.docs[0]
    assert isinstance(doc, ArchitectureDoc)
    assert doc.repo_relative.endswith("identity-policy.md")
    assert doc.title == "Identity Policy"


def test_section_parsing(tmp_path: Path):
    repo = tmp_path
    arch_root = repo / "docs" / "architecture"
    arch_root.mkdir(parents=True)
    shutil.copytree(_FIXTURES / "subsystems", arch_root / "subsystems")
    cfg = _cfg(repo)
    corpus = load_architecture(cfg)
    doc = corpus.docs[0]
    assert "Identity Policy" in doc.sections
    assert "Permissions" in doc.sections
    assert "Error Taxonomy" in doc.sections


def test_load_architecture_empty_when_dir_missing(tmp_path: Path):
    cfg = _cfg(tmp_path)
    corpus = load_architecture(cfg)
    assert isinstance(corpus, ArchitectureCorpus)
    assert corpus.docs == ()


def test_load_architecture_empty_when_globs_match_nothing(tmp_path: Path):
    repo = tmp_path
    arch_root = repo / "docs" / "architecture"
    arch_root.mkdir(parents=True)
    (arch_root / "irrelevant.txt").write_text("not markdown")
    cfg = _cfg(repo)
    corpus = load_architecture(cfg)
    assert corpus.docs == ()


def test_find_arch_doc(tmp_path: Path):
    repo = tmp_path
    arch_root = repo / "docs" / "architecture"
    arch_root.mkdir(parents=True)
    shutil.copytree(_FIXTURES / "subsystems", arch_root / "subsystems")
    cfg = _cfg(repo)
    corpus = load_architecture(cfg)
    doc = find_arch_doc(corpus, corpus.docs[0].repo_relative)
    assert isinstance(doc, ArchitectureDoc)
    assert find_arch_doc(corpus, "profiles/rust-cargo/rust/error-handling.md") is None


def test_find_arch_section_case_and_whitespace_insensitive(tmp_path: Path):
    repo = tmp_path
    arch_root = repo / "docs" / "architecture"
    arch_root.mkdir(parents=True)
    shutil.copytree(_FIXTURES / "subsystems", arch_root / "subsystems")
    cfg = _cfg(repo)
    corpus = load_architecture(cfg)
    doc = corpus.docs[0]
    assert find_arch_section(doc, "Permissions") is True
    assert find_arch_section(doc, "permissions") is True
    assert find_arch_section(doc, "  ERROR taxonomy  ") is True
    assert find_arch_section(doc, "Nonexistent") is False
