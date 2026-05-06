# Plan 13 — scope reviewer must accept gate-keeping cross-file fixes

## Why

Plan 12 strengthened the doer/triage prompts to authorize editing files outside
`files_to_touch` when needed to keep gates green. But the **scope reviewer**
(`quikode/scope_review.py` + `prompts/scope-review.md`) was still rejecting
those same edits as "overreach" because they crossed module borders. The
result: the doer fixes the cross-file issue, scope reviewer marks it
illegitimate, the worker reverts to the lane-only diff, the gate fails again,
triage tells the doer to fix the cross-file issue, infinite loop.

Concrete evidence from the overnight run: R-0021/S-08-bdd-B-0030 attempt 9.
The doer correctly fixed `crates/tanren-cli-app/src/project.rs` and
`crates/tanren-store/src/lib.rs` to make the BDD scenarios runnable. The scope
reviewer rejected those hunks because they were "in different modules from the
declared `tests/bdd/features/B-0030-disconnect-project.feature` lane." The
triage then dutifully told the doer to revert them — which would put the gate
back into the failing state.

The prompts were in conflict: doer = "fix everything red"; scope review =
"reject everything cross-module."

## The fix

Edit `prompts/scope-review.md` to add a "Hard rule: gate-keeping cross-file
fixes are ALWAYS legitimate" section. The rule:

> Edits outside `files_to_touch` are legitimate when:
> - An earlier subtask of THIS task committed a bug, OR
> - Triage notes from a prior attempt identified the cross-file fix, OR
> - A test fixture/harness/generated artifact panics on initialization.
>
> The test: ask "would removing this edit cause a gate failure on this branch?"
> If yes → legitimate. If no → overreach.

Module borders are heuristics; gate-greenness is the contract.

## Why this doesn't open a flood

The rule is gated by gate-failure. A doer who edits unrelated files "because
they were failing anyway" has to demonstrate that removing the edit causes a
gate failure — which is exactly the case where we WANT the edit to land. A
doer who edits unrelated files for unrelated reasons (cleanup, refactor) still
gets caught: their out-of-lane edits don't gate-greenness anything, so they're
overreach.

## Sequencing

This is a runtime hotfix landed during the overnight tanren run. It
complements plan 12 (no-ci-leak-invariant). Both are pure prompt changes that
ship via reinstall — no FSM code changes, no daemon restart needed.

## Validation

The next R-0021/S-08 attempt should land its cross-file fix without rejection.
If retries still climb, that's a sign one of these is true and we need a
different mechanism:

- The doer's cross-file fix is actually wrong (would cause OTHER failures).
- The scope reviewer is still rejecting despite the new rule (model adherence
  failure — would need a stronger phrasing or a deterministic carve-out for
  files mentioned in triage notes).
- The BDD tag-checker rejects something orthogonal to the cross-file fix.
