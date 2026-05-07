"""Default configuration template."""

from __future__ import annotations

from .profiles import ProjectProfile, get_profile

DEFAULT_CONFIG_TOML = """\
# quikode config
profile = "tanren"
repo_path = "{repo_path}"
dag_path = "{dag_path}"
image_tag = "quikode-tanren-dev:latest"
postgres_enabled = true
postgres_db = "tanren"
postgres_user = "postgres"
postgres_password = "dev"
postgres_image = "postgres:16-alpine"
database_url = "postgres://postgres:dev@postgres:5432/tanren"
execution_backend = "docker"
max_parallel = 3
base_branch = "main"
pr_remote = "origin"
triage_budget_per_phase = 3

[execution]
# Reserved for future remote backends ("ssh-docker", "vm-sandbox").

[agents.planner]
cli = "codex"
model = "gpt-5.5"

[agents.doer]
cli = "opencode"
model = "zai-coding-plan/glm-5.1"

[agents.checker]
cli = "codex"
model = "gpt-5.3-codex"

[agents.triage]
cli = "codex"
model = "gpt-5.5"
"""


def render_config_toml(
    *,
    repo_path: object,
    dag_path: object,
    profile: str | ProjectProfile = "tanren",
) -> str:
    """Render a profile-aware fresh-workspace config.

    `DEFAULT_CONFIG_TOML` stays intentionally simple for tests and older
    callers that format only repo/dag. The CLI uses this helper so profile
    defaults are emitted directly instead of patched in by string replace.
    """
    profile_def = profile if isinstance(profile, ProjectProfile) else get_profile(str(profile))
    resources = profile_def.resource_defaults
    return f"""\
# quikode config
profile = "{profile_def.name}"
repo_path = "{repo_path}"
dag_path = "{dag_path}"
image_tag = "{profile_def.default_image}"
postgres_enabled = {_toml_bool(profile_def.postgres_enabled)}
postgres_db = "{profile_def.postgres_db}"
postgres_user = "{profile_def.postgres_user}"
postgres_password = "{profile_def.postgres_password}"
postgres_image = "{profile_def.postgres_image}"
database_url = "{profile_def.database_url}"
execution_backend = "docker"
max_parallel = 3
base_branch = "{profile_def.base_branch}"
pr_remote = "origin"
triage_budget_per_phase = 3
local_ci_command = "{profile_def.local_ci_command}"
subtask_check_command = "{profile_def.subtask_check_command}"
pre_commit_runner = "{profile_def.pre_commit_runner}"

[execution]
# Reserved for future remote backends ("ssh-docker", "vm-sandbox").

[resources]
cpu_per_task = {int(resources.get("cpu_per_task", 4))}
mem_per_task_gb = {int(resources.get("mem_per_task_gb", 12))}
host_reserved_cpu = {int(resources.get("host_reserved_cpu", 4))}
host_reserved_mem_gb = {int(resources.get("host_reserved_mem_gb", 16))}
max_parallel_auto = {_toml_bool(bool(resources.get("max_parallel_auto", False)))}

[agents.planner]
cli = "codex"
model = "gpt-5.5"

[agents.doer]
cli = "opencode"
model = "zai-coding-plan/glm-5.1"

[agents.checker]
cli = "codex"
model = "gpt-5.3-codex"

[agents.triage]
cli = "codex"
model = "gpt-5.5"
"""


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"
