# Plan 17 — clean-slate prompt architecture for the do→scope→check→triage loop

## Why this plan exists

R-0005's `S-07-web-surface` reached **18 attempts over 9+ hours** of wall-clock,
including an operator-mediated `BLOCKED → pending` transition with a
"scope-expansion prompt update" that didn't unstick it. The structural deadlock
was visible in the artifacts as soon as `qk show` was readable again
(plan 15):

- The acceptance checker's verdict was **PASS** — every criterion met,
  `just web-test` green, BDD green.
- The scope reviewer was rejecting the commit because the diff included
  `crates/tanren-store/src/migration/m20260504_000001_organization_invitations.rs`,
  outside the subtask's `files_to_touch`.
- The migration edit was **necessary**: without it, `just web-test` panics on
  migration up.
- The prior attempt's triage agent had written
  `WHAT_TO_DO_DIFFERENTLY: remove the migration fix from this subtask; land it
  separately first.`
- The next doer attempt either followed that advice (→ web-test panic →
  checker fail) or ignored it (→ scope reviewer rejected on the strength of
  the triage notes saying "remove this" plus the doer keeping it).

That is an oscillation. The retry budget burns to zero. None of plans 12, 13,
or 14 fixed it because each layered *more* invariants onto an already-overloaded
prompt set. Adding rules wasn't the answer; the responsibilities were *crossed*.

## Diagnosis: who's doing whose job

The four agents in this loop and what each *should* own:

| Agent | Single responsibility |
|---|---|
| **doer** | Implement the subtask. Run gates. Note out-of-lane edits + reasons in summary. |
| **scope reviewer** | Lane-discipline judge. In-lane, or legitimately out (gate-fix / generated / companion)? |
| **acceptance checker** | Verify each acceptance criterion PASS / FAIL. |
| **triage** | Forensic root-cause narrative when any of the above fails. |

The actual prompts had crossed wires:

- **scope reviewer** read **prior-attempt triage notes** to detect gate-fix
  intent. But triage notes are about a *different* attempt — they reflect
  what last time looked like, not what *this* commit's doer was trying to do.
  When the prior triage said "remove the migration fix" and the current doer
  kept it anyway, the reviewer saw two contradictory signals from agents
  *other* than the doer of the diff in front of it.
