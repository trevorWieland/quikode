from __future__ import annotations

from quikode.config import Config
from quikode.config_loader import load_config
from quikode.profiles import BUILTIN_PROFILES, get_profile


def test_builtin_profiles_cover_required_names():
    assert {"generic-python", "generic-rust", "tanren"} <= set(BUILTIN_PROFILES)


def test_tanren_profile_owns_tanren_defaults():
    profile = get_profile("tanren")
    assert profile.local_ci_command == "just ci"
    assert profile.subtask_check_command == "just check"
    assert profile.merge_policy == "squash-delete-branch"
    assert "BDD" in profile.bdd_conventions


def test_generic_python_profile_defaults_are_not_tanren(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    dag = tmp_path / "dag.json"
    dag.write_text('{"nodes":[]}')
    cfg_dir = tmp_path / ".quikode"
    cfg_dir.mkdir()
    cfg_dir.joinpath("config.toml").write_text(
        f'profile = "generic-python"\nrepo_path = "{repo}"\ndag_path = "{dag}"\n'
    )

    cfg = load_config(tmp_path)

    assert cfg.profile == "generic-python"
    assert cfg.image_tag == "quikode-python-dev:latest"
    assert cfg.local_ci_command == "python -m pytest"
    assert cfg.subtask_check_command == "python -m pytest"


def test_config_default_profile_preserves_existing_tanren_behavior(tmp_path):
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json")
    assert cfg.profile == "tanren"
    assert cfg.local_ci_command == "just ci"
