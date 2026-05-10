# Plan 56 — auto-detect "merged via foundation integration" by ancestry check

## Why

The operator increasingly uses a release-batch workflow: pull N AWAITING_REVIEW
PRs into a local integration branch, opinionate the merge, run `just ci`
against the unified state, then merge the integration branch to main. The
constituent PRs end up CLOSED on GitHub (their commits are in main, but
GitHub never saw a "Merge" button click — they were closed manually).

Currently quikode's post-PR FSM polls GitHub for `pr.merged_at`. When it
sees `pr.state=CLOSED, merged=false`, the task enters an unhappy path
(treated as "closed without merge"). The operator has to follow each
integration push with `qk mark-merged R-XXXX` for each PR in the batch
to force the right state — easy to forget, scales poorly with cadence,
and is the kind of manual chore that a sustained release-review pattern
makes painful.

Quikode CAN detect "this PR's commits are now in main" without going
through GitHub's merge flag: `git merge-base --is-ancestor <task_branch_tip>
origin/main`. If true, the PR's work IS in main regardless of HOW it
got there.

## What ships

### Worker-level ancestry check

`quikode/workers/pr_lifecycle.py` (or wherever the PR-state poller lives —
search `_handle_polled_pr_state` / `_handle_pr_closed_without_merge` /
similar):

When the GitHub poll reports `pr.state=CLOSED, merged=false`:

1. Before transitioning the task to the closed-without-merge unhappy
   state, run an ancestry check:
   ```python
   branch_ref = task.branch  # the task's quikode/r-XXXX branch ref
   branch_tip = run_git(["rev-parse", branch_ref], cwd=task.worktree_path)
   if not branch_tip:
       return existing_closed_without_merge_path
   result = run_git(
       ["merge-base", "--is-ancestor", branch_tip, "origin/main"],
       cwd=task.worktree_path,
       check=False,
   )
   if result.returncode == 0:
       # Commits ARE in main — treat as merged regardless of GitHub flag.
       fsm_runtime.enter_merged(...)
       log.info("task %s: PR #%d closed but commits in main → auto-marked merged via ancestry", ...)
       return
   # Commits not in main → fall through to existing closed-without-merge handling
   ```

2. Run `git fetch origin --quiet` (or some lightweight refresh) before
   the ancestry check so we're comparing against the actual current
   `origin/main`, not a stale local view. Throttle if needed (one fetch
   per polling cycle, not per task).

3. If the ancestry check passes, mark the task MERGED via the same FSM
   path `qk mark-merged` uses. Persist a note explaining why
   ("auto-merged: PR closed without merge, but commits are ancestors of
   origin/main; release-integration pattern detected").

4. If the ancestry check FAILS (commits genuinely missing), continue
   the existing closed-without-merge handling — the operator did intend
   to abandon the work.

### New CLI: `qk detect-merged`

Operator-facing dry-run that walks every open / closed-without-merge
task and reports which would be auto-merged by the ancestry check.
Useful for verifying behavior + one-shot retroactive marking of
already-integrated PRs.

```
qk detect-merged              # dry-run; lists tasks + their ancestry status
qk detect-merged --apply      # actually fire enter_merged for each ancestry-match
```

### Configuration knob

`auto_detect_merged_via_ancestry: bool = Field(default=True)`. On by
default (safe — only fires when ancestry check passes). Operator can
disable for workflows where closed-without-merge should ALWAYS block
(e.g., regulated environments).

### Tests

- `tests/test_workers_pr_lifecycle_ancestry.py` (new):
  - Closed PR + branch tip IS ancestor of origin/main → task transitions
    to MERGED with the right note
  - Closed PR + branch tip NOT ancestor → existing closed-without-merge
    handling preserved (no false-positive auto-merge)
  - Closed PR + branch ref missing (worktree wiped) → falls through to
    existing handling
  - `auto_detect_merged_via_ancestry=False` → ancestry check skipped
- `tests/test_cli_detect_merged.py` (new):
  - dry-run mode reports each task's ancestry status without applying
  - `--apply` mode fires enter_merged for ancestry matches, no-ops for
    non-matches

### Plans index + orientation

- Add plan 56 row to `plans/00-INDEX.md`.
- `orientation.md` §7 invariants: new bullet noting that PR-close-without-
  GH-merge is auto-detected via ancestry when configured (default on);
  release-batch workflow no longer requires per-PR `qk mark-merged`.

## Operational followup (manager handles)

After agent ships:
1. Validation ladder green.
2. Commit + push + reinstall.
3. Daemon restart picks up the new behavior.
4. Next release-batch review by the operator becomes:
   - Integrate locally
   - Push main
   - `gh pr close` each constituent PR with a comment
   - Wait one poll cycle; quikode auto-marks them MERGED via ancestry
   - Stacked descendants unblock naturally

## Out of scope

- Auto-detecting "main was force-pushed to a different SHA but the
  PR's history is still semantically equivalent" — too clever, the
  ancestry check is a strict guarantee that the operator can reason
  about.
- Notifying via ntfy when an auto-detect-merge fires; the existing
  review-ready ntfy push (plan 30) covers "task settled in
  awaiting_review" but a "task auto-merged" signal would be useful.
  Plan 57 candidate.
