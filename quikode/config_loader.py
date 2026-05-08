"""Workspace config discovery and TOML loading."""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any, Literal, cast

from quikode.config import Config, StackingStrategy
from quikode.profiles import get_profile

log = logging.getLogger("quikode.config_loader")

# Plan 38 PR-C audit-log support: TOML sub-section → list of int knobs
# that ride under it. Used by `_log_int_overrides` so the audit-log
# walks every int field, not just the top-level ones. Keys are keys
# inside the sub-section dict (NOT prefixed); values are the matching
# `Config.model_fields` names.
_TOML_SECTION_INT_KNOBS: dict[str, dict[str, str]] = {
    "resources": {
        "cpu_per_task": "cpu_per_task",
        "mem_per_task_gb": "mem_per_task_gb",
        "host_reserved_cpu": "host_reserved_cpu",
        "host_reserved_mem_gb": "host_reserved_mem_gb",
        "container_stats_sample_seconds": "container_stats_sample_seconds",
    },
    "conflicts": {
        "resolver_max_iterations": "conflict_resolver_max_iterations",
        "rebase_max_attempts": "rebase_max_attempts",
    },
    "intent": {
        "max_reviews_per_task": "intent_max_reviews_per_task",
        "max_replans": "intent_max_replans",
    },
    "stacking": {
        "max_depth": "stacking_max_depth",
        "max_breadth_per_root": "stacking_max_breadth_per_root",
        "rebase_coalesce_window_s": "rebase_coalesce_window_s",
    },
    "daemon": {
        "heartbeat_staleness_s": "daemon_heartbeat_staleness_s",
        "min_run_for_backoff_reset_s": "daemon_min_run_for_backoff_reset_s",
        "heartbeat_stale_kill_s": "daemon_heartbeat_stale_kill_s",
    },
}


def _log_int_overrides(raw: dict[str, Any], defaults: Config) -> None:
    """Plan 38 PR-C audit log: emit one INFO line per int knob the toml
    overrides relative to the Field default.

    The trigger for this audit log is the `subtask_doer_timeout_s`
    drift incident (commit d06cdcd bumped Field default 1200 → 1800,
    but live workspace config.toml's still pinned 1200, capping doer
    calls at the prior ceiling). The drift was invisible because no
    daemon-start log surfaced "your toml is overriding the Field
    default of 1800 with 1200." This loop closes that gap: every
    daemon-start, the operator sees a definitive list of which knobs
    their toml is overriding and what the Field default is — so the
    NEXT bump is visible immediately on a stale-toml workspace.

    Iterates `Config.model_fields` instead of a hand-maintained list
    so new int Fields get the audit treatment automatically.
    """
    for field_name, field_info in Config.model_fields.items():
        if field_info.annotation is not int:
            continue
        # Discover the toml location for this knob.
        raw_value: Any = None
        toml_key: str | None = None
        if field_name in raw:
            raw_value = raw.get(field_name)
            toml_key = field_name
        else:
            for section_name, section_map in _TOML_SECTION_INT_KNOBS.items():
                if field_name in section_map.values():
                    section_dict = raw.get(section_name) or {}
                    if not isinstance(section_dict, dict):
                        continue
                    inner_key = next((k for k, v in section_map.items() if v == field_name), None)
                    if inner_key is not None and inner_key in section_dict:
                        raw_value = section_dict.get(inner_key)
                        toml_key = f"{section_name}.{inner_key}"
                        break
        if raw_value is None or toml_key is None:
            continue
        try:
            override = int(raw_value)
        except (TypeError, ValueError):
            continue
        default_value = getattr(defaults, field_name)
        if not isinstance(default_value, int):  # pragma: no cover — model_fields filtered to int
            continue
        if override == default_value:
            continue
        log.info(
            "config[%s] = %d (overrides Field default %d)",
            toml_key,
            override,
            default_value,
        )


def find_config_root(start: Path | None = None) -> Path:
    """Walk up looking for .quikode/config.toml; default to cwd."""
    cur = (start or Path.cwd()).resolve()
    for parent in [cur, *cur.parents]:
        if (parent / ".quikode" / "config.toml").exists():
            return parent
    return Path.cwd().resolve()


