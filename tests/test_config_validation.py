from __future__ import annotations

from pathlib import Path

import pytest

from quikode.config import Config
from quikode.config_validation import ConfigValidationError, validate_launch_config

_PROFILE_FIX = Path(__file__).resolve().parent / "fixtures" / "standards_profiles"
_ARCH_FIX = Path(__file__).resolve().parent / "fixtures" / "architecture_docs"


def _cfg(tmp_path: Path, **overrides) -> Config:
    repo = tmp_path / "repo"
    repo.mkdir()
    dag = tmp_path / "dag.json"
    dag.write_text('{"nodes":[]}')
    cfg = Config(
        repo_path=repo,
        dag_path=dag,
        standards_profiles_dir=_PROFILE_FIX,
        standards_profiles=["rust-cargo"],
        architecture_docs_dir=_ARCH_FIX,
        architecture_doc_globs=["**/*.md"],
    )
    return cfg.model_copy(update=overrides)


def test_launch_config_accepts_loaded_audit_docs(tmp_path: Path) -> None:
    validate_launch_config(_cfg(tmp_path))


def test_launch_config_rejects_missing_standards_profiles(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, standards_profiles=[])

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_launch_config(cfg)

    message = str(exc_info.value)
    assert "standards_profiles" in message
    assert "runtime standards audits cannot run" in message


def test_launch_config_rejects_missing_architecture_docs(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, architecture_docs_dir=tmp_path / "missing-arch")

    with pytest.raises(ConfigValidationError) as exc_info:
        validate_launch_config(cfg)

    message = str(exc_info.value)
    assert "architecture_docs_dir" in message
    assert "no architecture docs loaded" in message
