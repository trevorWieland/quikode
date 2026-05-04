# Runbook validation — 2026-05-03 findings

Run-through with strict adherence to `runbook-operations.md`. Goal was 3
consecutive autonomous fixture runs. Run 1 surfaced enough issues that
Runs 2+3 were aborted in favor of documenting findings.

## Result

- **Run 1**: 2/4 cleanly merged (T-002, T-004 reached AWAITING_MERGE).
  T-001 ended ABORTED (PR auto-closed by github with 0 changed files
  after a spurious rebase ate its changes). T-003 was mid-flight when
  the run was stopped.
- **Real bugs found and fixed in worktree.py**:
  1. Concurrent `git fetch origin main` race on parent repo (lock
     contention) — `fetch_base` now retries with backoff on
     `cannot lock ref` markers.
  2. `commit_subtask` "nothing to commit + branch ahead of base" was
     an infinite retry loop (each triage spawned a doer that did
     pointless soft-resets) — now treated as idempotent re-entry
     success.
- **Real bugs found, NOT fixed yet** (need investigation):
  3. T-001's `_poll_pr_loop` triggered a spurious rebase against an
     unchanged main, ran the conflict_resolver, and ended with 0-diff
     vs main — losing T-001's commits. Mechanism unclear; likely
     either github reporting transient CONFLICTING on a freshly-opened
     PR, or the rebase --onto semantics with empty/null parent_branch
     producing wrong results.
  4. After (3), github auto-closed the empty PR. The daemon correctly
     transitioned T-001 to ABORTED. But the trigger condition (empty
     PR after rebase) is the bug.

## Runbook gaps surfaced

### Gap 1: no "fixture full-E2E validation" workflow

`runbook-operations.md` §"Starting a tanren run" says daemon + parallel.
§"For a fixture smoke test" says `quikode run --max-parallel 1` (serial)
or `quikode dev-test` (T-001 only smoke). Neither describes the
full-DAG-with-stacked-children-and-sibling-conflicts validation that
the prior 3-run E2E exercised.

**Patch**: add a new section `## Fixture full E2E validation` describing:
- daemon mode is fine for fixture too, just use the same path as tanren
- expected interaction points (post review on T-001, merge T-002, watch
  stacked rebase on T-003)
- expected end state (4/4 merged with no manual intervention)
- between runs: revert main on the fixture github repo OR start with
  pre-existing merges and accept that some tasks become no-ops

### Gap 2: no "fixture github main state between runs" guidance

After Run N completes, fixture's main has T-001..T-004 merged. For Run
N+1 to have anything to do, main must be reverted to the baseline
(just `/health`). `runbook-operations.md` §"Routine maintenance"
describes `quikode reset` for the workspace but nothing about the
fixture's github repo state.

**Patch**: add to operations runbook a `## Fixture between-run reset`
subsection with the manual `git revert --no-edit <merged-shas>` recipe.
Or: ship a `quikode fixture-reset-main` helper that does the reverts.

### Gap 3: no "subtask retrying without flatline" symptom section

`runbook-incident-response.md` covers BLOCKED, FAILED, daemon down,
review ignored, PR auto-closed, rebase storms, RESPONDING_TO_REVIEW
stuck, ccusage anomalies. But not: "task is in subtask loop attempt
6+, not BLOCKED yet, costs are mounting." The progress check should
catch flatline but in practice it returned PROGRESSING/UNCERTAIN even
on the runaway loop in Run 1 (the doer was making content changes,
just identical content under different SHAs — the agent considered
that "progress").

**Patch**: add `## Symptom: subtask retrying without convergence` to
incident-response. Include:
- check `quikode show <id>` for retry count + per-attempt root_cause
- if root_causes are repeating verbatim: the progress agent's heuristic
  is failing → manually `quikode retry <id>` after killing the daemon
- if root_causes are different but cost is mounting: this is normal
  retry within budget; let it run unless it hits hard ceiling
- check `progress_checks` table for what the agent has been seeing

### Gap 4: no "worktree missing / corrupted" symptom

When a task's `worktree_path` row points to a directory that doesn't
exist on disk (rare, but observed in Run 1 for T-001), the worker's
next operation crashes. `recover_orphan_tasks` handles this on startup
but not mid-run.

**Patch**: add `## Symptom: worktree gone but task active` to
incident-response. Include:
- `ls .quikode/worktrees/` to verify
- `quikode retry <id>` (full reset is safest)
- For preservation: `quikode show <id>` to see prior plan_text + commits

