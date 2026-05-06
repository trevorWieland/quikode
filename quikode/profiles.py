"""Built-in project profiles.

Profiles hold project/domain assumptions that should not live in generic
worker, scheduler, or prompt code. Config files can select a profile and then
override any individual field locally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, cast

ProfileName = Literal["generic-python", "generic-rust", "tanren"]


@dataclass(frozen=True)
class ProjectProfile:
    name: ProfileName
    default_image: str
    base_branch: str = "main"
    local_ci_command: str = ""
    subtask_check_command: str = ""
    pre_commit_runner: Literal["auto", "lefthook", "pre-commit", "none"] = "auto"
    resource_defaults: dict[str, int | bool] = field(default_factory=dict)
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
        validation_commands=("cargo fmt --check", "cargo clippy --workspace", "cargo test --workspace"),
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
}


def get_profile(name: str | None) -> ProjectProfile:
    key = (name or "tanren").strip()
    if key not in BUILTIN_PROFILES:
        valid = ", ".join(sorted(BUILTIN_PROFILES))
        raise ValueError(f"unknown project profile {key!r}; expected one of: {valid}")
    return BUILTIN_PROFILES[cast(ProfileName, key)]
