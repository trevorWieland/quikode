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


class StackingStrategy(StrEnum):
    """Phase C stacking — how aggressively child PRs stack on uncommitted parents."""

    OFF = "off"
    WITHIN_MILESTONE = "within-milestone"
    AGGRESSIVE = "aggressive"


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
        default=1800,
        ge=60,
        le=14400,
        description=(
            "Per-subtask doer agent timeout. Plan 33 calibration (after the "
            "tanren deploy where 7 consecutive opencode/glm-5.1 doer calls "
            "rc=124'd at duration_s ~= 1314s, hitting the prior 1200s "
            "ceiling): bumped to 1800s (30 min). The doer prompt's targeted "
            "rubric / standards / architecture context makes the call "
            "meaningfully heavier than the pre-Plan-33 shape, and smaller "
            "models need the headroom to land both the diff and the "
            "DoerEnvelope JSON before SIGTERM."
        ),
    )
    subtask_checker_timeout_s: int = Field(
        default=900,
        ge=30,
        le=3600,
        description=(
            "Per-subtask checker agent timeout. Plan 33 calibration: bumped "
            "from 600s to 900s alongside the doer bump — the targeted "
            "EvaluationContract (rubric grading template + standards refs) "
            "makes the checker's reasoning surface bigger too, and we want "
            "the proportional headroom so the checker doesn't false-fail "
            "doer work that just barely fit in the new doer ceiling."
        ),
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
        description="How often the daemon polls open PRs for CI status + formal Reviews.",
    )
    review_rounds_max: int = Field(
        default=15,
        ge=1,
        le=100,
        description="Plan 28: counts CHANGES_REQUESTED rounds. Block when exhausted.",
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
        default=1800,
        ge=120,
        le=3600,
        description=(
            "Per-invocation timeout for the fixup planner. Plan 33 "
            "calibration (after the tanren deploy doer-timeout incident): "
            "bumped from 1200s to 1800s. The fixup-planner now renders the "
            "full EvaluationContract (planner-equivalent prompt) in addition "
            "to the audit-bundle decomposition; the planner-equivalent "
            "prompt growth + structured JSON output for multi-finding "
            "decomposition needs the same headroom as the doer."
        ),
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
    # ----- v3 polish: auto-merge (plan 28: triggered by APPROVED review) -----
    auto_merge_when_clean: bool = Field(
        default=False,
        description="Squash-merge automatically when a non-bot APPROVED review lands and the PR is clean.",
    )

    # ----- plan 30: unified review-ready signal -----
    # Threshold serves two purposes: (1) ntfy notification fires once per
    # settled period; (2) stacked-diff dependents become eligible (in
    # `stacking_readiness="settled"` mode). One threshold, two consumers.
    review_ready_settle_s: int = Field(
        default=900,
        ge=0,
        le=14400,
        description=(
            "Seconds in AWAITING_REVIEW before the review-ready-settled signal fires. "
            "Triggers ntfy notification AND unblocks stacked-diff dependents in "
            "stacking_readiness='settled' mode."
        ),
    )
    notify_ntfy_url: str = Field(
        default="https://ntfy.sh",
        description="ntfy server base URL.",
    )
    notify_ntfy_topic: str = Field(
        default="",
        description="ntfy topic. Empty = no review-ready notifications fire.",
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

    # ----- v2 Phase A: conflicts (plan 31 cleanup) -----
    conflict_auto_resolve: bool = Field(
        default=True,
        description="Run the conflict-resolver agent automatically on rebase conflict.",
    )
    conflict_resolver_max_iterations: int = Field(
        default=6,
        ge=1,
        le=20,
        description=(
            "Max conflict-resolver iterations within ONE rebase attempt before "
            "aborting + BLOCK. Each iteration runs the resolver agent on the "
            "current conflict markers and continues `git rebase --continue`."
        ),
    )
    rebase_max_attempts: int = Field(
        default=2,
        ge=1,
        le=10,
        description=(
            "Max OUTER rebase attempts (each containing up to "
            "`conflict_resolver_max_iterations` resolver iterations) before "
            "BLOCKING. Plan 31 split this from the resolver-iteration cap; "
            "they were the same knob pre-plan-31, which made the budget gate "
            "ambiguous."
        ),
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
    subtask_witness_timeout_seconds: int = Field(
        default=15,
        ge=1,
        le=600,
        description=(
            "Plan 33 §7.2: per-witness wall-clock cap for the per-subtask "
            "scoped witness runner. Per-subtask total budget is derived as "
            "`2 * len(behavior_evidence_advanced) * subtask_witness_timeout_seconds`. "
            "Default 15s suits unit-shaped witnesses; bump for BDD-heavy suites."
        ),
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
    # plan 35: standards profile + architecture-doc roots
    standards_profiles_dir: Path = Field(Path("profiles"), description="Standards profile root.")
    standards_profiles: list[str] = Field(default_factory=list, description="Profile names that apply.")
    architecture_docs_dir: Path = Field(Path("docs/architecture"), description="Architecture-docs root.")
    architecture_doc_globs: list[str] = Field(default_factory=lambda: ["**/*.md"], description="Doc globs.")
    architecture_path_map: dict[str, str] = Field(default_factory=dict, description="Path→doc map (PR-B).")
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

    # ----- plan 38 PR-A: role → MODEL bindings + per-role timeouts -----
    # Per-role model resolved via `quikode.model_registry.MODELS`. CLI derived
    # from the model — no `<role>_cli` knob. Defaults MUST match
    # `agent_registry.ROLES[*].default_model`.
    planner_model: str = Field(default="gpt-5.5")
    subtask_doer_model: str = Field(default="GLM-5.1-zai")
    subtask_checker_model: str = Field(default="gpt-5.5")
    subtask_triage_model: str = Field(default="gpt-5.5")
    pre_pr_rubric_model: str = Field(default="gpt-5.5")
    pre_pr_standards_model: str = Field(default="gpt-5.5")
    pre_pr_architecture_model: str = Field(default="gpt-5.5")
    pre_pr_behavior_model: str = Field(default="gpt-5.5")
    fixup_planner_model: str = Field(default="gpt-5.5")
    merge_planner_model: str = Field(default="gpt-5.5")
    conflict_resolver_model: str = Field(default="GLM-5.1-zai")
    progress_model: str = Field(default="gpt-5.5")
    # Per-role timeouts not already declared above (registry `timeout_s_field`).
    planner_timeout_s: int = Field(default=1200, ge=60, le=14400)
    subtask_triage_timeout_s: int = Field(default=600, ge=30, le=3600)
    merge_planner_timeout_s: int = Field(default=1800, ge=120, le=7200)
    conflict_resolver_timeout_s: int = Field(default=1800, ge=60, le=14400)
    progress_timeout_s: int = Field(default=180, ge=30, le=1800)
    # Plan 38 PR-B.7: intent reviewer + replan planner roles migrated off
    # the retired `cfg.<role>: AgentRole` accessors onto the JsonAgent layer.
    intent_reviewer_model: str = Field(default="gpt-5.5")
    intent_reviewer_timeout_s: int = Field(default=600, ge=60, le=3600)
    replan_planner_model: str = Field(default="gpt-5.5")
    replan_planner_timeout_s: int = Field(default=1800, ge=60, le=14400)

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
    "Config",
    "StackingStrategy",
]
