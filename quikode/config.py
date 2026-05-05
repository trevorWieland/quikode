"""Configuration loading. Single TOML file at .quikode/config.toml.

Implemented as Pydantic models so the field metadata (bounds, descriptions,
validators) can be re-used by the TUI settings modal: the modal renders
itself from `Config.model_json_schema()` instead of duplicating the field
list. See `docs/design-tui.md` "Implementation prerequisites" for why.
"""

from __future__ import annotations

import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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

    # ----- core paths -----
    repo_path: Path = Field(description="Absolute path to the target git repo.")
    dag_path: Path = Field(description="Absolute path to the DAG json file.")
    image_tag: str = Field(
        default="quikode-tanren-dev:latest",
        min_length=1,
        description="Docker image tag for the dev container.",
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
        description=(
            "Per-subtask doer agent timeout. Median observed time is ~9 min on "
            "tanren R-* nodes; 20-min cap gives 2x headroom and halves the cost "
            "of a hang. Lower for faster fail-fast on flaky agents."
        ),
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
        description=(
            "Hard ceiling on per-subtask doer↔checker attempts. Progress-check "
            "agent normally blocks long before this; this is the absolute backstop."
        ),
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
    subtask_transient_max_retries: int = Field(
        default=5,
        ge=0,
        le=50,
        description=(
            "Hard cap on transient (timeout/container/network) retries that "
            "don't count against the real-failure budget."
        ),
    )
    pre_commit_runner: Literal["auto", "lefthook", "pre-commit", "none"] = Field(
        default="auto",
        description=(
            "Which pre-commit toolchain to invoke before committing per-subtask. "
            "'auto' detects lefthook.yml or .pre-commit-config.yaml; 'none' skips."
        ),
    )
    pre_commit_timeout_s: int = Field(
        default=300,
        ge=10,
        le=3600,
        description=(
            "Hard timeout for the pre-commit hook invocation. A hook that "
            "hangs past this is treated as a real failure (not transient)."
        ),
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
        description=(
            "Whether the review-watcher addresses review-thread comments authored by "
            "bot accounts (e.g. chatgpt-codex-connector). Plain issueComments are always ignored."
        ),
    )
    review_rounds_max: int = Field(
        default=15,
        ge=1,
        le=100,
        description=(
            "Maximum review-response rounds before BLOCKING the task with a "
            "'review rounds exhausted; manual merge/close needed' note. "
            "Without this cap, codex-style reviewers can keep finding nits "
            "indefinitely (R-0002 hit round 10 in one observed run with "
            "$30+ in cycles). The cap is a safety net; the typical task "
            "settles in 1-3 rounds. Set high since the cost is low — the "
            "cap rarely fires, but when it does, it prevents runaway."
        ),
    )
    review_response_extra_slots: int = Field(
        default=1,
        ge=0,
        le=8,
        description=(
            "Additional pool slots reserved for review-response futures over and above "
            "`max_parallel`. Without this, response-cycle work shares pool capacity with "
            "regular task workers — at max-parallel=3 with 3 long-running tasks, review "
            "responses on AWAITING_MERGE PRs queue indefinitely and PRs sit unresolved. "
            "Default 1 lets the daemon dispatch one in-flight review on top of the regular "
            "worker fleet without unbounded resource growth."
        ),
    )
    fixup_max_rounds: int = Field(
        default=3,
        ge=1,
        le=10,
        description=(
            "Maximum number of fixup-decomposition rounds before BLOCKING. Each round "
            "is a fixup planner invocation that emits 1-5 mini-subtasks scoped to fixing "
            "the current failure (final-check, CI, etc); the worker runs them through "
            "the same per-subtask doer/checker/triage loop as the original spec. "
            "Default 3 — most failures converge in 1, occasional second round catches "
            "a cascade, third round is the safety net before declaring the task BLOCKED."
        ),
    )
    preempt_at_subtask_boundary: bool = Field(
        default=False,
        description=(
            "When True, the worker checks at each subtask-completion boundary whether "
            "a higher-priority queued task warrants yielding its slot. Yield = "
            "transition the task back to PENDING with `resume_from_existing_subtasks=1`, "
            "tear down the container, return the slot to the pool. The orchestrator's "
            "next tick picks the higher-priority work; the yielded task gets re-picked "
            "when its priority is highest. Off by default — opt in for max-parallel "
            "configurations where review/stacked-child throughput matters more than "
            "minimizing per-task wall time. Each yield costs ~2-3 min of "
            "container teardown + re-provision overhead."
        ),
    )
    preempt_yield_threshold: int = Field(
        default=200,
        ge=0,
        le=1000,
        description=(
            "Minimum priority delta (queued_max - my_score) required to trigger a "
            "yield at a subtask boundary. Higher = more conservative, less yielding. "
            "Default 200 means: yield only when the queued candidate is meaningfully "
            "more urgent (~40 dependents extra, or a stacked child with a fan-out "
            "boost) — not on every minor priority shift."
        ),
    )
    # ----- v3 polish: auto-merge -----
    auto_merge_when_clean: bool = Field(
        default=False,
        description=(
            "When AWAITING_MERGE PRs are OPEN, MERGEABLE, all checks SUCCESS, and all "
            "review threads resolved, the daemon merges them automatically (squash + "
            "delete-branch). Off by default — opt in for trusted task types."
        ),
    )
    auto_merge_min_age_s: int = Field(
        default=60,
        ge=0,
        le=3600,
        description=(
            "Minimum time the task must have been in AWAITING_MERGE before "
            "auto-merge fires. Gives humans a chance to inspect."
        ),
    )

    # ----- v3 settled-task notifications -----
    notify_settled_channel: Literal["none", "ntfy", "slack", "both"] = Field(
        default="none",
        description=(
            "Channel(s) for 'task settled and ready for review' pings. "
            "'none' (default) is opt-in disabled. 'ntfy' uses ntfy.sh "
            "(zero-auth, free, iOS/Android push apps). 'slack' uses an "
            "incoming-webhook URL. 'both' fires on both for redundancy. "
            "Settled = AWAITING_MERGE + green CI + no unresolved threads "
            "+ no churn for cfg.notify_settled_after_s. Run `quikode "
            "notify-test` after configuring to verify delivery."
        ),
    )
    notify_settled_after_s: int = Field(
        default=1800,
        ge=60,
        le=14400,
        description=(
            "Quiet window (seconds) before pinging that a task is ready "
            "for review. Two clocks must both pass: time since the most "
            "recent commit on the PR branch AND time since the last "
            "review_round increment. Default 30min — short enough to ping "
            "while context is fresh, long enough that a fresh codex cycle "
            "won't get pinged about something that's about to change."
        ),
    )
    notify_ntfy_url: str = Field(
        default="https://ntfy.sh",
        description=(
            "ntfy server base URL. Public ntfy.sh is free + no auth; "
            "self-hosted instances also work. The full URL is built as "
            "`<url>/<topic>`."
        ),
    )
    notify_ntfy_topic: str = Field(
        default="",
        description=(
            "ntfy topic name. Effectively a shared secret — anyone who "
            "knows the topic can post + subscribe. Use a long random "
            "string (e.g. `quikode-tanren-7f3a8b9c2d`)."
        ),
    )
    notify_slack_webhook_url: str = Field(
        default="",
        description=(
            "Slack incoming-webhook URL. Create one at "
            "https://api.slack.com/messaging/webhooks and paste the "
            "result here. Empty means no Slack delivery."
        ),
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
        description=(
            "Maximum depth of a stack before forcing children to wait. "
            "Default raised from 4 to 6 to accommodate tanren chains 3+ deep."
        ),
    )
    stacking_max_breadth_per_root: int = Field(
        default=12,
        ge=1,
        le=200,
        description=(
            "Defensive cap: max children stacked off a single grandparent "
            "transitively. Catches planning bugs where a single root spawns "
            "a fan-out that should have been multiple roots."
        ),
    )
    stacking_auto_rebase_on_parent_merge: bool = Field(
        default=True,
        description="Auto-rebase a stacked child off the new base branch when its parent merges.",
    )
    stacking_readiness: Literal["speculative", "settled"] = Field(
        default="speculative",
        description=(
            "When a parent's branch becomes 'stack-ready' for children. "
            "'speculative' (default): parent's branch exists on origin in any "
            "PR-bearing state — children can fork the moment parent opens its "
            "PR. Maximum throughput, but every parent fixup commit forces a "
            "child rebase. 'settled': parent must be in AWAITING_MERGE quietly "
            "for `stack_settle_quiet_s` (no recent CI-fix or review-response "
            "churn). Lower throughput, dramatically less rebase churn — useful "
            "when codex auto-reviews drive many fixup rounds (R-0002 hit 11+) "
            "and each round would otherwise re-rebase every child."
        ),
    )
    stack_settle_quiet_s: int = Field(
        default=600,
        ge=0,
        le=3600,
        description=(
            "When stacking_readiness='settled': minimum quiet time in "
            "AWAITING_MERGE before a parent qualifies as a stack base. "
            "Resets whenever the daemon dispatches a CI-fix or review-response "
            "(parent transitions out and back in). 0 collapses 'settled' to "
            "'reached AWAITING_MERGE once', skipping the quiet check."
        ),
    )
    rebase_coalesce_window_s: int = Field(
        default=30,
        ge=0,
        le=600,
        description=(
            "When a rebase is scheduled for a task, additional rebase triggers "
            "for the same task within this window are coalesced (skipped). The "
            "first rebase's post-completion tick will surface any genuinely new "
            "conflict and trigger a fresh rebase. Set to 0 to disable coalescing."
        ),
    )

    # ----- pre-PR pipeline: local CI gate + 3-stage audit -----
    local_ci_command: str = Field(
        default="just ci",
        description=(
            "Shell command run inside the dev container as the local-CI gate. "
            "Default 'just ci' matches the tanren / fixture convention. "
            "Output is parsed via `triage.parse_ci_failure` for structured "
            "findings. Empty string disables the local-CI step."
        ),
    )
    local_ci_timeout_s: int = Field(
        default=1800,
        ge=60,
        le=7200,
        description=(
            "Timeout for the local-CI command. tanren's full `just ci` runs "
            "~25min on a clean cache; 30min default leaves headroom."
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
        description=(
            "Categories the rubric agent rates 1-10. Default covers the "
            "operator-cited concerns; add or remove via TOML override."
        ),
    )
    pre_pr_rubric_min_score: int = Field(
        default=7,
        ge=1,
        le=10,
        description=(
            "Minimum score in EVERY rubric category for the audit to pass. "
            "Anything below this fails the gate, routing the findings into "
            "the triage merge bundle."
        ),
    )
    pre_pr_standards_profile_globs: list[str] = Field(
        default_factory=lambda: [
            "docs/standards/**/*.md",
            "docs/architecture/**/*.md",
            "AGENTS.md",
            "CONTRIBUTING.md",
        ],
        description=(
            "Glob patterns (relative to repo_path) the standards-audit agent "
            "reads as the canonical repo standards. The agent compares the "
            "branch's diff against these and flags non-alignment."
        ),
    )
    pre_pr_audit_max_cycles: int = Field(
        default=3,
        ge=1,
        le=10,
        description=(
            "How many full pipeline cycles (CI + 3 audits → triage → fixup → "
            "subtask loop → re-run pipeline) before BLOCKing. Each cycle is "
            "expensive; 3 is enough that genuine fixes converge while real "
            "stuck-points get surfaced for human review."
        ),
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
        description=(
            "Heartbeat freshness threshold. The supervisor and TUI consider "
            "the orchestrator alive but stale if the heartbeat is older than this."
        ),
    )
    daemon_min_run_for_backoff_reset_s: int = Field(
        default=300,
        ge=30,
        le=3600,
        description=(
            "If the inner `quikode run` ran for at least this long before crashing, "
            "the supervisor treats it as 'not in a tight crash loop' and resets the "
            "exponential-backoff schedule to its first entry."
        ),
    )
    daemon_backoff_schedule_s: list[int] = Field(
        default_factory=lambda: [60, 300, 1800],
        description=(
            "Exponential backoff schedule (seconds) the supervisor uses between "
            "crash-restarts. The last value is the cap (kept on subsequent crashes)."
        ),
    )
    daemon_heartbeat_stale_kill_s: int = Field(
        default=600,
        ge=60,
        le=3600,
        description=(
            "If the orchestrator's heartbeat is older than this for two consecutive "
            "polls (worker hung, not crashed), the supervisor SIGTERMs it so the "
            "normal crash-restart path can recover. Set well above the longest "
            "expected legitimate stall (subtask doer timeouts, big rebases). 0 disables."
        ),
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
        description=(
            "Checks intent drift after dep merges. Lightweight verdict role "
            "— gpt-5.4-mini handles the 'are these two specs still compatible' "
            "judgment quickly and cheaply."
        ),
    )
    progress: AgentRole = Field(
        default_factory=lambda: AgentRole(cli=AgentCli.CODEX, model="gpt-5.4-mini"),
        description=(
            "Progress-check agent that decides whether a struggling subtask is "
            "making progress, has flatlined, or it's too early to tell."
        ),
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
        # Accept legacy plain strings from older configs.
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

    def _agent(d: dict | None, default: AgentRole) -> AgentRole:
        if not d:
            return default
        return AgentRole(
            cli=AgentCli(d.get("cli", default.cli.value)),
            model=d.get("model", default.model),
            extra_args=list(d.get("extra_args", default.extra_args)),
        )

    def _path(s: str | None, default: Path) -> Path:
        if not s:
            return default
        p = Path(s).expanduser()
        return p if p.is_absolute() else (root / p).resolve()

    agents = raw.get("agents", {})
    resources = raw.get("resources", {})
    conflicts = raw.get("conflicts", {})
    intent = raw.get("intent", {})
    stacking = raw.get("stacking", {})
    daemon = raw.get("daemon", {})
    defaults = Config(repo_path=root, dag_path=root)
    return Config(
        repo_path=_path(raw["repo_path"], root),
        dag_path=_path(raw["dag_path"], root),
        image_tag=raw.get("image_tag", defaults.image_tag),
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
        respond_to_bot_reviews=bool(raw.get("respond_to_bot_reviews", defaults.respond_to_bot_reviews)),
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
        auto_merge_min_age_s=int(raw.get("auto_merge_min_age_s", defaults.auto_merge_min_age_s)),
        notify_settled_channel=raw.get("notify_settled_channel", defaults.notify_settled_channel),
        notify_settled_after_s=int(raw.get("notify_settled_after_s", defaults.notify_settled_after_s)),
        notify_ntfy_url=raw.get("notify_ntfy_url", defaults.notify_ntfy_url),
        notify_ntfy_topic=raw.get("notify_ntfy_topic", defaults.notify_ntfy_topic),
        notify_slack_webhook_url=raw.get("notify_slack_webhook_url", defaults.notify_slack_webhook_url),
        cpu_per_task=int(resources.get("cpu_per_task", defaults.cpu_per_task)),
        mem_per_task_gb=int(resources.get("mem_per_task_gb", defaults.mem_per_task_gb)),
        host_reserved_cpu=int(resources.get("host_reserved_cpu", defaults.host_reserved_cpu)),
        host_reserved_mem_gb=int(resources.get("host_reserved_mem_gb", defaults.host_reserved_mem_gb)),
        max_parallel_auto=bool(resources.get("max_parallel_auto", defaults.max_parallel_auto)),
        container_stats_sample_seconds=int(
            resources.get("container_stats_sample_seconds", defaults.container_stats_sample_seconds)
        ),
        conflict_auto_resolve=bool(conflicts.get("auto_resolve", defaults.conflict_auto_resolve)),
        conflict_max_resolve_attempts=int(
            conflicts.get("max_resolve_attempts", defaults.conflict_max_resolve_attempts)
        ),
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
        stacking_readiness=str(stacking.get("readiness", defaults.stacking_readiness)),  # type: ignore[arg-type]
        stack_settle_quiet_s=int(stacking.get("settle_quiet_s", defaults.stack_settle_quiet_s)),
        rebase_coalesce_window_s=int(
            stacking.get("rebase_coalesce_window_s", defaults.rebase_coalesce_window_s)
        ),
        local_ci_command=str(raw.get("local_ci_command", defaults.local_ci_command)),
        local_ci_timeout_s=int(raw.get("local_ci_timeout_s", defaults.local_ci_timeout_s)),
        pre_pr_rubric_categories=list(raw.get("pre_pr_rubric_categories", defaults.pre_pr_rubric_categories)),
        pre_pr_rubric_min_score=int(raw.get("pre_pr_rubric_min_score", defaults.pre_pr_rubric_min_score)),
        pre_pr_standards_profile_globs=list(
            raw.get("pre_pr_standards_profile_globs", defaults.pre_pr_standards_profile_globs)
        ),
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
        planner=_agent(agents.get("planner"), defaults.planner),
        doer=_agent(agents.get("doer"), defaults.doer),
        checker=_agent(agents.get("checker"), defaults.checker),
        triage=_agent(agents.get("triage"), defaults.triage),
        conflict_resolver=_agent(agents.get("conflict_resolver"), defaults.conflict_resolver),
        intent_reviewer=_agent(agents.get("intent_reviewer"), defaults.intent_reviewer),
        progress=_agent(agents.get("progress"), defaults.progress),
        claude_auth_dir=_path(raw.get("claude_auth_dir"), defaults.claude_auth_dir),
        claude_json_path=_path(raw.get("claude_json_path"), defaults.claude_json_path),
        codex_auth_dir=_path(raw.get("codex_auth_dir"), defaults.codex_auth_dir),
        opencode_auth_dir=_path(raw.get("opencode_auth_dir"), defaults.opencode_auth_dir),
        opencode_config_dir=_path(raw.get("opencode_config_dir"), defaults.opencode_config_dir),
        github_token_env=raw.get("github_token_env", defaults.github_token_env),
    )


DEFAULT_CONFIG_TOML = """\
# quikode config
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
