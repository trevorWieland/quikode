# Plan 31 — Stacked-diff rebase model (L1 + L2, done right)

**Status:** queued (single PR, no flags, no backwards compatibility).

## Why

Audit (2026-05-07) revealed that today's L2 behavior is **"always un-stack onto main on parent push"** — when a parent's PR branch advances (fixup commit, review-feedback push), `_safe_retarget_or_recreate` rebases the child onto `origin/main` and retargets the child's PR base to `main`. The child's stacked-PR identity is destroyed on the first parent fixup.

Plan 30's stacked-diff workflow assumes children **stay stacked on the parent's evolving tip**, with PR base = parent's branch, until the parent itself merges. Plan 31 rebuilds the rebase semantic to match. Today's code is treated as suspect — no flag, no opt-in, the new semantic is the only semantic.

Plan 31 also fixes two L1 bugs the audit surfaced.

## Decisions resolved (per user)

- **L1 + L2 child semantic**: child always rebases onto the parent's NEW tip (not onto main). PR base stays = parent's branch. Only when the parent merges to main does the child's PR retarget to main (same as today's merge-cascade path — preserved).
- **No backcompat**: the un-stack-onto-main code path on parent-push is REMOVED. No flag to keep it. Today's behavior is treated as a wrong implementation that ships gets replaced.
- **Conflict-resolver iteration cap**: today's hardcoded `max_iterations = 6` at `workers/rebase_conflicts.py:74` is wrong. Replaced by `cfg.conflict_resolver_max_iterations` (renamed from `conflict_max_resolve_attempts`, default 6 — preserves the effective cap but makes it configurable). The OUTER rebase retry budget is its own knob `cfg.rebase_max_attempts` (default 2 — preserves the effective L1 outer cap).

## Files modified

1. `quikode/workers/rebases.py` — `_prepare_rebase_plan`: when child has `parent_branches`, rebase target is `<parent_branch>.tip` (NOT `origin/main`). The `--onto` form moves child's commits onto the parent's new tip, preserving stack identity.
2. `quikode/workers/rebases.py` — `_finish_rebase_to_main` becomes `_finish_rebase`: stop calling `_safe_retarget_or_recreate` on parent-push rebases. Retarget-to-main is preserved ONLY for the parent-merged path (where the parent's branch is genuinely gone and the child must reattach to main).
3. `quikode/workers/rebases.py` — split the worker entry: `run_rebase_to_parent_tip` (parent advanced; stay stacked) vs `run_rebase_to_main` (parent merged; reattach). The orchestrator chooses based on the trigger reason it computes in `rebase_watch.py`.
4. `quikode/orchestration/rebase_watch.py` — `_schedule_rebase_to_main` becomes `_schedule_rebase` and accepts a `target: Literal["parent_tip", "main"]` derived from the trigger. `_schedule_cascade_rebase` (cascade-on-push) targets `parent_tip`; `_schedule_rebases_for_merged_parent` targets `main`.
5. `quikode/orchestration/rebase_watch.py` — add cascade-walk-level coalesce: a parent-branch-keyed `last_cascade_walk_ts` map gated by `cfg.rebase_coalesce_window_s`, suppresses redundant descendant-tree walks within the window. Per-child coalesce stays as belt-and-suspenders.
6. `quikode/workers/rebase_conflicts.py:74` — `max_iterations = self.cfg.conflict_resolver_max_iterations` (was hardcoded 6).
7. `quikode/workers/pr_lifecycle.py:191` — outer rebase budget gate uses `self.cfg.rebase_max_attempts` (was `conflict_max_resolve_attempts`).
8. `quikode/config.py` — rename: `conflict_max_resolve_attempts` → `conflict_resolver_max_iterations`. Add `rebase_max_attempts: int = Field(default=2, ge=1, le=10)`. No back-compat shim — config_loader fails on the old name.
9. `quikode/config_loader.py` — read the new keys.
10. `prompts/conflict-resolver.md` — for parent-tip rebase, the resolver's framing changes: it's resolving conflicts between the child's commits and the parent's new commits, not between the child and main. New jinja vars: `rebase_target_kind: "parent_tip" | "main"`, `parent_branch`, `parent_diff_excerpt`. Template branches on `rebase_target_kind` to render the right context.

## Tests

- `tests/orchestration/test_rebase_watch_l1.py` (new, ~150 LoC): main advances mid-task → cascade fires → conflict resolver gets `main_diff_excerpt` → on resolution, child's PR stays on its parent (or retargets to main if parent merged). Asserts FSM event sequence + final state.
- `tests/orchestration/test_rebase_watch_l2.py` (new, ~200 LoC): A → B → C chain. Push fixup to A. Assert: B rebases onto A's new tip with PR base unchanged (`base=A.branch`); C rebases onto B's new tip with PR base unchanged (`base=B.branch`); no calls to `_safe_retarget_or_recreate` fire on the push path. Run twice — second push within `rebase_coalesce_window_s` should suppress the cascade walk.
- `tests/orchestration/test_rebase_resolver_iterations.py` (new, ~80 LoC): conflict resolver fires, hits the `cfg.conflict_resolver_max_iterations` cap, BLOCKs cleanly with a forensic note.
- `tests/test_config_loader.py` (extend): old `conflict_max_resolve_attempts` key in config.toml → loader fails with a sharp error pointing at plan 31's rename. No silent acceptance.

Today's `tests/test_review_watcher.py` etc. were deleted in plan 28; the rebase tests were never written (audit confirmed). Plan 31 establishes the regression surface.

## What gets deleted

- The retarget-to-main code path inside `_finish_rebase_to_main` for parent-push triggers (~40 LoC). Lives only in the parent-merged path.
- The hardcoded `max_iterations = 6` literal.
- Any test that asserts the un-stacking semantic — there are none today, so this is forward-only.

## Migration

In-flight tasks at deploy time:

- Tasks in `REBASING_TO_MAIN` state will continue running today's code on the daemon they're attached to. After daemon restart for the deploy, orphan recovery resets them to PENDING_CI. The new code picks them up cleanly. No data migration needed.
- Children currently with PR `base=main` that WERE stacked on a parent before the un-stack happened: they look like fresh roots from the new code's perspective. Their "stacked" identity in the store (`parent_pr_branches`) may still be set if the un-stack never cleared it. The new code's `_prepare_rebase_plan` will try to rebase them onto the parent tip on the next cascade — this is correct behavior IF the parent branch still exists. If it doesn't (parent merged), the prune-dead-parents helper from plan 32 handles it. Pre-plan-32, the affected children should be `qk retry`-ed at deploy time per the fruit-of-rotten-tree pattern. List the wipe set in the deploy commit.

## Rollout

Single PR. Daemon restart required. Tanren workspace config has `[stacking] strategy = "aggressive" readiness = "settled"` already; no workspace-config changes needed.

## Sequencing

Plan 31 is foundational for plan 32 (merge-node first-class entity). Plan 32 assumes children-on-single-parent already work the right way — its multi-parent → merge-node → single-parent reduction would be incoherent if today's un-stacking semantic were preserved. Ship 31 first; 32 builds on it.
