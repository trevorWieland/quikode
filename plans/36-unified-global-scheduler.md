# Plan 36 - unified global scheduler and priority policy

## Goal

Create one authority for "what should run next?" across projects, phases, model chains, and resources.

Today normal task starts, review/CI fix responses, rebase work, merge-node refreshes, retry/resume work, and stack eligibility are controlled by separate paths. Multi-project orchestration requires all runnable work to become explicit candidates in a single queue.

## Current state

- `Orchestrator._pick_next()` ranks ready task starts only.
- Review and CI responses are dispatched by review watcher paths with `max_parallel + review_response_extra_slots`.
- Stacking readiness is a hard mode (`off`, `within-milestone`, `aggressive`) plus active-state exceptions.
- Subtask-boundary preemption is present but effectively inert.
- Task priority is an integer from fan-out, open PR boost, and subtask progress.

## Design

Introduce:

```python
Candidate(
    ref=TaskRef(...),
    phase="task_start" | "review_fix" | "ci_fix" | "rebase" | "merge_node_refresh",
    agent_role="planner" | "doer" | "checker" | ... | None,
    resources=ResourceRequest(...),
    model_request=ModelRequest(...),
    eligibility=[EligibilityReason(...)]
)
```

Introduce `PriorityPolicy.score(candidate, context) -> PriorityDecision`.

The decision must explain itself:

- project weight and fairness debt
- phase urgency
- critical path / downstream fan-out
- blocked or human-attention severity
- age and staleness
- retry health and loop risk
- PR/review/CI state
- resource fit
- model capacity fit

No code path should call `pool.submit()` directly without a scheduler decision.

## Priority policy

Initial scoring terms:

- `phase_urgency`: review/CI fixes and rebases have explicit urgency, not bypass slots.
- `criticality`: longest remaining path and direct fan-out.
- `project_weight`: weighted fair sharing across projects.
- `fairness_debt`: projects that have not received slots accrue priority.
- `resume_boost`: open PR and completed subtasks remain, but as named score terms.
- `staleness`: tasks waiting too long gain priority.
- `risk_penalty`: repeated same-signature retries, flatline verdicts, or near-budget states reduce auto-run priority and may route to blocked/manual review.

Stacking is simplified: only aggressive stacking exists after plan 42. Parent readiness remains an eligibility predicate, not a separate strategy.

## Implementation

1. Extract current project-local candidate collection into a pure `ProjectCandidateProvider`.
2. Add candidates for review fix, CI fix, rebase, merge-node refresh, and resume/retry.
3. Replace `review_response_extra_slots` with scheduler-managed phase capacity.
4. Replace `prefer_primary_candidates()` with policy score terms plus explicit `eligibility`.
5. Remove inert preemption or implement it through a scheduler-issued `YieldDecision`.
6. Add global scheduler loop:
   - poll all project providers
   - build candidate list
   - filter by resources and model capacity
   - score
   - dispatch selected work
   - record decision in `ControlStore.scheduler_events`
7. Keep per-project worker execution unchanged in PR-A; only dispatch moves upward.

## Acceptance

- A test with two projects proves the scheduler alternates by project weight when both have equal work.
- Review/CI fixes appear as candidates and no longer bypass normal scheduling.
- A saturated model budget blocks candidates requiring that model while allowing candidates for other roles/models.
- Every dispatch has a stored `PriorityDecision` with score breakdown and rejected-candidate reasons.

## Migration

Ship behind `qk control run` first. Existing `qk run` stays single-project until the control runner is stable.

