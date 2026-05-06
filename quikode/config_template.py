"""Default configuration template."""

from __future__ import annotations

DEFAULT_CONFIG_TOML = """\
# quikode config
profile = "tanren"
repo_path = "{repo_path}"
dag_path = "{dag_path}"
image_tag = "quikode-tanren-dev:latest"
max_parallel = 3
base_branch = "main"
pr_remote = "origin"
triage_budget_per_phase = 3

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
