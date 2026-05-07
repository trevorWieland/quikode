# Plan 24 — final spec-stabilization subtask, deterministically injected

## Symptom

After every spec-subtask plan completes (last `S-*` lands and pushes),
the worker enters `local_ci_checking` → cycle 1 of the pre-PR audit
gauntlet. The local CI gate (`just ci`) **almost always fails on cycle 1**
because:

- Per-subtask checks run a fast gate (`just check`), not the full
  `just ci` (which runs tests, BDD scenarios, web-test, etc.).
- Cross-subtask integration breakage isn't visible until all subtasks
  have landed.
- The doer for the last subtask hands off without anyone owning the
  question "is the entire branch's CI green?"

The cycle 1 failure cascades into the fixup planner, which decomposes
the cycle 1 findings (rubric/standards/local_ci) into F-* subtasks.
The whole gauntlet then re-runs as cycle 2. This burns one full cycle
of audit work to discover what the doer-of-the-last-subtask was best
positioned to fix in real time.

## Fix

Append a deterministic system-injected subtask to every spec plan,
sitting after every planner-emitted subtask and depending on all of
them:

- `id = "Z-99-stabilize-spec-gate"` (Z-prefix sorts last under any
  scheduler ordering)
- `title = "Stabilize spec gate — ensure all gate checks pass cleanly"`
- `depends_on =` every prior subtask id
- `files_to_touch = []` — no declared lane; scope-review adjudicates
  every cross-file edit per the existing invariant "gate-keeping
  cross-file fixes are always legitimate" (plan 13)
- `acceptance = ["Spec gate (`{cfg.subtask_check_command}`) passes
  with rc=0", "All committed changes from prior subtasks remain
  functional"]`
- `boundary` and `notes` explicitly tell the doer that this is a
  stabilization slot — run the gate, fix anything red, repeat
- `kind = "spec"` (no FSM branching needed; reuses the existing
  doer/checker/triage loop)

Injection is **programmatic, not planner-driven** (per user
direction): the planner doesn't need to know about it; we inject
during `validate_and_build_plan` if no `Z-99-*` subtask is present.
Idempotent across resumes: re-parsing the persisted `plan_text`
short-circuits when the stabilization subtask already appears in the
input.

## Companion: doer prompt rule for test-authoring

Every doer call now carries a "test-author owns test green"
invariant. If the doer writes or modifies tests, it is responsible
for running those tests through their actual runner before stopping.
Handing off red tests for the next subtask, the stabilization
subtask, or the pre-PR audit to discover wastes retries and surfaces
failures to layers that can't fix them as efficiently as the
test-author can.

## Why programmatic injection beats planner-prompt teaching

- **Determinism.** Planners forget under context pressure; injection
  is unconditional. The user explicitly preferred this: "if the
  shape and text of the new enforced final subtask is the same […]
  we could also just default to injecting it ourselves without
  asking the agent to."
- **Prompt-budget savings.** No new section in the planner prompt;
  the planner stays focused on decomposition.
- **Schema-level guarantee.** The validator runs every parse, so
  there's no "plan looked OK at planning time but missed the
  stabilization subtask later" failure mode.

## Why `files_to_touch=[]` works (and isn't a regression)

`scope_review.review_scope_drift` always runs its agent when
`actual_set ⊄ declared_set`. With `declared = []`, every doer edit
is "out of lane" and triggers scope-review. The reviewer reads the
doer's contemporaneous summary; under the existing "gate-keeping
cross-file fixes are always legitimate" prompt rule, it
rubber-stamps gate-fix justifications. Cost: one extra scope-review
agent call per stabilization-subtask attempt. Benefit: every cross-
file fix is recorded (post-plan-21) instead of slipping through.

## Edge cases

- **Branch is already CI-green.** The doer runs the gate, sees
  rc=0, declares done. One short-attempt no-op subtask. Cheap.
- **Resume after the stabilization subtask is partway done.** The
  worktree has whatever fix the doer was working on (preserved by
  the unstage-not-revert invariant). Plan 22's prior-doer-output
  carry-forward gives the next attempt the prior investigation.
- **Cycle 2 of pre-PR audit fixup.** Fixup plans (`FixupPlan`) do
  NOT get a stabilization subtask — they are addressing already-
  surfaced findings, not building new feature work whose CI cleanup
  needs to be owned.

## Validation

- 4 new schema tests:
  - `test_stabilization_injection_appends_when_command_given`
  - `test_stabilization_injection_skipped_when_command_none`
  - `test_stabilization_injection_idempotent_if_already_present`
  - `test_parse_planner_output_threads_command_through`
- Existing schema tests unaffected (default `spec_gate_command=None`).
- Full suite: `uv run pytest tests/ -q --deselect ...daemon-budget`
  — 858 passed.

## Future affordance

A future TUI render can show the Z-99 stabilization subtask with a
distinct visual cue (gate-stabilization vs feature work) using the
ID prefix as the marker. Not part of this plan.
