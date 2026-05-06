# Plan 14 — checker fails on observed failures, never fabricated ones

## Why this plan exists

Plan 12 strengthened the subtask checker prompt with this clause:

> "If the subtask's stated acceptance criteria don't already cover these
> runtime checks, add a synthetic FAIL bullet citing the missing runtime check.
> The doer's next attempt will pick it up."

I added that to close a hole I'd just seen with R-0005's broken migration: a
migration subtask "passed" because it compiled, even though the migration
panicked at runtime. The intent was good — make the checker more thorough.

The unintended consequence: the checker started **manufacturing acceptance
criteria the planner never wrote**, and the doer couldn't satisfy them.

Concrete case: R-0021 S-08-bdd-B-0030 hit attempt 11 with this checker output:

> "[FAIL] synthetic runtime invariant for surface-touching subtasks
> (api/web/cli/mcp/tui startup against fixtures): acceptance criteria do not
> explicitly require or prove per-surface startup/runtime checks beyond BDD
> assertions, so required runtime invariant is missing per checker hard rule."

Eight stated acceptance criteria PASSED. The "surface startup" criterion was
not in the planner's spec. It was the checker auto-generating it from my new
prompt. Tanren's `WebHarness`/`TuiHarness` deliberately delegate to
`InProcessHarness` (documented in `docs/architecture/subsystems/behavior-proof.md`
as a known interim design); the checker's synthetic criterion implicitly
demanded a tanren-wide harness rewrite, far outside R-0021's scope.

R-0019 and R-0020 BDD subtasks passed BEFORE this prompt change. Same
architecture, same harness, same delegation pattern — they just didn't trip the
fabricated criterion.

## What "fail on observed failures" means

The checker should:

1. Verify the **planner's stated acceptance criteria** are met. PASS or FAIL
   each.
2. Re-run the gate the doer was told to run (`just check` / etc.) and confirm
   it exits 0. If it doesn't, FAIL with a real gate-output cite.
3. Notice runtime invariants the gate doesn't enforce — *but only when there is
   evidence on this branch the invariant is being violated*. (E.g. if running
   `cargo run -q tanren-cli -- migrate up` would panic, FAIL with the panic
   stack as evidence. NOT "the criteria don't promise migration runs, so I'm
   demanding it.")
4. Never invent criteria from whole cloth.

## Updated prompt language

Replace the "synthetic FAIL bullet" clause with:

> "Do NOT fabricate criteria the planner didn't write. If the planner's
> acceptance set under-specifies runtime exercise, it's tempting to add a
> synthetic bullet — DON'T. That makes the subtask un-passable. Instead: if
> you can verify a real gate failure on this branch, fail on THAT. If the gate
> passes and the planner's criteria are met, return PASS even if you suspect a
> deeper issue — the audit gauntlet's full `just ci` will catch it pre-PR.
> 
> The principle: **fail on real, observed failures; don't fail on hypothetical
> ones**. A synthetic-criterion FAIL the doer can't fix sets up the exact
> retry-loop quikode is designed to avoid."

## R-0021 disposition

After this fix lands and reinstalls, R-0021's next S-08 attempt should pass —
all 8 real criteria already PASS in attempt 11; only the fabricated ninth
criterion fails. Reinstall complete; daemon picks up the new prompt on next
checker invocation.

## Lesson

Strengthening prompts in one place often weakens behavior somewhere else. The
"no CI failure leaks to main" invariant is real, but its enforcement point is
the **audit gauntlet** (the full `just ci` run pre-PR), not the per-subtask
checker. The per-subtask checker should be a fast, targeted verification of
the slice's planner-stated contract; pushing it to be a second auditor breaks
the ecosystem.
