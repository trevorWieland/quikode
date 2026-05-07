# Plan 32 — Merge-node as a first-class entity (L3)

**Status:** queued (likely two PRs, depends on plan 31 shipping first; no backwards compatibility).

## Why

Today's multi-parent handling is a paper feature. The plumbing exists (JSON-array `parent_branches` / `parent_pr_branches`, `stacking.construct_merge_base`, `_multi_parent_rebase_boundary`) but the **detection layer is single-parent-keyed**: each cascade trigger is keyed to one advancing branch, the 30s coalesce window can swallow legitimate second-parent pushes, and a deleted parent-branch (after merge) silently breaks the merge-base construction. Tanren has 64 multi-parent nodes (27% of the DAG); this isn't an edge case.

The user's design (resolved in conversation):

> "It's itself a different subloop, that has the merge conflicts (if any), and also has the context of all behaviors of both parents, and then can follow the same plan → implement → check, but since this merged branch isn't going into main yet, that can instead just serve now as a unified A+B branch. That way, if C, D, E all three depend on both A, B, A+B only need to be intelligently merged once, and that product (a new branch itself!), serves as the base_branch for C, D, E. … If A gets a change, that percolates to A+B needing a change, that percolates to C, D, E each needing a change."

Translated: the merge of N parents is a **first-class synthetic task** with its own branch, lifecycle, and audit gauntlet. Multiple downstream children share it. Updates propagate through it like any other parent.

This plan introduces merge-nodes and rewrites the multi-parent code paths to reduce to single-parent semantics from the children's perspective.

## Decisions resolved (per user)

- **Multi-parent integration**: synthetic merge-node task per unique parent set. Materialized lazily (when first needed by a downstream child or when triggered by an ancestor's settle), reused across all children that share the same parent set.
- **One-parent-merged-others-open semantic**: merge-node's parent set updates to drop the merged parent (its commits are now in `main`); merge-node re-runs against `(main, remaining_parents)`. The merge-node never blocks because of a missing parent.
- **Conflict-resolver-failure semantic**: there is no "this is unsolvable, BLOCK forever." The merge-node's own subloop (plan → resolve → audit) IS the way to legitimately resolve conflicts that the deterministic resolver can't. Eventually all code merges to main; the system must always find a path.
- **Order of integration when sequential merge is needed**: by the order each parent appears in the downstream child's `Node.depends_on` (DAG seed order). Stable, semantically meaningful (the seed-author's intended integration order). Closeness-based ordering is deferred — revisit if practice shows seed order produces bad merge sequences.
- **No backcompat**: today's `construct_merge_base` + `_multi_parent_rebase_boundary` paths are REMOVED. The new code is the only code. Children always see a single effective parent (a merge-node when their `Node.depends_on` has > 1 entry).

## The merge-node abstraction

A merge-node is a task in the same `tasks` table, with `kind = "merge"` (column already exists; today only `kind="spec"` rows are created). Differences from a spec task:

- **Identity**: id is derived: `M-<sorted-parent-ids-hash>` (e.g., `M-a83f7b` for sorted parents `["R-0002", "R-0008"]`). Deterministic so the lookup-or-create idempotency holds.
- **No DAG node**: not in the seed DAG; created at runtime. The store is the source of truth.
- **Parents**: `parent_task_ids` is the set of source spec tasks. `parent_branches` is their PR branches.
- **No PR**: merge-nodes never open a PR. They're internal integration artifacts. The eventual review of integrated work happens at the downstream child PRs (which inherit the merge-node's state).
- **Audit gauntlet**: runs the same local-CI + rubric + standards + behavior audits as a spec task. The "behavior" audit verifies the merged content; rubric/standards verify the merge resolution. Goes through `LOCAL_CI_CHECKING` → `PRE_PR_AUDITING` → "ready" (a new state, see below).
- **No `merged` terminal**: a merge-node's terminal "ready" state is `MERGE_NODE_READY`. It stays in this state until a parent advances (back to `PROVISIONING` for the re-merge cycle) or until all parents have merged to main (terminal `MERGE_NODE_RETIRED` — its branch is no longer needed; downstream children's effective base becomes `main`).
- **Branch**: `quikode/merge/<sorted-parent-ids>-<short-content-sha>`. Force-pushed on each update. Retains the same name across updates so children's PR base stays stable.
- **Worktree**: standard `<worktree_root>/<merge-node-id>` like any other task.
- **Doer / fixup-planner / scope-review**: same prompts as a spec task, except the planner's job is to produce a clean integration of the source branches (not to implement a feature). New planner prompt `merge-planner.md` in `prompts/`. Conflict-resolver runs first (deterministic git rebase + resolver iterations); only when it fails, the merge-doer subloop kicks in with full context of both parents' diffs and behaviors.

