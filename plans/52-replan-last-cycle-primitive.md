# Plan 52 — `qk replan-cycle` primitive between rewind and retry

## Why

The orientation §3.1 escalation primitives skip a step:

| Primitive | Discards |
|---|---|
| `qk resume` | nothing |
| `qk reset-retries` + resume | retry counters |
| `qk rewind <task> <subtask>` | target subtask + topo-after subtask state |
| `qk retry <task>` | worktree, branch, ALL subtask rows; planner re-decomposes from scratch |

There's a 100x cost gap between rewind (one subtask) and retry (everything).
For tasks with substantial done predecessors (R-0040 at 34/35; R-0008 at
40/62; R-0002 at 26/32), retry is catastrophic — the fix to a single
non-converging fixup-cycle subtask discards weeks of doer/checker work
that already passed.

The planner runs at multiple stages, not just task start:

1. **Initial plan** (PLANNING state, runs once at task start). Produces
   `S-01-*` ... `S-NN-*` + `Z-99-stabilize-spec-gate`.
2. **Fixup plan** (pre-PR audit cycle, runs once per cycle). Produces
   `F-N-M-*` subtasks scoped to the cycle's findings. N=cycle number,
   M=subtask index.
3. **Merge plan** (merge-node tasks, runs once at PLANNING). Produces
   integration subtasks.
4. **Replan** (post-PR review feedback, runs after a CHANGES_REQUESTED
   review). Produces a new set of `R-*` (or similar) subtasks.

Each cycle's subtasks form a logical group with a shared planning
intent. When that group's plan turns out to be wrong-shape (the
decomposition is asking the doer for something that can't converge),
the right intervention is **re-plan THAT cycle**, not the whole task.

R-0040 today: 34/35 done. F-CI-1 (a fixup-cycle subtask) keeps
empty-diffing. The other 34 committed subtasks are sound and graded.
A `qk retry` torches them. A `qk rewind F-CI-1` (which I just used)
preserves them but only re-runs F-CI-1 against the SAME planner output
— if the planner-decomposed F-CI-1 is fundamentally a no-op or
misdirected, repeated rewind-resets will keep failing the same way.

The missing primitive: reset all subtasks belonging to the most recent
planning cycle, AND re-fire just that planner.

## What ships

### CLI: `qk replan-cycle <task_id>`

Reset all subtasks that belong to the task's most recent planning
cycle, and re-run that cycle's planner. Preserves earlier cycles'
done-subtask commits + retry counters.

Behavior:

- Refuses (exit 2) when the task is not in `BLOCKED` or `FAILED` (mirror
  `qk reset-retries` and `qk rewind` constraints — primitive only valid
  for stuck states).
- Identifies the most recent planning cycle (see §"Cycle identification"
  below).
- For each subtask in that cycle: state→pending, retries=0, transient_retries=0,
  flatline_count=0, progress_check_count=0, retry_reasons=NULL.
- Force-pushes the task's branch back to the commit BEFORE the first
  subtask of that cycle was committed (per the same logic as
  `qk rewind`). Preserves all earlier-cycle commits.
- Sets a marker on the task row so the worker re-runs the matching
  planner phase (fixup_planner / replan_planner / merge_planner /
  initial planner) with the prior cycle's findings/feedback as input.
- Drops the task back to PENDING with a `replan_from_cycle_N` resume
  marker.

Exit messages:
- `replan R-NNNN: cycle <N> (kind=<initial|fixup|merge|replan>) reset` —
  M subtasks zeroed, branch rewound to commit <SHA>, planner will
  re-fire on the next scheduling tick.

Add `--dry-run` (mirror `qk rewind`) that prints the plan without making
changes.

### Cycle identification

The subtask id naming convention encodes the planning origin:

- `S-NN-*` → initial planner output (first cycle)
- `Z-99-*` → injected by initial planner; counts as part of cycle 1
- `F-N-M-*` → fixup-cycle N, subtask M (per fixup planner output)
- `F-CI-N-*` → CI-driven fixup (audit-coverage path)
- merge-node subtasks → from merge planner
- post-PR replan subtasks → typically `R-N-M-*` or similar (verify
  in `quikode/workers/pr_lifecycle.py` / replan_planner code)

Cleanest implementation: read all subtasks ordered by creation, group
by their planning cycle (using a new `planning_cycle` field on the
subtask row, populated when each planner runs), and target the
highest-numbered cycle. If the schema lacks a `planning_cycle` field,
add it — backfill existing rows by parsing the subtask id with the
naming convention as a heuristic.

