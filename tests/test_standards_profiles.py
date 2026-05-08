"""Plan 35 PR-A: tests for the standards-profile loader.

Coverage:

- Hand-rolled YAML frontmatter parser handles the seed file shape.
- `load_profiles` walks the configured tree.
- `find_doc` returns None for paths outside loaded profiles.
- `find_section` is case-insensitive and whitespace-folded.
- Malformed frontmatter raises with the offending file path.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from quikode.config import Config
from quikode.standards_profiles import (
    StandardsDoc,
    find_doc,
    find_section,
    load_profiles,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "standards_profiles"
_SEED_ROOT = Path(__file__).resolve().parent.parent / "quikode" / "standards_profiles_seed"


def _cfg(repo_root: Path, profiles: list[str], *, profiles_dir: Path | None = None) -> Config:
    return Config(
        repo_path=repo_root,
        dag_path=repo_root / "dag.json",
        standards_profiles_dir=profiles_dir or (repo_root / "profiles"),
        standards_profiles=profiles,
        architecture_docs_dir=repo_root / "docs" / "architecture",
    )


def test_load_profiles_walks_seed_files_round_trip(tmp_path: Path):
    """Round-trip the bundled seed files through the loader."""
    repo = tmp_path
    (repo / "profiles").mkdir()
    shutil.copytree(_SEED_ROOT / "rust-cargo", repo / "profiles" / "rust-cargo")
    cfg = _cfg(repo, ["rust-cargo"])
    profiles = load_profiles(cfg)
    assert len(profiles) == 1
    profile = profiles[0]
    assert profile.name == "rust-cargo"
    # 9 .md files in the rust-cargo seed.
    assert len(profile.docs) == 9
    error_handling = next(d for d in profile.docs if d.name == "error-handling")
    assert error_handling.category == "rust"
    assert error_handling.importance == "critical"
    assert "rust" in error_handling.applies_to_languages
    assert "Rules" in error_handling.sections


def test_sections_parse_h1_h2_h3():
    """`#`, `##`, and `###` headings all become section names."""
    repo = _FIXTURES
    cfg = _cfg(repo, ["rust-cargo"], profiles_dir=_FIXTURES)
    profiles = load_profiles(cfg)
    doc = profiles[0].docs[0]
    # The fixture has `# Error Handling`, `## Rules`, `## Notes`.
    assert "Error Handling" in doc.sections
    assert "Rules" in doc.sections
    assert "Notes" in doc.sections


def test_find_doc_returns_doc_for_repo_relative_path():
    repo = _FIXTURES
    cfg = _cfg(repo, ["rust-cargo"], profiles_dir=_FIXTURES)
    profiles = load_profiles(cfg)
    doc = find_doc(profiles, "rust-cargo/rust/error-handling.md")
    assert isinstance(doc, StandardsDoc)
    assert doc.name == "error-handling"


def test_find_doc_returns_none_for_paths_outside_profiles():
    repo = _FIXTURES
    cfg = _cfg(repo, ["rust-cargo"], profiles_dir=_FIXTURES)
    profiles = load_profiles(cfg)
    # Architecture-doc style path — should NOT match any profile doc.
    assert find_doc(profiles, "docs/architecture/subsystems/identity-policy.md") is None
    # Repo-relative-but-unknown path.
    assert find_doc(profiles, "profiles/rust-cargo/rust/missing.md") is None


def test_find_section_is_case_and_whitespace_insensitive(tmp_path: Path):
    repo = _FIXTURES
    cfg = _cfg(repo, ["rust-cargo"], profiles_dir=_FIXTURES)
    profiles = load_profiles(cfg)
    doc = profiles[0].docs[0]
    assert find_section(doc, "Rules") is True
    assert find_section(doc, "rules") is True
    assert find_section(doc, "  RULES  ") is True
    assert find_section(doc, "no-such-section") is False


def test_load_profiles_raises_on_missing_required_key(tmp_path: Path):
    """A frontmatter missing `kind` raises RuntimeError naming the file."""
    repo = tmp_path
    profile_root = repo / "profiles" / "broken" / "rust"
    profile_root.mkdir(parents=True)
    bad = profile_root / "bad.md"
    # Missing `kind`.
    bad.write_text("---\nname: bad\ncategory: rust\nimportance: high\n---\n\nbody\n")
    cfg = _cfg(repo, ["broken"])
    with pytest.raises(RuntimeError) as exc_info:
        load_profiles(cfg)
    assert "bad.md" in str(exc_info.value)
    assert "kind" in str(exc_info.value)


def test_load_profiles_raises_on_unterminated_frontmatter(tmp_path: Path):
    repo = tmp_path
    profile_root = repo / "profiles" / "broken" / "x"
    profile_root.mkdir(parents=True)
    bad = profile_root / "unterminated.md"
    bad.write_text("---\nkind: standard\nname: x\ncategory: x\nimportance: high\n\nno close fence\n")
    cfg = _cfg(repo, ["broken"])
    with pytest.raises(RuntimeError) as exc_info:
        load_profiles(cfg)
    assert "unterminated.md" in str(exc_info.value)


def test_load_profiles_raises_on_invalid_importance(tmp_path: Path):
    repo = tmp_path
    profile_root = repo / "profiles" / "broken" / "x"
    profile_root.mkdir(parents=True)
    bad = profile_root / "bad-importance.md"
    bad.write_text("---\nkind: standard\nname: x\ncategory: x\nimportance: super-high\n---\n\nbody\n")
    cfg = _cfg(repo, ["broken"])
    with pytest.raises(RuntimeError) as exc_info:
        load_profiles(cfg)
    assert "bad-importance.md" in str(exc_info.value)
    assert "importance" in str(exc_info.value)


def test_load_profiles_empty_when_no_profiles_configured(tmp_path: Path):
    cfg = _cfg(tmp_path, [])
    assert load_profiles(cfg) == ()


def test_load_profiles_raises_when_named_profile_missing(tmp_path: Path):
    cfg = _cfg(tmp_path, ["rust-cargo"])
    with pytest.raises(RuntimeError) as exc_info:
        load_profiles(cfg)
    assert "rust-cargo" in str(exc_info.value)