## Lifecycle FSM

The merge-node reuses most of the existing FSM:

```
[create] → PROVISIONING → PLANNING → DOING_SUBTASK (← merge planner emits subtasks) → ...
                                  └─ for trivial cases (clean octopus / sequential merge), planner emits
                                     a single "S-01-integrate" subtask whose doer is the conflict resolver
                                     itself → CHECKING_SUBTASK → COMMITTING → PUSHING → LOCAL_CI_CHECKING
                                     → PRE_PR_AUDITING → MERGE_NODE_READY (new terminal-ish state).
```

For non-trivial merges (semantic conflicts, behavioral integration issues — "A added a method, B renamed it"), the planner emits multiple subtasks and the regular doer/checker loop drives them. The audit gauntlet still runs at the end. No PR opens; on `AUDIT_PASSED`, the FSM enters `MERGE_NODE_READY` directly (skipping `PR_OPENING`).

New events / states:

- State: `MERGE_NODE_READY` — terminal-ish; the merge-node is integrated and ready to serve as a base. Reset to `PENDING` (with resume marker preserving prior subtasks) when any parent advances.
- State: `MERGE_NODE_RETIRED` — terminal. All parents have merged to main; the merge-node branch is no longer needed.
- Event: `MERGE_NODE_BUILT` (PRE_PR_AUDITING → MERGE_NODE_READY).
- Event: `PARENT_ADVANCED` (MERGE_NODE_READY → PENDING with resume marker).
- Event: `ALL_PARENTS_MERGED` (MERGE_NODE_READY → MERGE_NODE_RETIRED).

The existing `kind` column on subtasks gets a new value `"merge"` so subtasks emitted under the merge-node show up correctly in `qk show`.

## How children resolve their effective parent

When a child task with `len(node.depends_on) > 1` provisions:

1. Compute the merge-node id from its sorted `node.depends_on`.
2. Look up the merge-node in the store. If not present, create it (`PENDING` state) and refuse to schedule the child until the merge-node is `MERGE_NODE_READY`.
3. If present and `MERGE_NODE_READY`: rewrite the child's `parent_branches` and `parent_pr_branches` to `[merge_node.branch]` (single-entry list). The child sees a single effective parent — all the existing single-parent stacking semantics apply. The child's PR base = `merge_node.branch`.
4. If present but not ready: child waits (treated as PENDING with un-met deps; scheduler tier filter naturally defers it).

When the merge-node updates (parent advanced), it goes back to `PENDING` and re-runs. Its branch is force-pushed. Children that depend on it observe the branch tip change via the existing `_maybe_schedule_cascade_for_push` mechanism — same code path as plan 31's L2 stacked-rebase. From the child's perspective, the merge-node IS its single parent; nothing new is needed in the child's rebase code.

This is the **structural simplification**: multi-parent → merge-node → single-parent. The complexity is encapsulated in the merge-node's lifecycle; the child code stays single-parent.

Recursive composition: if D depends on `[A+B, C]`, the merge-node `M(M(A,B), C)` is computed. The merge-planner sees parents `[merge_node_M_AB, C]` and integrates them. Same mechanism, arbitrary depth.

## Cascade dynamics