- **triage** had a `WHAT_TO_DO_DIFFERENTLY` section that prescribed specific
  files to add or remove. That's both *scope* policy (the scope reviewer's
  job) and *implementation* policy (the doer's job). Triage was telling the
  doer how to change scope — which the next attempt's scope reviewer would
  then read as authoritative — short-circuiting the doer's own judgment.
- **acceptance checker** ran the full gate (`just check` etc.) as a "Hard
  invariant: no broken artifact passes" override. But the **objective check**
  already runs `just check` mechanically before the LLM checker is even
  invoked. The checker was duplicating the objective check's work and
  occasionally fabricating synthetic FAIL criteria when it thought the
  planner under-specified runtime exercise (the bug plan 14 walked back).
- **doer** had *some* of the right invariants ("no CI failure leaves your
  branch", "files_to_touch is the default scope, not a hard prohibition")
  but no clear instruction that the **doer's summary** was the place to
  justify cross-file edits.

## What changed

Four prompts rewritten clean from the responsibility map above, plus one
data-flow correction.

### `prompts/subtask-doer.md`

- Two non-negotiable invariants: gate must be green; never rewrite git
  history. The "no CI failure leaks" rule is stated once, plainly, and the
  formatter-fix-mode list is folded under it.
- The summary is explicitly framed as **authoritative for the scope
  reviewer's intent judgment**. The doer is told to list each out-of-lane
  edit with a concrete cite (specific gate / test / panic) — handwaving will
  be rejected, a concrete cite will be accepted.
- BDD slice rules compressed to a single paragraph + reference doc. The
  validator is fast and self-explanatory; the prompt doesn't need to
  re-encode every rule.
- Triage feedback (when present) is framed as *context, not a fix recipe* —
  read it, then apply your own judgment. No "authoritative" framing.

149 → 88 lines.

### `prompts/scope-review.md`

- Reads **the doer's summary of THIS commit** (new input). Drops the
  prior-attempt triage notes parameter entirely.
- Single judgment rule, stated plainly: for each out-of-lane file, did the
  doer's summary name a concrete reason? Yes → legitimate. Silent or
  hand-waving → overreach.
- Lenient examples retained (auto-gen outputs, lint refactors, companion
  files, gate-fixes). Cross-file gate-fixes are described as "doer obliged
  to fix any gate failure regardless of file" without a separate "Hard rule"
  block — the judgment rule covers it.
- "Be specific in the rejection reason" so the next doer attempt knows
  exactly which files lacked justification, and can either drop them or
  document the rationale.

118 → 82 lines.

### `prompts/subtask-checker.md`

- Single responsibility stated up front: verify acceptance criteria against
  the working tree. Does NOT run the full gate (the objective check already
  did and passed, otherwise the LLM checker wouldn't have been invoked).
  Does NOT judge scope. Does NOT prescribe fixes.
- The "don't fabricate criteria" invariant from plan 14 is preserved as the
  central "How to verify" guidance.
- Output simplified — no `ROOT_CAUSE` line in the FAIL output. The cited
  evidence on each criterion is the entire signal; the triage agent
  composes the narrative.

66 → 51 lines.

### `prompts/subtask-triage.md`

- Reframed in the opening: "**root-cause investigator**". You are NOT a
  gate. You do NOT prescribe code edits. You do NOT decide pass/fail.
- Explicit instruction to identify which layer failed (objective gate /
  acceptance checker / scope reviewer / commit transport) and the specific
  signal — that's the entire output.
- Output schema is now `ROOT_CAUSE: ... CONFIDENCE: ...` only. The
  `WHAT_TO_DO_DIFFERENTLY` section is removed by name and explicitly
  forbidden in a "Forbidden in your output" list.
- "Hard invariant: no CI failure leaves the branch" removed — that's doer
  policy, not triage policy. Stating it in two prompts let the prompts
  drift.

66 → 60 lines.

### `quikode/scope_review.py` + callers

- `review_scope_drift` parameter `triage_notes` → `doer_summary`.
- `workers/subtask_completion.py:_handle_subtask_pass` reads
  `self.last_doer_summary` (already populated by the subtask execution
  mixin from the doer's stdout) and passes it to the lane review.
- `workers/subtasks.py:_handle_passed_subtask` and `_handle_subtask_pass`
  no longer carry `triage_notes` — they didn't need it for any other
  purpose.
- One test signature in `tests/test_subtask_block_stops_loop.py`
  (`fake_pass`) updated to drop the now-removed parameter.

## How the R-0005 oscillation breaks under the new design

Replay attempt 17 with the new prompts:

1. **Doer** writes the web-surface diff plus the migration fix. Summary
   says (per the new rule):
   > Out-of-lane: `crates/tanren-store/src/migration/m20260504_000001_organization_invitations.rs` —
   > required because `just web-test` panics on migration `up` without the
   > one-column-per-`alter_table` split (sqlite incompat).
2. **Objective check** runs (`just check`). Passes — migration fix is in.
3. **Acceptance checker** runs. Each criterion PASS with cited evidence.
4. **Scope reviewer** runs. Sees the migration file out of lane; reads the
   doer's summary, finds a concrete cite naming `just web-test` panic;
   marks legitimate.
5. Commit lands. Loop done.

The oscillation can't recur because:

- Triage no longer says "remove file X" — it just describes what failed.
- Scope reviewer no longer reads triage notes from a *different* attempt as
  the source of truth on intent — it reads the *current* doer's summary,
  which is the actual contemporaneous record.
- Doer is told explicitly to put gate-fix justifications in the summary,
  with a concrete-cite-vs-handwaving threshold the reviewer will use to
  judge.

## What this plan intentionally does NOT do

- **Reorder the loop.** The actual code order remains
  `do → objective_check → acceptance_check → pre_commit_gate → scope_review → commit`.
  Reordering is a code change with its own risks (e.g. running scope review
  before the LLM checker would mean a passing commit has to be re-staged
  later). The prompt rewrites alone close the oscillation; reordering is
  not necessary.
- **Add new agents or remove existing ones.** Same four agents, just with
  cleaner responsibilities.
- **Touch `prompts/doer.md`, `prompts/checker.md`, `prompts/triage.md`** —
  those are the higher-level *task-level* prompts, not the per-subtask loop
  this plan addresses.

## Validation

- `uv run ruff check quikode tests` — clean.
- `uv run ruff format --check quikode tests` — clean.
- `uv run ty check quikode tests` — clean.
- `uv run pytest tests/ -q` — clean.
- Functional: after reinstall + daemon restart, watch R-0005 (currently on
  attempt 18 mid-flight) reach a clean commit instead of cycling. The new
  prompts are loaded fresh per render via Jinja's `FileSystemLoader`, so
  the next agent call after reinstall picks them up; the python signature
  changes require the daemon restart.

## Status

**Shipped** in this commit on `optimizations`.
