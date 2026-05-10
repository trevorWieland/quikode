# Plan 53 — fixup planner does root-cause tracing, not verbatim distillation; doer reproduces CI failure cleanly before declaring no-op

## Why

2026-05-10 morning incident: R-0040 PR #147 CI failed with
`generated-interface-contracts.ts is out of date. Run: pnpm
--filter @tanren/web run contracts:generate`. The fixup planner
read that error message and emitted **F-CI-1-sync-generated-web-
interface-contracts**, instructing the doer to run
`pnpm --filter @tanren/web run contracts:generate`. Verbatim
distillation of the error.

When the doer ran F-CI-1 in its local container, `pnpm contracts:
generate` produced no diff because the LOCAL container's
`/workspace/target/` and `node_modules` had cached state that
matched the committed TS file. Doer correctly reported "all gates
pass, no diff needed." But GitHub's CI runner — with no caches —
regenerates from a different effective baseline and DOES detect
drift. Same commit, same script, opposite results, all
environmental.

Two compounding problems:

1. **The fixup planner over-trusts the literal CI error message.**
   It treats the error string as a precise root cause and emits a
   subtask that just runs the suggested command. It doesn't
   understand that:
   - CI errors often surface symptoms one level UP the build graph
     from the actual broken inputs.
   - The same script can produce different outputs in different
     environments (cached vs fresh, different toolchain pinning).
   - "Run X to fix Y" is a CI-runner heuristic, not a guaranteed
     truth.

2. **The doer has no positive way to express "no-op + all gates
   pass."** It runs `pnpm contracts:generate`, sees no diff,
   correctly reports it. But the empty-diff path falls through to
   plan 51's transport stop-loss after 3 attempts. The doer's
   correct local diagnosis (file is in sync) is treated as a
   transport failure.

**Critical missing context:** the planner never sees that local CI
passes while GitHub CI fails. That single signal — "the CI failure
doesn't reproduce locally" — would have prompted a smarter
investigation: probably an environmental drift, possibly stale
caches, definitely not a one-line fix. With that context, the
planner could emit a subtask like "investigate why local just ci
passes but GitHub fails on web-contracts-check; reproduce the
failure under fresh-state conditions, then fix the underlying
input-or-toolchain divergence."

## What ships

### Part 1: fixup-planner prompt — root-cause tracing required

`prompts/fixup-planner.md`:

- Add a hard rule: **the fixup planner must trace each finding
  back to a hypothesis about WHY it failed, not just WHAT failed.**
  Replace any "the CI says run X, so the subtask should run X"
  framing with "the CI says X — what does X depend on, why might it
  be out of date, and what's the smallest change that fixes the
  underlying cause?"
- Add a new mandatory section in the planner output: per-subtask
  `root_cause_hypothesis` field on `FixupPlannerOutput.subtasks[]`
  (or `notes` field if schema change is too invasive). For F-CI
  subtasks the hypothesis must explain the suspected upstream
  cause, not restate the CI error.
- Add explicit guidance: "if the CI error suggests running a
  generator (e.g. `pnpm contracts:generate`), the subtask must
  invoke the FULL build chain producing the generator's inputs
  before running the generator itself. Do not assume cached
  intermediate artifacts (e.g. `target/`, `node_modules`)
  represent the canonical build state."

### Part 2: planner gets local-vs-CI-passing signal

The planner's prompt context (`prompts/_evaluation_context.md.j2`
or wherever fixup planner inputs are rendered) must include:

- **The local `just ci` outcome at the same commit GitHub reports
  failing on**, when known. Captured by:
  - The post-PR FSM's CI-fix scheduler runs `just ci` locally before
    invoking the fixup planner (existing pre_pr.py local-CI gate
    output is already in artifacts; if it's not in this code path,
    add it).
  - Pass that outcome into the fixup-planner prompt as a
    structured field: `local_ci_at_head_passed: bool` plus a brief
    excerpt.
- The fixup-planner's prompt must explicitly handle three cases:
  - GitHub fails AND local fails: real bug; emit fix.
  - GitHub fails AND local passes: environmental drift OR cached-
    state masking; emit an investigation subtask, not a one-liner.
  - GitHub passes AND local passes: CI is stale; refuse to plan
    (this case might already be handled by the audit cycle, but
    document it explicitly).

### Part 3: doer prompt — reproduce-then-fix for CI fixups

`prompts/subtask-doer.md`:

For subtasks with `kind == "fixup_ci"`:

- Add a section: "before declaring a CI-fix subtask done, you MUST
  attempt to reproduce the CI failure under fresh-state conditions:
  - For dependency-graph-related fixes: `cargo clean` (or the
    equivalent for the language), wipe relevant caches, then run
    the failing recipe.
  - For codegen drift: invoke the FULL chain producing the generated
    artifact's inputs, NOT just the final-step generator.
  - If the failure does NOT reproduce after a clean rebuild, the
    fix is environmental and the subtask CANNOT be no-op'd by
    running the suggested command alone. In that case, surface a
    structured 'cannot_reproduce' marker (see below) and stop;
    don't keep claiming success."

