# Per-subtask commit + pre-commit gate (proposal)

> **Status (2026-05-02):** IMPLEMENTED in v3-batch3. This document is kept
> as architectural reference. See `quikode/worker.py` (`_subtask_loop`)
> and `tests/test_per_subtask_commit.py` / `tests/test_pre_commit_gate.py`
> for the live behavior.

User idea (2026-05-02): commit and push to the task branch after each
subtask passes its checker, so pre-commit hook failures surface
incrementally per slice instead of accumulating to the end of the task.

## Why this matters

The 2026-05-02 R-0001 incident exposed two problems caused by the
"commit only at the very end" architecture:

1. **Lost work when a task fails mid-flight.** S-01 through S-06 landed
   cleanly but were never committed; they sat as uncommitted edits in
   the worktree. When S-07 hung and the orchestrator killed the task,
   recovering the work required preserving the worktree on disk and
   adding the new `quikode resume` command. Per-subtask commits would
   have made the recovery trivial — `git checkout` to the post-S-06
   commit and pick up from there.

2. **Pre-commit hooks fire too late.** tanren's `lefthook.yml` runs
   `cargo fmt --check`, `clippy -D warnings`, `ruff`, and other
   slice-relevant checks. With one commit per task, these all run after
   8-10 subtasks of accumulated changes. A formatting violation in S-02
   surfaces as a `lefthook` failure during the post-final-check commit,
   2+ hours after the violation was introduced. The triage agent then
   has to reverse-engineer which subtask caused which violation.

   Per-subtask commits surface each violation in its own subtask cycle:
   the doer makes changes, the checker verifies, the commit fires the
   hooks, and a hook failure goes back into the same triage loop with
   the right context.

## Proposed flow

Replace the current per-subtask cycle:

```
   doer → checker (subjective) → triage (on FAIL) → ... → done
```

with:

```
   doer → checker (subjective) → commit → triage (on commit FAIL)
                                       → done
```

The new commit step:
1. Stages whatever the doer changed in the subtask scope.
2. Runs `git commit -m "<task>/<subtask>: <subtask title>"`.
3. lefthook (or whatever pre-commit framework) fires automatically.
4. If hooks pass: commit lands, subtask is DONE, loop continues.
5. If hooks fail: commit aborts (no half-state). The hook output is fed
   to the triage agent as `commit_failure_output`. The doer gets one
   more attempt within the existing subtask retry budget, with the
   triage notes saying "the implementation passed the checker but
   failed lefthook with X / Y / Z — fix these specific lint/format
   issues and try again."

## Key design decisions

### What scope does the commit stage?

Two options:

(a) **Stage everything** in the worktree (`git add -A`). Simple. Risk:
    the doer might have changed files outside the subtask's scope (e.g.
    edited an unrelated crate while exploring). Those leak into the
    subtask's commit.

(b) **Stage only the subtask's `files_to_touch` + their dependents**.
    Cleaner blame trail. Risk: the doer often has to touch files the
    planner didn't anticipate, and we'd lose those changes.

**Recommendation: (a)**. The whole-worktree commit is the simpler
mental model and matches what a human developer would do mid-feature.
The boundary discipline is enforced by the planner (subtask's
`boundary` field) and the checker (per-subtask acceptance), not by the
git index.

### What if the doer made no changes?

Empty commits are a smell — usually means the subtask was vacuous or
the doer stubbed out. Three options:

(a) **Skip the commit** entirely; mark subtask DONE without a commit.
    Clean but loses the audit trail.
(b) **Make an empty commit** with `--allow-empty`. Records the subtask
    boundary in git history.
(c) **Fail the subtask** — empty changes is suspicious enough to
    re-prompt the doer.

**Recommendation: (b)**. Empty commits are cheap, the title carries
the subtask info, and rebases can squash them later if needed. The
checker's PASS verdict is the signal that the subtask is real, not the
diff size.

### When does push happen?

Today: at the end of the task, once. With per-subtask commits we have
options:

(a) **Push every subtask commit immediately.** Each subtask becomes
    visible in the GitHub PR (or pre-PR branch) as a separate commit.
    Risk: 8-10 force-pushes per task as the branch evolves.
(b) **Push only at task completion** (current behavior). Subtasks land
    as a single push. Less GitHub noise.
(c) **Push at the end of each subtask but only if a PR is already
    open.** Hybrid — early subtasks accumulate locally, push starts
    once the PR exists.

