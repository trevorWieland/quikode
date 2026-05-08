"""Render prompt templates with task context."""

from __future__ import annotations

from pathlib import Path

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, StrictUndefined

from .agent_schemas import DoerEnvelope
from .config import Config
from .dag import DAG, Node
from .evaluation_contract import EvaluationContract
from .subtask_schema import STABILIZATION_SUBTASK_ID, Plan

# Bundled prompts ship inside the quikode package itself (../prompts at the repo root,
# i.e. one level up from the package dir). They serve as the default if the user
# hasn't overridden them in <config-root>/prompts.
_BUNDLED_PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


def _env(prompts_dir: Path) -> Environment:
    loaders = [FileSystemLoader(prompts_dir)] if prompts_dir.exists() else []
    if _BUNDLED_PROMPTS.exists() and prompts_dir != _BUNDLED_PROMPTS:
        loaders.append(FileSystemLoader(_BUNDLED_PROMPTS))
    if not loaders:
        raise FileNotFoundError(f"no prompts found at {prompts_dir} or bundled at {_BUNDLED_PROMPTS}")
    return Environment(
        loader=ChoiceLoader(loaders),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def render(cfg: Config, template: str, **ctx) -> str:
    env = _env(cfg.prompts_dir)
    return env.get_template(template).render(**ctx)


def planner_prompt(
    cfg: Config,
    dag: DAG,
    node: Node,
    contract: EvaluationContract,
    *,
    prior_attempt_notes: str | None = None,
) -> str:
    milestone = dag.milestones.get(node.milestone, {})
    return render(
        cfg,
        "planner.md",
        node=node,
        contract=contract,
        repo_root=str(cfg.repo_path),
        milestone_title=milestone.get("title", ""),
        prior_attempt_notes=prior_attempt_notes,
    )


def doer_prompt(cfg: Config, node: Node, plan: str, triage_notes: str | None = None) -> str:
    return render(cfg, "doer.md", node=node, plan=plan, triage_notes=triage_notes)


def checker_prompt(
    cfg: Config,
    node: Node,
    plan: str,
    ci_result: str,
    ci_failure_excerpt: str | None = None,
    manual_probe_results: str | None = None,
) -> str:
    return render(
        cfg,
        "checker.md",
        node=node,
        plan=plan,
        ci_result=ci_result,
        ci_failure_excerpt=ci_failure_excerpt,
        manual_probe_results=manual_probe_results,
    )


def triage_prompt(
    cfg: Config,
    node: Node,
    plan: str,
    phase: str,
    retry_count: int,
    retry_budget: int,
    checker_output: str | None = None,
    ci_log_excerpt: str | None = None,
    review_comments: list[dict] | None = None,
    recent_doer_summary: str | None = None,
) -> str:
    return render(
        cfg,
        "triage.md",
        node=node,
        plan=plan,
        phase=phase,
        retry_count=retry_count,
        retry_budget=retry_budget,
        checker_output=checker_output,
        ci_log_excerpt=ci_log_excerpt,
        review_comments=review_comments or [],
        recent_doer_summary=recent_doer_summary,
    )


# ----- v2 Phase 0: subtask prompts -----


def subtask_doer_prompt(
    cfg: Config,
    node: Node,
    subtask,
    contract: EvaluationContract,
    *,
    plan: Plan | None = None,
    triage_notes: str | None = None,
    prior_doer_envelope: DoerEnvelope | None = None,
) -> str:
    """Plan 38 PR-B.5: subtask-doer prompt takes the full
    `EvaluationContract` (rendered into §3 via `ec_targeted`) plus an
    optional structured `prior_doer_envelope` carrying the prior
    attempt's bookkeeping (files_touched / witnesses_run / summary).
    The doer emits a `DoerEnvelope` JSON object — bookkeeping only;
    the diff is what's graded. Z-99 still runs the full local-CI gate;
    per-subtask gating uses `cfg.subtask_check_command`."""
    gate_command = (
        cfg.local_ci_command if subtask.id == STABILIZATION_SUBTASK_ID else cfg.subtask_check_command
    )
    return render(
        cfg,
        "subtask-doer.md",
        node=node,
        plan=plan,
        subtask=subtask,
        contract=contract,
        triage_notes=triage_notes,
        prior_doer_envelope=prior_doer_envelope,
        subtask_check_command=gate_command,
    )


def subtask_checker_prompt(
    cfg: Config,
    node: Node,
    subtask,
    contract: EvaluationContract,
    *,
    doer_envelope: DoerEnvelope | None,
    diff_text: str,
    witness_results: dict[str, dict],
) -> str:
    """Plan 38 PR-B.5: checker grades the unified diff against the
    targeted contract. Receives the doer's `DoerEnvelope` as
    informational context (NOT the contract being graded), the diff,
    and the per-subtask scoped witness results."""
    return render(
        cfg,
        "subtask-checker.md",
        node=node,
        subtask=subtask,
        contract=contract,
        doer_envelope=doer_envelope,
        diff_text=diff_text[:20000],
        witness_results=witness_results,
    )


def subtask_triage_prompt(
    cfg: Config,
    node: Node,
    subtask,
    contract: EvaluationContract,
    *,
    doer_envelope: DoerEnvelope | None,
    checker_verdict: str,
    diff_text: str,
) -> str:
    """Plan 38 PR-B.5: senior-engineer-tutoring-junior triage. Inputs
    are the targeted contract slice, the doer envelope (informational),
    the checker's text output, and the unified diff. Output is a
    `SubtaskTriageOutput` JSON instance."""
    return render(
        cfg,
        "subtask-triage.md",
        node=node,
        subtask=subtask,
        contract=contract,
        doer_envelope=doer_envelope,
        checker_verdict=checker_verdict,
        diff_text=diff_text[:20000],
    )


# ----- v3 fixup decomposition: per-failure mini-planner -----


def fixup_planner_prompt(
    cfg: Config,
    node: Node,
    contract: EvaluationContract,
    *,
    kind: str,
    round_no: int,
    max_rounds: int,
    trigger: str,
    original_final_acceptance: list[str],
    done_subtasks: list[dict],
    prior_fixup_subtasks: list[dict],
    checker_output: str | None = None,
    ci_excerpt: str | None = None,
    review_threads_block: str | None = None,
    triage_root_cause: str | None = None,
) -> str:
    """Render the fixup-planner prompt.

    Plan 33 PR-B: takes `contract` so the fixup planner sees the same
    four-stage rubric the spec planner saw (rendered via `ec_full`).
    `addresses_findings` per-subtask is gone (Plan 33 D2) — fixup slices
    declare their gap-closure via the stage-typed fields directly.

    `kind` is one of `fixup-final` / `fixup-ci` / `fixup-review` /
    `fixup-pre-pr-audit`. `trigger` is a human-readable label echoed
    back into the prompt. All failure-context fields are optional —
    pass only what's available for the current trigger.
    """
    return render(
        cfg,
        "fixup-planner.md",
        node=node,
        contract=contract,
        kind=kind,
        round_no=round_no,
        max_rounds=max_rounds,
        trigger=trigger,
        original_final_acceptance=original_final_acceptance,
        done_subtasks=done_subtasks,
        prior_fixup_subtasks=prior_fixup_subtasks,
        checker_output=checker_output,
        ci_excerpt=ci_excerpt,
        review_threads_block=review_threads_block,
        triage_root_cause=triage_root_cause,
    )


# ----- v2 Phase A: conflict resolver -----


def conflict_resolver_prompt(
    cfg: Config,
    node: Node,
    *,
    task_diff_excerpt: str,
    main_log_excerpt: str,
    main_diff_excerpt: str,
    conflicted_files: list[dict],
    rebase_target_kind: str = "main",
    parent_branch: str = "",
    parent_contexts: list[dict] | None = None,
) -> str:
    """Render the conflict-resolver prompt.

    `rebase_target_kind` selects the prompt's framing:
      * `"main"` — the task's PR was rebased onto a fresh main (L1).
      * `"parent_tip"` — task is stacked on a parent whose tip just
        moved (L2). `parent_branch` names the parent.
      * `"merge_node"` — invoked under a merge-node worker integrating
        N parents into one branch (plan 32 PR-B). `parent_contexts`
        supplies per-parent diff context so the resolver can attribute
        conflict regions to specific parents and decide which side
        wins (or honestly GIVE_UP on cross-parent semantic conflicts
        that need the merge-doer-subloop to resolve).
    """
    return render(
        cfg,
        "conflict-resolver.md",
        node=node,
        task_diff_excerpt=task_diff_excerpt[:8000],
        main_log_excerpt=main_log_excerpt[:4000],
        main_diff_excerpt=main_diff_excerpt[:8000],
        conflicted_files=conflicted_files,
        rebase_target_kind=rebase_target_kind,
        parent_branch=parent_branch,
        parent_contexts=parent_contexts or [],
    )


# ----- Plan 32 PR-B: merge-node planner -----


def merge_planner_prompt(
    cfg: Config,
    merge_node: Node,
    parent_contexts: list[dict],
    contract: EvaluationContract,
) -> str:
    """Render the merge-planner prompt for plan 32 PR-B's doer-subloop.

    Plan 33 PR-A: takes `merge_node` (Node) and `contract`
    (EvaluationContract) so the same four-stage rubric the spec planner
    sees flows into merge planning.

    `parent_contexts` is a list of per-parent dicts with keys:
      * `task_id` — source parent's task id (e.g. `R-0042`)
      * `branch` — parent's PR branch
      * `title` — parent's DAG node title (or fallback)
      * `summary` — short intent description (1-2 sentences); empty if
        no DAG node available
      * `diff_excerpt` — parent's diff against `cfg.base_branch`
        truncated to ~3000 chars
    """
    diff_cap = 3000
    capped: list[dict] = []
    for p in parent_contexts:
        capped.append(
            {
                "task_id": p.get("task_id", ""),
                "branch": p.get("branch", ""),
                "title": p.get("title", ""),
                "summary": p.get("summary", ""),
                "diff_excerpt": (p.get("diff_excerpt", "") or "")[:diff_cap],
            }
        )
    return render(
        cfg,
        "merge-planner.md",
        merge_node=merge_node,
        contract=contract,
        parent_contexts=capped,
        base_branch=cfg.base_branch,
        repo_root=str(cfg.repo_path),
        local_ci_command=(cfg.local_ci_command or "just ci"),
    )


# ----- v3 Phase A: progress-check agent -----


def progress_prompt(
    cfg: Config,
    subtask,
    *,
    attempts: list,
    acceptance: tuple[str, ...],
) -> str:
    return render(
        cfg,
        "progress.md",
        subtask=subtask,
        attempts=attempts,
        acceptance=acceptance,
    )


# ----- v2 Phase B: intent reviewer -----


def intent_reviewer_prompt(
    cfg: Config, node: Node, *, task_diff_excerpt: str, main_log_excerpt: str, main_diff_excerpt: str
) -> str:
    return render(
        cfg,
        "intent-reviewer.md",
        node=node,
        task_diff_excerpt=task_diff_excerpt[:6000],
        main_log_excerpt=main_log_excerpt[:3000],
        main_diff_excerpt=main_diff_excerpt[:6000],
    )
