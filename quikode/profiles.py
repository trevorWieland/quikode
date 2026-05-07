"""Built-in project profiles.

Profiles hold project/domain assumptions that should not live in generic
worker, scheduler, or prompt code. Config files can select a profile and then
override any individual field locally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, cast

ProfileName = Literal["generic-python", "generic-rust", "rust-just", "tanren", "zaimu"]


@dataclass(frozen=True)
class ProjectProfile:
    name: ProfileName
    default_image: str
    base_branch: str = "main"
    local_ci_command: str = ""
    subtask_check_command: str = ""
    pre_commit_runner: Literal["auto", "lefthook", "pre-commit", "none"] = "auto"
    resource_defaults: dict[str, int | bool] = field(default_factory=dict)
    postgres_enabled: bool = True
    postgres_db: str = "tanren"
    postgres_user: str = "postgres"
    postgres_password: str = "dev"
    postgres_image: str = "postgres:16-alpine"
    database_url: str = "postgres://postgres:dev@postgres:5432/tanren"
    merge_policy: str = "manual"
    manual_probe_credentials: str = ""
    domain_prompt_context: str = ""
    bdd_conventions: str = ""
    validation_commands: tuple[str, ...] = ()


BUILTIN_PROFILES: dict[ProfileName, ProjectProfile] = {
    "generic-python": ProjectProfile(
        name="generic-python",
        default_image="quikode-python-dev:latest",
        local_ci_command="python -m pytest",
        subtask_check_command="python -m pytest",
        pre_commit_runner="auto",
        resource_defaults={
            "cpu_per_task": 2,
            "mem_per_task_gb": 4,
            "host_reserved_cpu": 2,
            "host_reserved_mem_gb": 4,
        },
        postgres_enabled=False,
        database_url="",
        validation_commands=("python -m pytest",),
    ),
    "generic-rust": ProjectProfile(
        name="generic-rust",
        default_image="quikode-rust-dev:latest",
        local_ci_command="cargo test --workspace",
        subtask_check_command="cargo check --workspace",
        pre_commit_runner="auto",
        resource_defaults={
            "cpu_per_task": 4,
            "mem_per_task_gb": 8,
            "host_reserved_cpu": 2,
            "host_reserved_mem_gb": 8,
        },
        postgres_enabled=False,
        database_url="",
        validation_commands=("cargo fmt --check", "cargo clippy --workspace", "cargo test --workspace"),
    ),
    "rust-just": ProjectProfile(
        name="rust-just",
        default_image="quikode-rust-just-dev:latest",
        local_ci_command="just ci",
        subtask_check_command="just check",
        pre_commit_runner="auto",
        resource_defaults={
            "cpu_per_task": 3,
            "mem_per_task_gb": 8,
            "host_reserved_cpu": 2,
            "host_reserved_mem_gb": 8,
            "max_parallel_auto": True,
        },
        postgres_db="app",
        database_url="postgres://postgres:dev@postgres:5432/app",
        validation_commands=("just check", "just ci"),
    ),
    "tanren": ProjectProfile(
        name="tanren",
        default_image="quikode-tanren-dev:latest",
        local_ci_command="just ci",
        subtask_check_command="just check",
        pre_commit_runner="auto",
        resource_defaults={
            "cpu_per_task": 4,
            "mem_per_task_gb": 12,
            "host_reserved_cpu": 4,
            "host_reserved_mem_gb": 16,
        },
        merge_policy="squash-delete-branch",
        domain_prompt_context=(
            "Tanren work uses a Rust/Node/Postgres stack, BDD behavior-proof "
            "features, and just-based validation."
        ),
        bdd_conventions=(
            "BDD features live under tests/bdd/features and must satisfy the "
            "project's behavior-proof tag checks."
        ),
        validation_commands=("just check", "just ci"),
    ),
    "zaimu": ProjectProfile(
        name="zaimu",
        default_image="quikode-zaimu-dev:latest",
        base_branch="dev",
        local_ci_command="just ci",
        subtask_check_command="just check",
        pre_commit_runner="auto",
        resource_defaults={
            "cpu_per_task": 3,
            "mem_per_task_gb": 8,
            "host_reserved_cpu": 2,
            "host_reserved_mem_gb": 8,
            "max_parallel_auto": True,
        },
        postgres_db="zaimu",
        database_url="postgres://postgres:dev@postgres:5432/zaimu",
        merge_policy="squash-delete-branch",
        domain_prompt_context=(
            "Zaimu work uses a Rust/Node/Postgres stack, BDD behavior-proof "
            "features, just-based validation, and PRs targeting dev."
        ),
        bdd_conventions=(
            "BDD features live under tests/bdd/features with closed tags "
            "@positive, @falsification, @cli, @api, @tui, and @web."
        ),
        validation_commands=("just check", "just ci"),
    ),
}


def get_profile(name: str | None) -> ProjectProfile:
    key = (name or "tanren").strip()
    if key not in BUILTIN_PROFILES:
        valid = ", ".join(sorted(BUILTIN_PROFILES))
        raise ValueError(f"unknown project profile {key!r}; expected one of: {valid}")
    return BUILTIN_PROFILES[cast(ProfileName, key)]