**Recommendation: (b)**. Per-subtask local commits are the value;
intermediate pushes don't add much and create review noise.

### What if a hook fails in a way the agent can't fix?

Some hooks need credentials (gpg signing), system tools (a specific
binary version), or external services. If they fail and the agent
can't fix them in 1-2 retries, the subtask is BLOCKED. The user has
to investigate.

**Mitigation**: the existing `quikode show <id>` already surfaces the
last triage notes; we just need to format `commit_failure_output`
into them clearly. No new infrastructure needed.

### Resume interaction

`quikode resume <id>` becomes more powerful with per-subtask commits.
Today it preserves uncommitted edits in the worktree. With per-subtask
commits, resume can `git checkout <last-committed-subtask-tip>` and
pick up cleanly, even after the worktree was deleted. Worth keeping
the existing resume code path as a fallback for old workspaces.

## Implementation sketch

Worker change:

```python
# In _subtask_loop, after checker PASS:
if verdict is Verdict.PASS:
    commit_ok, commit_output = self._commit_subtask(subtask)
    if not commit_ok:
        # Treat hook failure as a per-subtask FAIL — feed to triage
        triage_notes = self._triage_subtask_commit_failure(
            subtask, attempt, budget, commit_output
        )
        self.store.increment_subtask_retries(...)
        continue  # next attempt
    self._mark_subtask_done(subtask)
    settled = True
    break
```

New `_commit_subtask`:

```python
def _commit_subtask(self, subtask: Subtask) -> tuple[bool, str]:
    """Stage everything in the worktree and commit. Returns (ok, output).
    On hook failure, output is the lefthook/pre-commit stderr."""
    msg = f"{self.node.id}/{subtask.id}: {subtask.title}"
    rc1, _, _ = exec_in(
        self._h, ["git", "-C", "/workspace", "add", "-A"]
    )
    if rc1 != 0:
        return False, "git add -A failed"
    # --allow-empty: empty subtasks still record a boundary.
    rc2, out, err = exec_in(
        self._h,
        ["git", "-C", "/workspace", "commit", "--allow-empty", "-m", msg],
    )
    if rc2 != 0:
        return False, (out + "\n" + err)[:4000]
    return True, ""
```

New triage prompt template `subtask-triage-commit-failure.md`:

```markdown
You are the **commit-failure triage** for a subtask. The doer made
changes, the checker passed them, but `git commit` failed because
pre-commit hooks (lefthook / .pre-commit-config.yaml) caught a
violation.

## Subtask
ID: {{ subtask.id }}
Title: {{ subtask.title }}

## Hook output
```
{{ commit_output }}
```

## What to emit
ROOT_CAUSE: ...
WHAT_TO_DO_DIFFERENTLY: ...
```

Schema additions: a new column on `subtasks` for `last_commit_failure`
(text) so we can surface it in `quikode show`. Same migration pattern
as the existing _migrate.

## Risks + tradeoffs

- **Bigger fixed cost per subtask.** Adds ~5-30s per slice for the
  commit + hook execution. Across 10 subtasks that's a couple of
  minutes. Worth it.
- **Hook flakiness.** If a hook is itself flaky (network calls,
  external linters that timeout), subtask retries multiply. Mitigation:
  same retry budget as the doer, plus a flag to disable hooks
  (`--no-verify`) that the triage agent can recommend if the failure
  is environmental.
- **Branch divergence on resume.** If resume picks up a worktree where
  the last commit doesn't match what the store thinks is "done", we
  might re-commit + re-add changes. Solution: post-resume sanity check
  — the worker reads `git log --oneline` and reconciles against the
  store's done-subtask list before starting.

## v1 scope vs deferred

**v1**: per-subtask commit with `git add -A` + `--allow-empty`. Hook
failure feeds triage. Push deferred to end of task. ~150 LOC.

**v1.1**: dedicated commit-failure triage prompt (separate from
checker triage so the agent has the right context).

**v2**: per-subtask push when PR is already open. Stacked-diff
integration so individual subtask commits become individual review
units.

## Recommendation

Build v1 right after R-0001 lands. The architecture problems are real
and recurring. The fix is small. The dividend (recoverable runs +
faster lint feedback + better history) is large.

Sequence: R-0001 retry with claude → ccusage uniform refactor → DAG
viewer → per-subtask commit. Or: per-subtask commit before DAG viewer
since the next R-* run benefits immediately.
