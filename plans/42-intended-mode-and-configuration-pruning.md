# Plan 42 - intended-mode cleanup and configuration pruning

## Goal

Remove legacy pathways and knobs that no longer represent real choices.

The codebase should expose one intended operating model: aggressive stacked work, unified global scheduling, scheduler-visible capacity, and policy-driven agent/model choice.

## Current cleanup candidates

These are not all deleted in one PR. This plan defines the audit and cutover sequence.

- `stacking_strategy = off | within-milestone | aggressive`
  - Target: only aggressive stacking exists.
  - Parent readiness remains a safety predicate, not a strategy.

- `stacking_readiness = speculative | settled`
  - Target: one readiness policy selected by the scheduler. Default should be safe, and exceptions must be explicit policy.

- `preempt_at_subtask_boundary` and `preempt_yield_threshold`
  - Target: either real scheduler-issued yield decisions or delete.

- `review_response_extra_slots`
  - Target: delete after plan 36 converts review/CI responses into normal candidates.

- `max_parallel_auto`
  - Target: replace with global resource/model-capacity suggestions.

- Direct model fields on `AgentRole`
  - Target: role chains owned by model policy.

- Per-project notification fields
  - Target: notification profiles in control config, with project overrides only where needed.

- Fake or test-only runtime settings leaking into production config
  - Target: keep test fixtures, remove from user-facing config.

- Backwards-compatible loaders for retired config keys
  - Target: fail fast for retired keys once migration plan lands.

## Audit method

For every config field:

1. Is this a real domain policy the user should tune?
2. Is there a universal right answer?
3. Is this a transition flag for a plan that already shipped?
4. Is this only for tests?
5. Does it bypass the global scheduler or capacity policy?

Classify as:

- keep global
- keep project override
- internal constant
- test-only
- retired with explicit loader failure

## Implementation

1. Add a generated config inventory test that lists every public `Config` field and its classification.
2. Create a new intended-mode config schema for the control plane.
3. Add explicit loader failures for retired keys.
4. Remove alternate stacking code paths after plan 36.
5. Remove review extra slots after plan 36.
6. Remove inert preemption after plan 36.
7. Replace single model assignments after plan 38.
8. Update docs and TUI settings to expose only real policy.

## Acceptance

- There is one documented operating mode.
- `qk doctor` reports retired keys clearly and refuses to run with them.
- Tests prove `off` and `within-milestone` stacking cannot be selected after cutover.
- TUI settings no longer shows knobs that are not supported strategic choices.
- The plan index and runbooks no longer advise legacy recovery or scheduling behavior.