Recommended: add a `planning_cycle: int` and `planning_kind: str`
column to the `subtasks` table via migration. Each planner output
sets these fields when it inserts new subtask rows. Then
`replan-cycle` is a clean SQL: `WHERE task_id=? AND planning_cycle=(
SELECT MAX(planning_cycle) FROM subtasks WHERE task_id=?)`.

The migration should backfill existing rows by inferring from subtask
id (`S-*`/`Z-99-*` → cycle 1, `F-N-*` → cycle N+1, etc.) or default
to cycle 1 for any pattern that doesn't match — the inference doesn't
need to be perfect since the field is only used by the new primitive
going forward.

### Worker re-fire

When the worker picks up a task with the `replan_from_cycle_N` marker:

- Skip the initial planner (the task already has earlier-cycle commits
  + planner output preserved).
- Identify which planner kind to re-fire from the cycle's
  `planning_kind` column (fixup / merge / replan / initial).
- Run that planner with appropriate inputs:
  - **fixup**: the prior cycle's `pre_pr_*` audit outputs (already in
    artifacts table)
  - **replan**: the prior cycle's bundled review context (also in
    artifacts)
  - **merge**: the parent set + merge inputs
  - **initial**: same as full retry but with the worktree preserved
    (rare — only when cycle 1 is the only cycle, in which case
    replan-cycle ≈ retry)
- Emit the new subtasks at `planning_cycle = N` (same number, same
  position in history; the regenerated subtasks REPLACE the prior
  cycle's, they don't form a new cycle).

The replacement semantics matter: if the operator runs replan-cycle
twice on the same cycle, the second run replaces the second-attempt
subtasks too — there's no proliferation of phantom cycles.

### Orientation update

`orientation.md` §3.1 primitives table gains a row:

```
| `qk replan-cycle <task>` | latest planning cycle's subtasks + commits | every earlier cycle's commits + retry counters |
```

§3.2 decision table gains entries:

```
| Subtask BLOCKED after rewind already used, but the failing subtask
  belongs to a fixup/replan/merge cycle (NOT the initial plan), AND
  the task has substantial earlier-cycle commits to preserve |
  **`qk replan-cycle <task>`** | Re-decompose the bad cycle without
  torching the foundation. |
```

§3.4 escalation ladder updates:

```
1. resume / reset-retries (infra-class noise)
2. rewind (toxic state in target subtask, predecessors clean)
3. replan-cycle (cycle-level over-scoping; preserve earlier cycles)
4. retry (last resort; planner re-decomposes from scratch)
```

Make the orientation edits in this PR alongside the CLI.

### Tests

- `tests/test_cli_replan_cycle.py` (new):
  - replan-cycle on a task with cycle-2 fixup subtasks: cycle-1 commits
    preserved, cycle-2 subtasks reset to pending, branch force-pushed,
    resume marker set.
  - replan-cycle dry-run: identifies the right cycle, no changes made.
  - replan-cycle on a task with only initial-cycle subtasks: emits the
    "no later cycle to replan" warning OR proceeds to re-run initial
    planner (decide one explicitly; document in CLI help).
  - replan-cycle on a task NOT in BLOCKED/FAILED: refused, exit 2.
- `tests/test_workers_planner.py` or similar (new tests):
  - Worker picks up a task with `replan_from_cycle_N` marker → re-fires
    the matching planner kind, emits subtasks at the same cycle number.
  - Subtasks emitted at cycle N replace prior cycle-N subtasks (no
    duplicates).
- Migration test: pre-existing subtask rows get backfilled cycle/kind
  via the inference heuristic.

### Plans index

Add plan 52 row to `plans/00-INDEX.md`.

## Operational followup (manager handles)

After the agent ships:
1. Validation ladder green.
2. Commit + push.
3. Reinstall + daemon restart (the migration runs at startup; verify
   no errors loading existing tasks).
4. Use replan-cycle on R-0040 when its current rewind doesn't converge
   — this is the canonical first-use case.

## Out of scope

- Replan-cycle that takes a `<cycle_number>` argument so the operator
  can target a non-latest cycle (e.g. roll back two cycles). Plan 53
  candidate; rare in practice and adds complexity to "what gets reset
  + commits force-pushed."
- Per-subtask cycle metadata in the briefing / `qk show` output
  (cosmetic; useful but not blocking).
- Auto-replan-cycle when a task hits some signal (e.g. transport
  stop-loss on a fixup cycle subtask). Manual operator primitive only
  for now.
