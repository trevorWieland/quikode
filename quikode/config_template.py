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
pre_pr_standards_max_medium_findings = 1
pre_pr_standards_max_high_findings = 0
pre_pr_standards_max_critical_findings = 0
pre_pr_architecture_max_medium_findings = 1
pre_pr_architecture_max_high_findings = 0
pre_pr_architecture_max_critical_findings = 0
pre_pr_audit_output_retries = 5
pre_pr_release_valve_after_cycles = 5
pre_pr_release_valve_defer_stages = ["rubric", "standards", "architecture"]
pre_pr_release_valve_max_critical_findings = 0

[execution]
# Reserved for future remote backends ("ssh-docker", "vm-sandbox").

# Plan 38 PR-B.7: per-role MODEL bindings. The CLI is derived from the
# model via quikode.model_registry — no [agents.<phase>] sections.
planner_model = "gpt-5.5"
subtask_doer_model = "GLM-5.1-zai"
subtask_checker_model = "gpt-5.5"
subtask_triage_model = "gpt-5.5"
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
standards_profiles_dir = "profiles"
standards_profiles = ["rust-cargo"]
architecture_docs_dir = "docs/architecture"
architecture_doc_globs = ["**/*.md"]
pre_pr_standards_max_medium_findings = 1
pre_pr_standards_max_high_findings = 0
pre_pr_standards_max_critical_findings = 0
pre_pr_architecture_max_medium_findings = 1
pre_pr_architecture_max_high_findings = 0
pre_pr_architecture_max_critical_findings = 0
pre_pr_audit_output_retries = 5
pre_pr_release_valve_after_cycles = 5
pre_pr_release_valve_defer_stages = ["rubric", "standards", "architecture"]
pre_pr_release_valve_max_critical_findings = 0
playwright_cache_dir = "~/.cache/ms-playwright"

[execution]
# Reserved for future remote backends ("ssh-docker", "vm-sandbox").

[resources]
cpu_per_task = {int(resources.get("cpu_per_task", 4))}
mem_per_task_gb = {int(resources.get("mem_per_task_gb", 12))}
host_reserved_cpu = {int(resources.get("host_reserved_cpu", 4))}
host_reserved_mem_gb = {int(resources.get("host_reserved_mem_gb", 16))}
max_parallel_auto = {_toml_bool(bool(resources.get("max_parallel_auto", False)))}

# Plan 38 PR-B.7: per-role MODEL bindings. The CLI is derived from the
# model via quikode.model_registry — no [agents.<phase>] sections.
planner_model = "gpt-5.5"
subtask_doer_model = "GLM-5.1-zai"
subtask_checker_model = "gpt-5.5"
subtask_triage_model = "gpt-5.5"
"""


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"
