# Plan 18 — the doer must reconcile its summary with the actual diff

## Why this plan exists

Plan 17 redesigned the do→scope→check→triage loop with single responsibilities
and rerouted scope-review's input from "prior-attempt triage notes" to "this
attempt's doer summary." The redesign worked as intended on its first live
test against R-0005 attempt 20, but uncovered a deeper bug:

The new triage agent emitted a clean forensic narrative:

> ROOT_CAUSE: Scope reviewer rejected
> `crates/tanren-store/src/migration/m20260504_000001_organization_invitations.rs`
> because it was outside the declared web-only lane and the doer's summary
> does not give a concrete gate-fix justification for it. The doer's summary
> also stated "no Rust production files were touched," which conflicted with
> the modified Rust migration file.

The scope reviewer had done its job; the doer had written a summary that
contradicted the actual diff. Forensics on the worktree:

```
$ cd .quikode/worktrees/r-0005-ced170
$ git status --short
 M apps/web/src/app/lib/account-client.ts
 M apps/web/src/app/page.tsx
 M apps/web/src/i18n/messages/en.json
 M apps/web/tests/bdd/steps/account.steps.ts
 M crates/tanren-store/src/migration/m20260504_000001_organization_invitations.rs
?? apps/web/src/app/organizations/
?? apps/web/src/components/account/InvitationCreateForm.tsx
?? apps/web/src/components/account/InvitationCreateForm.stories.tsx
?? apps/web/src/components/account/InvitationList.tsx
?? apps/web/src/components/account/InvitationList.stories.tsx
$ git diff HEAD --stat
 5 files changed, 631 insertions(+), 69 deletions(-)
```

All of S-07's implementation is *intact* in the working tree (631 lines across
ten files). It has been intact for many attempts. The migration fix at the
bottom is the necessary gate-fix that makes `just web-test` pass.

The doer in attempt 20 looked at the spec, saw all required files already
existed, and wrote: *"All target files already appear to have complete
implementations. Out-of-lane edits: None required. No file changes needed."*
That summary describes what the doer **consciously did this attempt** — which
is "nothing." It does not describe **what the orchestrator is about to commit**
— which includes the out-of-lane migration file from the previous attempt.

## The mechanic that makes this happen

`worktree.py:_apply_lane_review` rolls back a rejected commit with
`git reset HEAD -- .` — that unstages the index but **does not revert the
working tree**. The next attempt starts with: HEAD untouched, index empty,
working tree carrying every file that was in the rejected diff. When the
orchestrator stages with `git add -A` the next time, the same out-of-lane
file goes right back into the index.

This is intentional and correct — reverting the working tree on rejection
would discard hours of legitimate doer work for one bad file. The bug is in
how the doer thinks about its summary: it reports its conscious actions
("I edited X, Y, Z this turn") rather than the actual commit content
("everything `git diff HEAD` will show, including persistent state from prior
attempts").

## What changes

A new section added to `prompts/subtask-doer.md`, just before the existing
Output section, headed **"Before stopping — inspect what will actually be
committed"**. It instructs the doer to run `git status -uall` and
`git diff HEAD --stat` before writing the summary, and for every file in
the diff make one of three explicit decisions:

- In-lane and correct → keep as is.
- Out-of-lane but a legitimate gate-fix → keep, justify in summary.
- Stale / wrong / unjustified → **fix in place** (don't blindly revert without
  checking whether reverting breaks a gate; per the user's standing
  preference, recover work rather than discard it).

The section closes with an explicit prohibition on the failure mode that
caused R-0005 attempt 20:

> Never claim "no out-of-lane edits" or "no changes" when `git diff HEAD --stat`
> shows otherwise.

The existing Output section's framing changes one word: "After implementing,
emit a brief summary…" → "After implementing AND inspecting the diff, emit a
brief summary…" — to reinforce the inspection step is required, not optional.

## What this plan intentionally does NOT do

- **Wipe R-0005's worktree.** The 631 lines of S-07 implementation are
  intact and recoverable; the only problem is one un-justified file in the
  diff. With this prompt fix, the next doer attempt sees the migration in
  its `git diff` output, decides whether to keep + justify it (likely, since
  removing it would panic `just web-test`), and writes a summary the scope
  reviewer can accept. Wiping would re-run S-01–S-06 (8+ hours of work that
  already committed cleanly) for zero gain.
- **Add an orchestrator-side worktree revert on scope rejection.** That
  approach was considered and rejected. Reverting the working tree on every
  scope rejection would discard the doer's good work in proportion to the
  doer's bad work — punishing legitimate gate-fixes the same as overreach.
  The doer-inspects-diff approach preserves work and pushes judgment to the
  agent that has the most context.
- **Add a `git status` snippet to scope-review's prompt.** Scope review reads
  the doer's summary; if the doer's summary is honest about the diff, the
  reviewer has all the information it needs. Putting the diff in two places
  invites them to drift.

## Validation

- `uv run ruff check quikode tests` — clean.
- `uv run ruff format --check quikode tests` — clean.
- `uv run ty check quikode tests` — clean.
- `uv run pytest tests/ -q` — clean.
- Functional: R-0005 attempt 21 (running on the new prompt because Jinja
  reloads templates per render after reinstall) acknowledges the migration
  file in its summary with a concrete `just web-test` cite; scope reviewer
  accepts; S-07-web-surface lands.

## Status

**Shipped** in this commit on `optimizations`.
