# Plan 12 — bake the "no CI failure leaks to main" invariant into every prompt

## Why

R-0005 BLOCKED at S-07-web-surface attempt 15 because:

1. R-0005's S-02 committed a SQLite migration with chained `add_column` in one
   `ALTER TABLE` (SeaQuery rejects it on SQLite — only postgres supports it).
2. S-02's per-subtask checker passed it because `just check` doesn't actually run
   migrations against a DB; it just compiles.
3. S-03..S-06 didn't run migrations either, so the bug stayed hidden.
4. S-07 ran `just web-test`, which invokes Playwright, which runs `cargo run -p
   tanren-cli -- migrate up`, which panicked on the bad migration.
5. The triage agent correctly identified the cause as the migration file in
   `crates/tanren-store/src/migration/...`. But the file was NOT in S-07's
   `files_to_touch`, so the triage agent told the doer to "mark this subtask
   blocked on the migration owner."
6. The doer obeyed. The progress agent flatlined after 3 cycles. BLOCKED.

There is no "migration owner". This task IS the owner of every commit on its
branch. The "blocked-on-upstream" framing was a prompt-level dead-end, not a real
constraint.

## The invariant

Every quikode-managed prompt now states the same thing in domain-appropriate
language:

> The orchestrator's contract with `main` is that **no CI failure, panic, test
> failure, type error, lint error, or migration error EVER leaks to `main` from
> a quikode branch**. There is no "pre-existing failure" exemption. There is no
> "out-of-scope" exemption. There is no "upstream owner". The branch is the
> task's; every commit on it is the task's.

## Prompt edits shipped tonight

- `prompts/subtask-doer.md` — added a "Hard invariant" section after `files_to_touch`.
  Explicitly authorizes editing files outside the list when needed to keep gates
  green. Forbids "blocked on upstream" reasoning.
- `prompts/subtask-triage.md` — replaced the "out-of-scope" advisory clause with
  a "Hard invariant" clause. Forbids the phrases "blocked on owner",
  "out-of-scope", "pre-existing", "upstream fix needed" in triage output.
- `prompts/subtask-checker.md` — added runtime-exercise requirements: migration
  subtasks must actually run the migration; fixture subtasks must verify
  initialization; surface subtasks must verify startup. Compile-time presence is
  necessary but never sufficient.
- `prompts/planner.md` — added two implications for plans: (a) acceptance
  criteria must include runtime exercise where applicable; (b) don't sequence so
  that S-02's correctness depends on S-07 fixing it later.
- `prompts/progress.md` — added an anti-pattern guard: triage rounds that keep
  citing "blocked on owner" / "out-of-scope" / "pre-existing" are by definition
  flatlined, return that verdict.

## R-0005 disposition

`qk resume R-0005` was issued after the prompt update. The 4 non-done subtasks
(S-07, S-08, S-09, S-10) are queued; S-07 will pick up the new prompt on its
next attempt and (we expect) actually fix the migration. If it BLOCKs again on
the same root cause despite the new prompt, that's a real signal that the
prompt change isn't enough and the next layer (e.g. an explicit cross-subtask
fixup mechanism in the FSM, plan 05's poisoned-worktree-wipe) is needed.

## Code-side follow-ups (NOT in this plan; tracked separately)

- Plan 06 (deterministic locality fingerprint): would have caught the loop
  earlier with less wasted budget — but the right behavior here was always to
  fix the migration, not to flatline-and-block faster.
- Plan 05 (auto poisoned-worktree wipe): does NOT apply here — the poison was
  the migration, which is the *correct* commit; we want to fix it, not throw
  it away.
- Plan TBD: per-subtask gate could shell out to `cargo run -p tanren-cli --
  migrate up` against a throwaway DB when the subtask's diff includes
  `crates/tanren-store/src/migration/`. Profile-aware, not generic — defer to
  prompt-level coverage for now.

## Operator handoff

If the user reads this in the morning and R-0005 is still BLOCKED at S-07 with
the same migration-panic root cause:

- The new prompts didn't move the doer enough; this is evidence that prompt-only
  changes aren't sufficient to overcome strong scoping bias in the doer model.
- Next escalation: directly fix the migration in
  `.quikode/worktrees/r-0005-ced170/crates/tanren-store/src/migration/m20260504_000001_organization_invitations.rs`
  (split the chained `ALTER TABLE` into one `add_column` per call) and
  `qk resume R-0005`. The doer's NEXT attempt sees the gate already green.

If R-0005 made progress past S-07: the prompt change worked, file the win in
the morning notes.