### Part 4: empty-diff + green-gates positive DONE path

`quikode/workers/subtask_execution.py`:

Plan 51 short-circuits empty diff to `failure_layer=transport`. Add
a sibling discriminator BEFORE that:

- After the doer returns and `_compute_subtask_diff_excerpt` returns
  empty, run `subtask_check_command` (already done as part of
  `_check_subtask`'s objective gate) AND the scoped witnesses.
- If BOTH pass on an empty diff AND the subtask kind is NOT
  `fixup_ci` → mark the subtask DONE without a commit (synthesize a
  PASS outcome, set state=done, add a `subtask_no_op_done` artifact
  recording the verification).
- If the kind IS `fixup_ci` AND both gates pass on empty diff → do
  NOT mark done (might be the F-CI-1 environmental-drift trap).
  Instead, treat as an explicit "cannot reproduce locally" signal:
  fail-fast with a distinct `failure_layer=cannot_reproduce` (new
  enum value) so the next triage / fixup planner gets the right
  context. Stop-loss threshold for this signature: K=2 (immediate
  operator-attention).

### Part 5: F-CI-* cycle backfill heuristic correction

The plan-52 migration backfilled `F-CI-*` → cycle 2 hardcoded. That's
wrong — F-CI-* subtasks come AFTER all pre-PR fixup cycles. Patch
`_infer_planning_provenance` to compute the right cycle for
`F-CI-*`:

- For `F-CI-*` rows: cycle = `MAX(planning_cycle for non-F-CI rows
  on this task) + 1`, kind = "fixup_ci".
- This requires the migration to do a two-pass: first backfill all
  non-F-CI rows, then pass two for F-CI rows.

This is a small fixup to plan 52's migration. Re-run safe (idempotent
re-passes are no-ops when the value already matches the heuristic).

### Tests

- `tests/test_prompts.py` (or fixup-planner-specific):
  - Fixup-planner prompt rendering includes `local_ci_at_head_passed`
    field when the input data has it.
  - Fixup-planner prompt includes the root-cause-tracing rule
    section (assert text presence — string-match is fine).
- `tests/test_subtask_execution_new_loop.py` (or sibling):
  - Empty diff + gates pass + kind != "fixup_ci" → no-op DONE path
    (synthesized PASS, state=done, `subtask_no_op_done` artifact).
  - Empty diff + gates pass + kind == "fixup_ci" → cannot_reproduce
    failure_layer (new enum value, distinct stop-loss at K=2).
- `tests/test_signature_layer_stop_loss.py`:
  - cannot_reproduce stop-loss fires at K=2 with operator-clear
    message naming environmental-drift hypothesis.
- `tests/test_planning_cycle_migration.py`:
  - F-CI-* row in a task that has F-1 through F-5 → backfilled
    cycle = 7, not 2.
- `tests/test_workers_pre_pr.py` or `test_workers_pr_lifecycle.py`:
  - When the post-PR FSM dispatches the fixup planner on a CI
    failure, it captures `local_ci_at_head_passed` from a fresh
    local-CI run AND passes it to the planner.

### Schema changes

- `SubtaskTriageOutput.failure_layer` enum gains `"cannot_reproduce"`
  (in `quikode/agent_schemas.py`).
- Stop-loss config: new field `subtask_cannot_reproduce_stop_loss_count`
  (Field default 2, ge=2, le=10). Plumb in load_config (plan 50's
  audit warns will catch you if you forget).
- `FixupPlannerOutput.subtasks[i]` gains optional
  `root_cause_hypothesis: str` (≤500 chars) — nullable for
  back-compat across existing artifacts. Default "" / None
  acceptable.

### Plans index

Add plan 53 row to `plans/00-INDEX.md`.

### Orientation update

`orientation.md` §7 invariants list — add a bullet:

> **Fixup-CI subtasks must reproduce-before-fix.** The doer for a
> `kind="fixup_ci"` subtask cannot declare done by running the CI
> error's suggested command alone — it must first reproduce the
> failure under fresh-state conditions. Empty-diff + green-local-
> gates on fixup-CI is interpreted as `cannot_reproduce`
> (environmental drift signal), not as no-op DONE.

## Operational followup (manager handles)

After the agent ships:
1. Validation ladder green.
2. Commit + push.
3. Reinstall + daemon restart.
4. The next time a fixup-CI cycle runs, the planner emits richer
   subtasks AND the doer's reproduce-before-fix rule catches the
   environmental-drift case at K=2 with a clearer signal.

## Out of scope

- Auto-detecting which build chain produces a given generated
  artifact's inputs (would require parsing the project's justfile /
  Makefile and following recipe dependencies). The doer prompt
  rule "invoke the FULL chain producing inputs" pushes the
  responsibility to the LLM doer, which is fine — we trust the
  doer to read the project's build system once told to.
- Running `just ci` in a fresh container per attempt to canonicalize
  the local environment. Heavy, defer to plan 54 candidate if the
  cannot_reproduce signal turns out to be common.
- Fixing the underlying environmental drift between local container
  and GitHub runner. Out of quikode scope; tanren-side concern.
