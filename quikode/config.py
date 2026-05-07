"""Configuration loading. Single TOML file at .quikode/config.toml.

Implemented as Pydantic models so the field metadata (bounds, descriptions,
validators) can be re-used by the TUI settings modal: the modal renders
itself from `Config.model_json_schema()` instead of duplicating the field
list. See `docs/design-tui.md` "Implementation prerequisites" for why.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .profiles import ProfileName


class AgentCli(StrEnum):
    """Which agent CLI to invoke."""

    CLAUDE = "claude"
    CODEX = "codex"
    OPENCODE = "opencode"


class StackingStrategy(StrEnum):
    """Phase C stacking — how aggressively child PRs stack on uncommitted parents."""

    OFF = "off"
    WITHIN_MILESTONE = "within-milestone"
    AGGRESSIVE = "aggressive"


class AgentRole(BaseModel):
    """Per-phase agent assignment (which CLI, which model)."""

    model_config = ConfigDict(extra="forbid")

    cli: AgentCli = Field(description="Which agent CLI to invoke for this phase.")
    model: str | None = Field(
        default=None,
        description="Model id passed to the CLI's --model flag. None = CLI default.",
    )
    extra_args: list[str] = Field(
        default_factory=list,
        description="Extra args appended verbatim to the CLI invocation.",
    )


class Config(BaseModel):
    """Per-workspace quikode configuration. Loaded from .quikode/config.toml.

    Numbers carry `ge=`/`le=` bounds enforced by Pydantic — invalid edits from
    the TUI settings modal show inline errors before the value is persisted.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        schema = super().model_json_schema(*args, **kwargs)
        props = schema.get("properties", {})
        if isinstance(props, dict):
            for name, prop in props.items():
                if isinstance(prop, dict) and not prop.get("description"):
                    prop["description"] = name.replace("_", " ")
        return schema

    # ----- core paths -----
    profile: ProfileName = Field(
        default="tanren",
        description="Built-in project profile used for defaults and domain prompt context.",
    )
    repo_path: Path = Field(description="Absolute path to the target git repo.")
    dag_path: Path = Field(description="Absolute path to the DAG json file.")
    image_tag: str = Field(
        default="quikode-tanren-dev:latest",
        min_length=1,
        description="Docker image tag for the dev container.",
    )
    postgres_enabled: bool = Field(
        default=True,
        description="Start a per-task Postgres sidecar for local docker execution.",
    )
    postgres_db: str = Field(default="tanren", min_length=1)
    postgres_user: str = Field(default="postgres", min_length=1)
    postgres_password: str = Field(default="dev", min_length=1)
    postgres_image: str = Field(default="postgres:16-alpine", min_length=1)
    database_url: str = Field(
        default="postgres://postgres:dev@postgres:5432/tanren",
        description="DATABASE_URL injected into task containers. Empty string disables injection.",
    )
    execution_backend: Literal["docker", "fake"] = Field(
        default="docker",
        description=(
            "Execution backend for task sandboxes. Phase 2 supports 'docker' "
            "and test-only 'fake'; future documented values are 'ssh-docker' "
            "and 'vm-sandbox'."
        ),
    )
    execution: dict[str, Any] = Field(
        default_factory=dict,
        description="Reserved execution-backend settings for future remote backends.",
    )

    # ----- orchestration -----
    max_parallel: int = Field(
        default=3,
        ge=1,
        le=32,
        description="Max in-flight tasks. Override with --max-parallel or auto-compute.",
    )
    base_branch: str = Field(
        default="main",
        min_length=1,
        description="Branch task PRs target (and stacking falls back to).",
    )
    pr_remote: str = Field(default="origin", min_length=1, description="git remote for push + PRs.")
    triage_budget_per_phase: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Max doer↔checker cycles per phase before BLOCKED.",
    )
    stall_warn_seconds: int = Field(
        default=1800,
        ge=60,
        le=14400,
        description="Warn if an active task's worktree is quiet this many seconds.",
    )

    # ----- v2 Phase 0: subtasks -----
    subtask_doer_timeout_s: int = Field(
        default=1200,
        ge=60,
        le=14400,
    )
    subtask_checker_timeout_s: int = Field(
        default=600,
        ge=30,
        le=3600,
        description="Per-subtask checker agent timeout.",
    )

    # ----- v3 Phase A: progress-driven retry overhaul -----
    subtask_hard_max_attempts: int = Field(
        default=30,
        ge=1,
        le=200,
    )
    subtask_progress_check_after: int = Field(
        default=4,
        ge=1,
        le=50,
        description="Attempt count at which the progress-check agent first runs.",
    )
    subtask_progress_check_every: int = Field(
        default=3,
        ge=1,
        le=20,
        description="After the first check, re-run the progress-check agent every N attempts.",
    )
    subtask_flatline_block_count: int = Field(
        default=2,
        ge=1,
        le=10,
        description="Consecutive FLATLINED progress verdicts before BLOCKING the subtask.",
    )
    subtask_same_signature_block_count: int = Field(
        default=5,
        ge=2,
        le=20,
        description=(
            "If the last N non-transient retry_reasons share the same "
            "(category, signature) tuple, BLOCK the subtask. Independent of "
            "the progress-check verdict — catches deadlocks where each "
            "attempt produces different-but-equally-invalid output that "
            "the progress-check agent rates 'progressing'. Plan 23."
        ),
    )
    subtask_transient_max_retries: int = Field(
        default=5,
        ge=0,
        le=50,
    )
    pre_commit_runner: Literal["auto", "lefthook", "pre-commit", "none"] = Field(
        default="auto",
    )
    pre_commit_timeout_s: int = Field(
        default=300,
        ge=10,
        le=3600,
    )

    # ----- v3 Phase B: continuous review loop -----
    review_poll_interval_s: int = Field(
        default=60,
        ge=10,
        le=3600,
        description="How often the daemon polls open PRs for new review threads.",
    )
    respond_to_bot_reviews: bool = Field(
        default=True,
    )
    review_rounds_max: int = Field(
        default=15,
        ge=1,
        le=100,
    )
    review_response_extra_slots: int = Field(
        default=1,
        ge=0,
        le=8,
    )
    fixup_max_rounds: int = Field(
        default=3,
        ge=1,
        le=10,
    )
    fixup_planner_timeout_s: int = Field(
        default=1200,
        ge=120,
        le=3600,
        description="Per-invocation timeout for the fixup planner. 20m absorbs the audit-bundle decomposition path (large structured JSON output).",
    )
    fixup_planner_retries_on_transient: int = Field(
        default=2,
        ge=0,
        le=5,
        description="Free retries when the fixup planner returns rc=124 (timeout / transient container failure). Doesn't burn `fixup_max_rounds`.",
    )
    preempt_at_subtask_boundary: bool = Field(
        default=False,
    )
    preempt_yield_threshold: int = Field(
        default=200,
        ge=0,
        le=1000,
    )
    # ----- v3 polish: auto-merge -----
    auto_merge_when_clean: bool = Field(
        default=False,
    )
    auto_merge_min_age_s: int = Field(
        default=60,
        ge=0,
        le=3600,
    )

    # ----- v3 settled-task notifications -----
    notify_settled_channel: Literal["none", "ntfy", "slack", "both"] = Field(
        default="none",
    )
    notify_settled_after_s: int = Field(
        default=1800,
        ge=60,
        le=14400,
    )
    notify_ntfy_url: str = Field(
        default="https://ntfy.sh",
    )
    notify_ntfy_topic: str = Field(
        default="",
    )
    notify_slack_webhook_url: str = Field(
        default="",
    )

    # ----- v2 Resources -----
    cpu_per_task: int = Field(
        default=4,
        ge=1,
        le=128,
        description="docker --cpus per task container.",
    )
    mem_per_task_gb: int = Field(
        default=12,
        ge=1,
        le=512,
        description="docker --memory (and --memory-swap) per container, in GB.",
    )
    host_reserved_cpu: int = Field(
        default=4,
        ge=0,
        le=128,
        description="CPUs to reserve for the host (orchestrator, sccache, agents).",
    )
    host_reserved_mem_gb: int = Field(
        default=16,
        ge=1,
        le=512,
        description="Memory to reserve for the host, in GB.",
    )
    max_parallel_auto: bool = Field(
        default=False,
        description="Compute max_parallel from host headroom on `run` startup.",
    )
    container_stats_sample_seconds: int = Field(
        default=30,
        ge=5,
        le=600,
        description="How often the orchestrator polls docker stats for in-flight containers.",
    )

    # ----- v2 Phase A: conflicts -----
    conflict_auto_resolve: bool = Field(
        default=True,
        description="Run the conflict-resolver agent automatically on rebase conflict.",
    )
    conflict_max_resolve_attempts: int = Field(
        default=2,
        ge=0,
        le=10,
        description="Max conflict-resolver retries before marking BLOCKED for human resolution.",
    )

    # ----- intent gap detection -----
    intent_max_reviews_per_task: int = Field(
        default=5,
        ge=0,
        le=50,
        description="Cap the intent-reviewer agent invocations per task.",
    )
    intent_max_replans: int = Field(
        default=2,
        ge=0,
        le=10,
        description="Cap how many times we replan a task before BLOCKED.",
    )

    # ----- v2 Phase C: stacking -----
    stacking_strategy: StackingStrategy = Field(
        default=StackingStrategy.OFF,
        description="When children may stack PRs on un-merged parent PRs.",
    )
    stacking_max_depth: int = Field(
        default=6,
        ge=1,
        le=20,
    )
    stacking_max_breadth_per_root: int = Field(
        default=12,
        ge=1,
        le=200,
    )
    stacking_auto_rebase_on_parent_merge: bool = Field(
        default=True,
        description="Auto-rebase a stacked child off the new base branch when its parent merges.",
    )
    stacking_readiness: Literal["speculative", "settled"] = Field(
        default="speculative",
    )
    stack_settle_quiet_s: int = Field(
        default=600,
        ge=0,
        le=3600,
    )
    rebase_coalesce_window_s: int = Field(
        default=30,
        ge=0,
        le=600,
    )

    # ----- pre-PR pipeline: local CI gate + 3-stage audit -----
    local_ci_command: str = Field(
        default="just ci",
    )
    local_ci_timeout_s: int = Field(
        default=1800,
        ge=60,
        le=7200,
    )
    subtask_check_command: str = Field(
        default="just check",
        description="Layer-1 objective gate, runs before the LLM checker on every subtask. Empty string disables.",
    )
    subtask_check_timeout_s: int = Field(
        default=300,
        ge=10,
        le=1800,
        description="Timeout for the per-subtask check command.",
    )
    pre_pr_rubric_categories: list[str] = Field(
        default_factory=lambda: [
            "security",
            "scalability",
            "maintainability",
            "extensibility",
            "performance",
            "type_strictness",
        ],
    )
    pre_pr_rubric_min_score: int = Field(
        default=7,
        ge=1,
        le=10,
    )
    pre_pr_standards_profile_globs: list[str] = Field(
        default_factory=lambda: [
            "docs/standards/**/*.md",
            "docs/architecture/**/*.md",
            "AGENTS.md",
            "CONTRIBUTING.md",
        ],
    )
    pre_pr_audit_max_cycles: int = Field(
        default=10,
        ge=1,
        le=20,
        description="Pipeline cycles (CI + 3 audits → triage → fixup → subtask loop → re-run) before BLOCKing.",
    )
    pre_pr_audit_timeout_s: int = Field(
        default=1200,
        ge=60,
        le=3600,
        description="Per-audit-agent timeout. Each of rubric / standards / behavior runs once per cycle.",
    )

    # ----- v3 Phase C: daemon supervisor -----
    daemon_heartbeat_staleness_s: int = Field(
        default=30,
        ge=5,
        le=600,
    )
    daemon_min_run_for_backoff_reset_s: int = Field(
        default=300,
        ge=30,
        le=3600,
    )
    daemon_backoff_schedule_s: list[int] = Field(
        default_factory=lambda: [60, 300, 1800],
    )
    daemon_heartbeat_stale_kill_s: int = Field(
        default=600,
        ge=60,
        le=3600,
    )

    # ----- runtime dirs -----
    state_dir: Path = Field(
        default=Path(".quikode"),
        description="Directory for SQLite db + per-task state.",
    )
    worktree_root: Path = Field(
        default=Path(".quikode/worktrees"),
        description="Root for git worktrees per task.",
    )
    log_dir: Path = Field(
        default=Path(".quikode/logs"),
        description="Per-task and orchestrator log files.",
    )
    prompts_dir: Path = Field(
        default=Path("prompts"),
        description="Custom prompt overrides; missing falls back to bundled.",
    )
    sccache_dir: Path = Field(
        default=Path(".quikode/sccache"),
        description="Shared rust build cache mounted into all containers.",
    )

    # ----- agent role assignments -----
    # Heavy reasoning roles run on codex gpt-5.5; lightweight verdict roles
    # run on codex gpt-5.4-mini. Claude was retired from default config due
    # to subscription token-expiry issues — every claude call risked a 401
    # mid-run, which surfaced as cascading "fixup planner returned empty"
    # BLOCKs across the workspace.
    planner: AgentRole = Field(
        default_factory=lambda: AgentRole(cli=AgentCli.CODEX, model="gpt-5.5"),
        description="Planner agent — emits structured plan JSON.",
    )
    doer: AgentRole = Field(
        default_factory=lambda: AgentRole(cli=AgentCli.OPENCODE, model="zai-coding-plan/glm-5.1"),
        description="Doer agent — implements subtasks.",
    )
    checker: AgentRole = Field(
        default_factory=lambda: AgentRole(cli=AgentCli.CODEX, model="gpt-5.3-codex"),
        description="Checker agent — runs the playbook + acceptance.",
    )
    triage: AgentRole = Field(
        default_factory=lambda: AgentRole(cli=AgentCli.CODEX, model="gpt-5.5"),
        description="Triage agent — root-causes failures.",
    )
    conflict_resolver: AgentRole = Field(
        default_factory=lambda: AgentRole(cli=AgentCli.CODEX, model="gpt-5.5"),
        description="Resolves rebase conflicts via the agent's full reasoning budget.",
    )
    intent_reviewer: AgentRole = Field(
        default_factory=lambda: AgentRole(cli=AgentCli.CODEX, model="gpt-5.4-mini"),
    )
    progress: AgentRole = Field(
        default_factory=lambda: AgentRole(cli=AgentCli.CODEX, model="gpt-5.4-mini"),
    )

    # ----- auth mounts -----
    claude_auth_dir: Path = Field(default_factory=lambda: Path.home() / ".claude")
    claude_json_path: Path = Field(default_factory=lambda: Path.home() / ".claude.json")
    codex_auth_dir: Path = Field(default_factory=lambda: Path.home() / ".codex")
    opencode_auth_dir: Path = Field(default_factory=lambda: Path.home() / ".local/share/opencode")
    opencode_config_dir: Path = Field(default_factory=lambda: Path.home() / ".config/opencode")
    github_token_env: str = Field(
        default="GITHUB_TOKEN",
        min_length=1,
        description="Env var holding a GitHub token if gh CLI is unavailable.",
    )

    # ----- validators -----
    @field_validator("stacking_strategy", mode="before")
    @classmethod
    def _coerce_stacking(cls, v: Any) -> Any:
        # Accept plain strings from older configs.
        if isinstance(v, str) and not isinstance(v, StackingStrategy):
            return StackingStrategy(v)
        return v

    @model_validator(mode="after")
    def _check_resource_consistency(self) -> Config:
        # host_reserved < total available is a property of the host, not of cfg
        # alone, so we can't enforce that here. We can flag obvious nonsense:
        if self.cpu_per_task <= 0:
            raise ValueError("cpu_per_task must be >= 1")
        if self.mem_per_task_gb <= 0:
            raise ValueError("mem_per_task_gb must be >= 1")
        return self


__all__ = [
    "AgentCli",
    "AgentRole",
    "Config",
    "StackingStrategy",
]