def load_config(root: Path | None = None) -> Config:
    root = (root or find_config_root()).resolve()
    cfg_path = root / ".quikode" / "config.toml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"no quikode config at {cfg_path}; run `quikode init` first")
    raw = tomllib.loads(cfg_path.read_text())

    # Plan 35 hard cutover: retired key (no backcompat shim).
    if "pre_pr_standards_profile_globs" in raw:
        raise ValueError(
            "`pre_pr_standards_profile_globs` is retired (plan 35). "
            "Migrate to `standards_profiles_dir` + `standards_profiles` + "
            "`architecture_docs_dir`. See plans/35-standards-profile-linking.md."
        )

    # Plan 38 PR-B.7 hard cutover: prior `[agents.<phase>]` TOML sections
    # (with `cli` + `model`) are retired. The role layer (`make_agent`)
    # binds roles to MODELS via `cfg.<role>_model`, not CLIs. Reject the
    # prior sections explicitly so a stale config surfaces immediately
    # instead of silently no-op'ing.
    if "agents" in raw:
        raise ValueError(
            "[agents.<phase>] TOML sections are retired (plan 38 PR-B.7). "
            'Migrate to `<role>_model = "<model_name>"` keys; the CLI is '
            "derived from the model via quikode.model_registry. See "
            "plans/38-json-schema-agent-layer.md."
        )

    def _path(s: str | None, default: Path) -> Path:
        if not s:
            return default
        p = Path(s).expanduser()
        return p if p.is_absolute() else (root / p).resolve()

    resources = raw.get("resources", {})
    conflicts = raw.get("conflicts", {})

    # Plan 31 explicit-fail on retired key. The old `max_resolve_attempts`
    # conflated two distinct knobs (inner resolver-iteration cap, outer
    # rebase-attempt cap); plan 31 split them. No silent acceptance.
    if "max_resolve_attempts" in conflicts:
        raise ValueError(
            "[conflicts].max_resolve_attempts is retired (plan 31). Replace with "
            "`resolver_max_iterations` (inner; default 6) and/or "
            "`rebase_max_attempts` (outer; default 2) under [conflicts]."
        )
    intent = raw.get("intent", {})
    stacking = raw.get("stacking", {})
    daemon = raw.get("daemon", {})
    execution = raw.get("execution", {})
    profile = get_profile(raw.get("profile"))
    defaults = Config(
        profile=profile.name,
        repo_path=root,
        dag_path=root,
        image_tag=profile.default_image,
        postgres_enabled=profile.postgres_enabled,
        postgres_db=profile.postgres_db,
        postgres_user=profile.postgres_user,
        postgres_password=profile.postgres_password,
        postgres_image=profile.postgres_image,
        database_url=profile.database_url,
        base_branch=profile.base_branch,
        local_ci_command=profile.local_ci_command,
        subtask_check_command=profile.subtask_check_command,
        pre_commit_runner=profile.pre_commit_runner,
        cpu_per_task=int(profile.resource_defaults.get("cpu_per_task", 4)),
        mem_per_task_gb=int(profile.resource_defaults.get("mem_per_task_gb", 12)),
        host_reserved_cpu=int(profile.resource_defaults.get("host_reserved_cpu", 4)),
        host_reserved_mem_gb=int(profile.resource_defaults.get("host_reserved_mem_gb", 16)),
        max_parallel_auto=bool(profile.resource_defaults.get("max_parallel_auto", False)),
    )
    _log_int_overrides(raw, defaults)
    return Config(
        profile=profile.name,
        repo_path=_path(raw["repo_path"], root),
        dag_path=_path(raw["dag_path"], root),
        image_tag=raw.get("image_tag", defaults.image_tag),
        postgres_enabled=bool(raw.get("postgres_enabled", defaults.postgres_enabled)),
        postgres_db=str(raw.get("postgres_db", defaults.postgres_db)),
        postgres_user=str(raw.get("postgres_user", defaults.postgres_user)),
        postgres_password=str(raw.get("postgres_password", defaults.postgres_password)),
        postgres_image=str(raw.get("postgres_image", defaults.postgres_image)),
        database_url=str(raw.get("database_url", defaults.database_url)),
        execution_backend=raw.get("execution_backend", defaults.execution_backend),
        execution=dict(execution),
        max_parallel=int(raw.get("max_parallel", defaults.max_parallel)),
        base_branch=raw.get("base_branch", defaults.base_branch),
        pr_remote=raw.get("pr_remote", defaults.pr_remote),
        triage_budget_per_phase=int(raw.get("triage_budget_per_phase", defaults.triage_budget_per_phase)),
        stall_warn_seconds=int(raw.get("stall_warn_seconds", defaults.stall_warn_seconds)),
        subtask_doer_timeout_s=int(raw.get("subtask_doer_timeout_s", defaults.subtask_doer_timeout_s)),
        subtask_checker_timeout_s=int(
            raw.get("subtask_checker_timeout_s", defaults.subtask_checker_timeout_s)
        ),
        subtask_hard_max_attempts=int(
            raw.get("subtask_hard_max_attempts", defaults.subtask_hard_max_attempts)
        ),
        subtask_progress_check_after=int(
            raw.get("subtask_progress_check_after", defaults.subtask_progress_check_after)
        ),
        subtask_progress_check_every=int(
            raw.get("subtask_progress_check_every", defaults.subtask_progress_check_every)
        ),
        subtask_flatline_block_count=int(
            raw.get("subtask_flatline_block_count", defaults.subtask_flatline_block_count)
        ),
        subtask_transient_max_retries=int(
            raw.get("subtask_transient_max_retries", defaults.subtask_transient_max_retries)
        ),
        pre_commit_runner=raw.get("pre_commit_runner", defaults.pre_commit_runner),
        pre_commit_timeout_s=int(raw.get("pre_commit_timeout_s", defaults.pre_commit_timeout_s)),
        review_poll_interval_s=int(raw.get("review_poll_interval_s", defaults.review_poll_interval_s)),
        review_response_extra_slots=int(
            raw.get("review_response_extra_slots", defaults.review_response_extra_slots)
        ),
        review_rounds_max=int(raw.get("review_rounds_max", defaults.review_rounds_max)),
        fixup_max_rounds=int(raw.get("fixup_max_rounds", defaults.fixup_max_rounds)),
        preempt_at_subtask_boundary=bool(
            raw.get("preempt_at_subtask_boundary", defaults.preempt_at_subtask_boundary)
        ),
        preempt_yield_threshold=int(raw.get("preempt_yield_threshold", defaults.preempt_yield_threshold)),
        auto_merge_when_clean=bool(raw.get("auto_merge_when_clean", defaults.auto_merge_when_clean)),
        review_ready_settle_s=int(raw.get("review_ready_settle_s", defaults.review_ready_settle_s)),
        notify_ntfy_url=raw.get("notify_ntfy_url", defaults.notify_ntfy_url),
        notify_ntfy_topic=raw.get("notify_ntfy_topic", defaults.notify_ntfy_topic),
        cpu_per_task=int(resources.get("cpu_per_task", defaults.cpu_per_task)),
        mem_per_task_gb=int(resources.get("mem_per_task_gb", defaults.mem_per_task_gb)),
        host_reserved_cpu=int(resources.get("host_reserved_cpu", defaults.host_reserved_cpu)),
        host_reserved_mem_gb=int(resources.get("host_reserved_mem_gb", defaults.host_reserved_mem_gb)),
        max_parallel_auto=bool(resources.get("max_parallel_auto", defaults.max_parallel_auto)),
        container_stats_sample_seconds=int(
            resources.get("container_stats_sample_seconds", defaults.container_stats_sample_seconds)
        ),
        conflict_auto_resolve=bool(conflicts.get("auto_resolve", defaults.conflict_auto_resolve)),
        conflict_resolver_max_iterations=int(
            conflicts.get(
                "resolver_max_iterations",
                defaults.conflict_resolver_max_iterations,
            )
        ),
        rebase_max_attempts=int(conflicts.get("rebase_max_attempts", defaults.rebase_max_attempts)),
        intent_max_reviews_per_task=int(
            intent.get("max_reviews_per_task", defaults.intent_max_reviews_per_task)
        ),
        intent_max_replans=int(intent.get("max_replans", defaults.intent_max_replans)),
        stacking_strategy=StackingStrategy(stacking.get("strategy", defaults.stacking_strategy.value)),
        stacking_max_depth=int(stacking.get("max_depth", defaults.stacking_max_depth)),
        stacking_max_breadth_per_root=int(
            stacking.get("max_breadth_per_root", defaults.stacking_max_breadth_per_root)
        ),
        stacking_auto_rebase_on_parent_merge=bool(
            stacking.get("auto_rebase_on_parent_merge", defaults.stacking_auto_rebase_on_parent_merge)
        ),
        stacking_readiness=cast(
            Literal["speculative", "settled"],
            str(stacking.get("readiness", defaults.stacking_readiness)),
        ),
        rebase_coalesce_window_s=int(
            stacking.get("rebase_coalesce_window_s", defaults.rebase_coalesce_window_s)
        ),
        local_ci_command=str(raw.get("local_ci_command", defaults.local_ci_command)),
        local_ci_timeout_s=int(raw.get("local_ci_timeout_s", defaults.local_ci_timeout_s)),
        subtask_check_command=str(raw.get("subtask_check_command", defaults.subtask_check_command)),
        subtask_check_timeout_s=int(raw.get("subtask_check_timeout_s", defaults.subtask_check_timeout_s)),
        pre_pr_rubric_categories=list(raw.get("pre_pr_rubric_categories", defaults.pre_pr_rubric_categories)),
        pre_pr_rubric_min_score=int(raw.get("pre_pr_rubric_min_score", defaults.pre_pr_rubric_min_score)),
        standards_profiles_dir=_path(
            raw.get("standards_profiles_dir"),
            (root / defaults.standards_profiles_dir).resolve()
            if not defaults.standards_profiles_dir.is_absolute()
            else defaults.standards_profiles_dir,
        ),
        standards_profiles=list(raw.get("standards_profiles", defaults.standards_profiles)),
        architecture_docs_dir=_path(
            raw.get("architecture_docs_dir"),
            (root / defaults.architecture_docs_dir).resolve()
            if not defaults.architecture_docs_dir.is_absolute()
            else defaults.architecture_docs_dir,
        ),
        architecture_doc_globs=list(raw.get("architecture_doc_globs", defaults.architecture_doc_globs)),
        architecture_path_map=dict(raw.get("architecture_path_map", defaults.architecture_path_map)),
        pre_pr_audit_max_cycles=int(raw.get("pre_pr_audit_max_cycles", defaults.pre_pr_audit_max_cycles)),
        pre_pr_audit_timeout_s=int(raw.get("pre_pr_audit_timeout_s", defaults.pre_pr_audit_timeout_s)),
        daemon_heartbeat_staleness_s=int(
            daemon.get("heartbeat_staleness_s", defaults.daemon_heartbeat_staleness_s)
        ),
        daemon_min_run_for_backoff_reset_s=int(
            daemon.get("min_run_for_backoff_reset_s", defaults.daemon_min_run_for_backoff_reset_s)
        ),
        daemon_backoff_schedule_s=list(daemon.get("backoff_schedule_s", defaults.daemon_backoff_schedule_s)),
        daemon_heartbeat_stale_kill_s=int(
            daemon.get("heartbeat_stale_kill_s", defaults.daemon_heartbeat_stale_kill_s)
        ),
        state_dir=_path(raw.get("state_dir"), root / ".quikode"),
        worktree_root=_path(raw.get("worktree_root"), root / ".quikode" / "worktrees"),
        log_dir=_path(raw.get("log_dir"), root / ".quikode" / "logs"),
        prompts_dir=_path(raw.get("prompts_dir"), root / "prompts"),
        sccache_dir=_path(raw.get("sccache_dir"), root / ".quikode" / "sccache"),
        claude_auth_dir=_path(raw.get("claude_auth_dir"), defaults.claude_auth_dir),
        claude_json_path=_path(raw.get("claude_json_path"), defaults.claude_json_path),
        codex_auth_dir=_path(raw.get("codex_auth_dir"), defaults.codex_auth_dir),
        opencode_auth_dir=_path(raw.get("opencode_auth_dir"), defaults.opencode_auth_dir),
        opencode_config_dir=_path(raw.get("opencode_config_dir"), defaults.opencode_config_dir),
        github_token_env=raw.get("github_token_env", defaults.github_token_env),
        # Plan 38 PR-A: role → MODEL bindings + new per-role timeouts.
        planner_model=str(raw.get("planner_model", defaults.planner_model)),
        subtask_doer_model=str(raw.get("subtask_doer_model", defaults.subtask_doer_model)),
        subtask_checker_model=str(raw.get("subtask_checker_model", defaults.subtask_checker_model)),
        subtask_triage_model=str(raw.get("subtask_triage_model", defaults.subtask_triage_model)),
        pre_pr_rubric_model=str(raw.get("pre_pr_rubric_model", defaults.pre_pr_rubric_model)),
        pre_pr_standards_model=str(raw.get("pre_pr_standards_model", defaults.pre_pr_standards_model)),
        pre_pr_behavior_model=str(raw.get("pre_pr_behavior_model", defaults.pre_pr_behavior_model)),
        fixup_planner_model=str(raw.get("fixup_planner_model", defaults.fixup_planner_model)),
        merge_planner_model=str(raw.get("merge_planner_model", defaults.merge_planner_model)),
        conflict_resolver_model=str(raw.get("conflict_resolver_model", defaults.conflict_resolver_model)),
        progress_model=str(raw.get("progress_model", defaults.progress_model)),
        planner_timeout_s=int(raw.get("planner_timeout_s", defaults.planner_timeout_s)),
        subtask_triage_timeout_s=int(raw.get("subtask_triage_timeout_s", defaults.subtask_triage_timeout_s)),
        merge_planner_timeout_s=int(raw.get("merge_planner_timeout_s", defaults.merge_planner_timeout_s)),
        conflict_resolver_timeout_s=int(
            raw.get("conflict_resolver_timeout_s", defaults.conflict_resolver_timeout_s)
        ),
        progress_timeout_s=int(raw.get("progress_timeout_s", defaults.progress_timeout_s)),
        intent_reviewer_model=str(raw.get("intent_reviewer_model", defaults.intent_reviewer_model)),
        intent_reviewer_timeout_s=int(
            raw.get("intent_reviewer_timeout_s", defaults.intent_reviewer_timeout_s)
        ),
        replan_planner_model=str(raw.get("replan_planner_model", defaults.replan_planner_model)),
        replan_planner_timeout_s=int(raw.get("replan_planner_timeout_s", defaults.replan_planner_timeout_s)),
    )