Existing single-parent cascade (plan 31's L2) handles parent-push → child-rebase. Plan 32 layers on:

1. **Parent push** → all merge-nodes that have this parent in their `parent_task_ids` enter `PENDING` (new resume marker `parent_advanced`). The orchestrator's review-watcher tick checks this and schedules the merge-nodes alongside spec tasks.
2. **Parent merged** → merge-nodes with this parent in their `parent_task_ids` get the parent removed. If `parent_task_ids` becomes a single-entry set, the merge-node retires (the remaining parent's branch IS the effective base; no merge needed). If it becomes empty (all parents merged), the merge-node enters `MERGE_NODE_RETIRED`.
3. **Merge-node ready** → all children that depend on this merge-node observe its branch tip via existing single-parent cascade; their rebase fires.
4. **Merge-node retired** → children's effective base becomes the surviving parent's branch (or `main` if the merge-node had only the one merged parent's commits). Child PRs retarget accordingly.

## Files modified

### Storage / FSM

1. `quikode/state_schema.py` — `tasks` table: `kind` column already exists with default `"spec"` (currently set on `subtasks`, need to verify; if not, add). New states added to `fsm.State`. New events. Migration: idempotent UPDATE for any pre-existing `kind=NULL` rows → `kind="spec"`.
2. `quikode/fsm.py` — add `MERGE_NODE_READY`, `MERGE_NODE_RETIRED`, `MERGE_NODE_BUILT`, `PARENT_ADVANCED`, `ALL_PARENTS_MERGED`. Transitions:
   - `(PRE_PR_AUDITING, MERGE_NODE_BUILT) → MERGE_NODE_READY` — for `kind=merge` rows; the existing `(PRE_PR_AUDITING, AUDIT_PASSED) → PR_OPENING` only fires for `kind=spec`. The worker selects the event based on kind.
   - `(MERGE_NODE_READY, PARENT_ADVANCED) → PENDING` (with resume marker).
   - `(MERGE_NODE_READY, ALL_PARENTS_MERGED) → MERGE_NODE_RETIRED`.
3. `quikode/fsm_runtime.py` — helpers for the new transitions.
4. `quikode/state_types.py` — `MERGE_NODE_READY` joins `POST_PR_STATES`-equivalent for stack-readiness purposes; `MERGE_NODE_RETIRED` is terminal.

### Merge-node creation + lookup

5. `quikode/merge_node.py` (new, ~250 LoC) — `compute_merge_node_id(parent_task_ids)`, `lookup_or_create(store, parent_task_ids)`, `propagate_parent_advanced(store, parent_id)`, `propagate_parent_merged(store, parent_id)`. The store-side surface area.
6. `quikode/store_tasks.py` — `create_merge_node(id, parent_task_ids, parent_branches)`, `merge_nodes_with_parent(parent_id)`, `merge_node_by_id(id)`. Plus `set_kind(task_id, kind)`.

### Merge-node worker

7. `quikode/workers/merge_node.py` (new, ~300 LoC) — the merge-node's own worker. Provisions a worktree off `cfg.base_branch`, fetches each parent's branch, attempts deterministic merge first (octopus → sequential per `Node.depends_on` order), then on conflict spawns `merge-planner` to plan integration subtasks. Reuses `_provision`, `_run_fixup_round` (with `kind="merge-integration"`), `_run_pre_pr_pipeline`. Skips `pr_opening`.
8. `quikode/workers/task_worker.py` — dispatch by `kind`: when row's `kind == "merge"`, instantiate the merge-node worker; otherwise the spec worker.

### Children: effective parent resolution

9. `quikode/workers/provisioning.py` (or wherever provisioning happens) — at the start of provisioning a spec task with multi-parent deps, resolve the merge-node and rewrite `parent_branches` / `parent_pr_branches` to the merge-node's branch. From this point the child's code is identical to single-parent today.
10. `quikode/orchestration/scheduler.py` — `is_parent_stack_ready` extended: a "parent" is ready if it's in `STACK_READY_STATES` (today's spec-task case) OR if it's a merge-node in `MERGE_NODE_READY` (new). The settled-readiness gate (plan 30) applies to merge-nodes too — the merge-node's `most_recent_awaiting_review_entry_ts` becomes `most_recent_merge_node_ready_entry_ts` (new helper, reads state_log).

### Cascade integration

11. `quikode/orchestration/rebase_watch.py` — `_schedule_rebases_for_merged_parent` calls `merge_node.propagate_parent_merged` to update merge-nodes whose parent set includes the merged task. `_schedule_cascade_rebase` calls `merge_node.propagate_parent_advanced` for merge-node parents (these enter PENDING and the daemon picks them up).

### Conflict resolver context

12. `prompts/conflict-resolver.md` — extended with per-parent context (jinja `parent_diffs: list[{branch, log, diff}]`) used when invoked under a merge-node. New jinja var `merge_node_id` distinguishes the merge-context from the L1/L2 single-parent rebase context.
13. `quikode/prompts.py:192-207` — extend `conflict_resolver_prompt` signature with `parent_contexts` arg.
14. `quikode/workers/rebase_conflicts.py` — when invoked under a merge-node worker, pass per-parent diffs.

### Merge-planner prompt

15. `prompts/merge-planner.md` (new, ~80 lines of jinja) — the merge-node's planner. Inputs: parent task ids, parent branches, parent diffs against `cfg.base_branch`, and a brief description of each parent's behavior (from their DAG node titles + summaries). Output: same JSON plan shape as `planner.md`, with subtasks scoped to the integration work.

### Order of sequential merge

16. `quikode/stacking.py` — today's `construct_merge_base` is REMOVED (no backcompat per user directive). Replaced by `_attempt_octopus_then_sequential` inside `quikode/workers/merge_node.py`, with sequential order = `Node.depends_on` of the downstream child's DAG node when there's a single referencing child, else `sorted(parent_task_ids)` (deterministic fallback for the rare case of multiple downstream children disagreeing on order — pick one, log a warning, accept the risk; in practice DAG seed authors don't put contradictory orderings).

## What gets deleted (no backcompat)

- `quikode/stacking.py:construct_merge_base` — replaced by merge-node's own integration logic.
- `quikode/workers/rebases.py:_multi_parent_rebase_boundary` — multi-parent rebase no longer happens at the child level. Children always see single-parent (the merge-node).
- The `parent_branches` array as a runtime input to child rebase code becomes a single-element list always (the merge-node's branch), set at provisioning. The store still keeps the original multi-parent JSON for audit/forensics.

## Tests

- `tests/test_merge_node_lifecycle.py` (new, ~250 LoC): create with 2 parents, run through plan/integrate/audit/ready. Parent advances → re-runs. All parents merge → retires.
- `tests/test_merge_node_recursive.py` (new, ~150 LoC): D depends on `[A+B, C]`. Verify M(M(A,B), C) is computed; M(A,B) materializes first; then the outer merge integrates M(A,B) and C; D's effective parent is the outer merge-node.
- `tests/test_merge_node_partial_merge.py` (new, ~120 LoC): A merges to main while B is still open. M(A,B) updates: parent A removed, retires (B is now the sole effective base) OR re-runs against `(main, B)` depending on which makes sense after we've implemented it. Asserts the "no BLOCK on partial merge" property.
- `tests/test_merge_node_conflict_resolution.py` (new, ~180 LoC): A and B make conflicting edits to the same file. Octopus fails → sequential fails → merge-planner emits integration subtasks → doer resolves → audit passes → ready. Asserts the planner gets per-parent context.
- `tests/test_scheduler_with_merge_nodes.py` (new, ~120 LoC): scheduler defers a multi-parent child until the merge-node is ready; primary-first tier still applies.

## Migration

In-flight tanren run at deploy time:

- All multi-parent children currently in any state with `parent_branches` length > 1 are wiped (fruit-of-rotten-tree). The deploy commit lists them. Deploy notes: `qk abort && qk retry` each.
- All currently-running spec tasks are unaffected.
- No merge-nodes exist pre-plan-32; first one materializes when the first multi-parent child provisions post-deploy.

## Rollout

Two PRs to keep the review surface manageable:

- **PR-A**: storage / FSM + merge-node creation+lookup + merge-node worker + scheduler integration. ~600 LoC + tests. Multi-parent children still BLOCK on schedule (no merge-node worker can run yet because the planner prompt + conflict-resolver-context aren't deployed); this is intentionally mechanical and verifiable in isolation.
- **PR-B**: merge-planner prompt + conflict-resolver multi-parent context + the integration tests. ~300 LoC + tests. After this lands, merge-nodes actually run.

Daemon restart on each PR. Validation ladder must stay green at every step.

## Audit gauntlet for merge-nodes (resolved)

The full pre-PR gauntlet (rubric + standards + behavior + local-CI) is calibrated for spec tasks shipping new feature work. For a merge-node, only two stages carry real signal — and skipping the others avoids redundant work:

- **Local CI gate (`just ci`)** — required. Compile, lint, fast tests must pass on the integrated branch.
- **Behavior audit** — required. `expected_evidence` is the **union of all source parents'** `expected_evidence`. The audit verifies every BDD scenario / witness from each parent still passes after integration. This is the "true risk surface of merging two branches autonomously" — A's behaviors might rely on a method B renamed; B's behaviors might rely on a contract A re-shaped; the integration may compile (CI passes) but break either parent's behavioral promise. The behavior audit catches this.
- **Rubric audit** — skipped for merge-nodes. The integration's diff is the union of parents' diffs (modulo any merge-doer additions for semantic resolution); both parents already passed rubric scoring individually. Re-scoring the union is redundant.
- **Standards audit** — skipped for the same reason. Standards drift is per-file; if neither parent introduced drift, the union doesn't either.

**Exception**: when the merge-doer subloop runs (i.e., octopus/sequential merge had unresolvable textual conflicts and the planner emitted integration subtasks), the doer's commits ARE genuinely new content not present in either parent. For those cycles, rubric + standards audits ALSO run on the merge-doer's diff (against the parents' merge-base, not against main), to catch new code that bypasses the parent-level audits. The `pre_pr_audit` pipeline gains a `merge_node_mode` flag that skips rubric/standards by default but re-enables them when the cycle's commits include subtasks with `kind="merge-integration"`.

Stage labels in the TUI reflect this: a merge-node row shows "local_ci · behavior" for the trivial-merge case, "local_ci · rubric · standards · behavior" for the conflict-resolution case.
