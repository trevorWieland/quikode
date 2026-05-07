from __future__ import annotations

from quikode.config import Config
from quikode.config_loader import load_config
from quikode.config_template import render_config_toml
from quikode.profiles import BUILTIN_PROFILES, get_profile


def test_builtin_profiles_cover_required_names():
    assert {"generic-python", "generic-rust", "rust-just", "tanren", "zaimu"} <= set(BUILTIN_PROFILES)


def test_tanren_profile_owns_tanren_defaults():
    profile = get_profile("tanren")
    assert profile.local_ci_command == "just ci"
    assert profile.subtask_check_command == "just check"
    assert profile.merge_policy == "squash-delete-branch"
    assert profile.base_branch == "main"
    assert profile.postgres_db == "tanren"
    assert "BDD" in profile.bdd_conventions


def test_zaimu_profile_targets_dev_with_laptop_defaults(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    dag = tmp_path / "dag.json"
    dag.write_text('{"nodes":[]}')
    cfg_dir = tmp_path / ".quikode"
    cfg_dir.mkdir()
    cfg_dir.joinpath("config.toml").write_text(
        f'profile = "zaimu"\nrepo_path = "{repo}"\ndag_path = "{dag}"\n'
    )

    cfg = load_config(tmp_path)

    assert cfg.profile == "zaimu"
    assert cfg.base_branch == "dev"
    assert cfg.image_tag == "quikode-zaimu-dev:latest"
    assert cfg.local_ci_command == "just ci"
    assert cfg.subtask_check_command == "just check"
    assert cfg.postgres_db == "zaimu"
    assert cfg.database_url == "postgres://postgres:dev@postgres:5432/zaimu"
    assert cfg.cpu_per_task == 3
    assert cfg.mem_per_task_gb == 8
    assert cfg.max_parallel_auto is True


def test_rust_just_profile_is_generic_just_ci(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    dag = tmp_path / "dag.json"
    dag.write_text('{"nodes":[]}')
    cfg_dir = tmp_path / ".quikode"
    cfg_dir.mkdir()
    cfg_dir.joinpath("config.toml").write_text(
        f'profile = "rust-just"\nrepo_path = "{repo}"\ndag_path = "{dag}"\n'
    )

    cfg = load_config(tmp_path)

    assert cfg.profile == "rust-just"
    assert cfg.base_branch == "main"
    assert cfg.local_ci_command == "just ci"
    assert cfg.subtask_check_command == "just check"
    assert cfg.postgres_db == "app"


def test_render_config_toml_emits_profile_defaults(tmp_path):
    repo = tmp_path / "repo"
    dag = tmp_path / "dag.json"
    rendered = render_config_toml(repo_path=repo, dag_path=dag, profile="zaimu")

    assert 'profile = "zaimu"' in rendered
    assert f'repo_path = "{repo}"' in rendered
    assert f'dag_path = "{dag}"' in rendered
    assert 'base_branch = "dev"' in rendered
    assert 'postgres_db = "zaimu"' in rendered
    assert "max_parallel_auto = true" in rendered


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
    assert cfg.postgres_enabled is False
    assert cfg.database_url == ""


def test_config_default_profile_preserves_existing_tanren_behavior(tmp_path):
    cfg = Config(repo_path=tmp_path, dag_path=tmp_path / "dag.json")
    assert cfg.profile == "tanren"
    assert cfg.local_ci_command == "just ci"