### Gap 5: runbook says "post review via gh pr review --comment" but the actual API is more nuanced

The runbook §"Reviewing PRs" says `gh pr review --comment --body "..."`
triggers the response cycle. **In practice that produces a top-level
review-body comment, NOT an inline review-thread**. The watcher polls
graphql `reviewThreads` which only includes inline-comment threads.

**Patch**: clarify in runbook that for the response cycle to fire, the
review must include at least one inline comment via either:
- `gh api -X POST .../pulls/<n>/comments -f body=... -f commit_id=... -f path=... -F line=N -f side=RIGHT`
- The GitHub UI's "Start a review" → "Add a single comment" on a specific line

A bare `gh pr review --comment` without inline comments doesn't trigger
the review-watcher.

### Gap 6: no "fixture validation requires --max-parallel ≥ 2" warning

Stacked diffs, sibling-CONFLICTING auto-rebase, auto-merge, and most
v3 features only exercise under parallelism. Running fixture smoke
with `--max-parallel 1` (the runbook default for fixture) provides
much weaker coverage than the user-intended validation.

**Patch**: in operations runbook, recommend `--max-parallel 3` for
"validation runs" specifically (separate from "smoke test runs" which
can stay --max-parallel 1).

## Code bugs found

### Bug 1 (FIXED): concurrent git fetch race

`worktree.py:fetch_base` called `git fetch origin main` with `check=True`.
3 workers spawning in parallel raced on `.git/refs/heads/main` lock.
One won, others got `error: cannot lock ref ...`, raised
`CalledProcessError`, task FAILED.

**Fix**: retry up to 3 times on lock-contention markers with short
backoff. Real auth/network failures still fail fast.

### Bug 2 (FIXED): commit_subtask "nothing to commit but ahead" loop

When a subtask's work was already committed in a prior attempt (e.g.
across orphan-recovery), the worker re-ran the subtask, the doer made
identical edits, the checker passed, but `git commit -m ...` returned
"nothing to commit, working tree clean". The worker treated this as a
real failure → triaged → doer tried `git reset --soft HEAD~1 &&
git commit` → loop forever, burning $0.13 in triage agent calls per
iteration.

**Fix**: when `git commit` fails with "nothing to commit" AND the
branch is ahead of main, treat as idempotent re-entry success. Push
the existing HEAD (no-op if already pushed) and mark subtask DONE.

### Bug 3 (NOT FIXED): spurious worker-side rebase on freshly-opened PR

T-001 went `pr_opening → polling_ci → rebasing → conflict_resolving →
checking → polling_ci → aborted`. The trigger for `polling_ci →
rebasing` is `mergeable == "CONFLICTING"`. But T-001 was just opened
and main was unchanged; CONFLICTING shouldn't have been reported.

Hypothesis: `gh pr view` may report `mergeable=UNKNOWN` initially, and
some path in the worker treats UNKNOWN as CONFLICTING. Or the worker
is triggering on a different signal.

**Investigate**: trace the exact transition trigger. If `mergeable=UNKNOWN`
is being treated as needing rebase, fix the predicate to require
`mergeable == "CONFLICTING"` strictly. If something else, find it.

### Bug 4 (NOT FIXED): empty-PR after spurious rebase + github auto-close

After Bug 3's spurious rebase + conflict-resolver, T-001's branch
ended up with 0 commits ahead of main (the resolver may have wrongly
accepted main's version). github auto-closed the empty PR; daemon
correctly aborted.

**Fix sequence**: fix Bug 3 first (the rebase shouldn't have fired).
Add a defensive check at the end of any rebase: if the rebased branch
has 0 commits ahead of base, something went wrong — abort the rebase
and BLOCK with a clear note instead of letting it land.

## Recommendation

Before another validation pass:

1. Fix Bug 3 + Bug 4 (~2-4 hours of investigation + targeted fix).
2. Patch the 6 runbook gaps above (~30 min of doc edits).
3. Then re-run the 3-run validation cleanly.

Bugs 1-2 are already patched in worktree.py. The runbook patches and
Bugs 3-4 fixes should land as one batch.

## Final state of Run 1 (for reference if resuming)

```
T-001  ABORTED  (PR #34 auto-closed; worktree deleted by daemon)
T-002  AWAITING_MERGE  PR #33  · $0.76
T-003  CHECKING_SUBTASK  (mid-flight when daemon stopped)
T-004  AWAITING_MERGE  PR #32  · $0.25
```

To resume after fixes: `quikode reset --yes --close-prs` + revert
main + restart from scratch.
