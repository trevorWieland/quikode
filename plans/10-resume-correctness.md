# Plan 10 — orphan-recovery and resume correctness

## What works today

`store.recover_orphan_tasks()` (store_review.py:212–272) runs at the start of every
`qk run`. The supervisor calls `qk run` on each crash-restart cycle, so a daemon
restart auto-recovers in-flight tasks:

- PROVISIONING → PENDING (clear branch/wt/cid, retry from scratch)
- PLANNING → PENDING (preserve plan via resume marker if set)
- DOING_SUBTASK / CHECKING / TRIAGING_SUBTASK → PENDING + resume marker
- COMMITTING / PUSHING → PENDING + resume marker
- PR_OPENING → PENDING_CI if pr_number set, else PENDING
- TRIAGING_FEEDBACK / ADDRESSING_FEEDBACK → PENDING_CI
- REBASING_TO_MAIN → PENDING_CI if pr_number, else PENDING

That's the right behavior. Three fragile spots:

## A. Worktree state isn't validated post-recovery

After recovery, the next `_run_one(task_id)` enters TaskWorker. If the recovery target
expects an existing worktree (e.g. PENDING_CI for a task with pr_number), the worker
expects the worktree dir at `cfg.worktree_root/<task-slug>` to be intact. But:

- A `qk reset` between supervisor start and supervisor next-tick could have removed it.
- The worktree could be inconsistently left behind from a crash mid-rebase (rebase-todo
  files still present).
- The branch on disk could have diverged from what `branch` column says.

**Fix.** Add a worker-startup probe `_validate_or_reconstruct_worktree()` that:

1. Confirms `worktree_path` exists and is a valid git worktree.
2. Confirms branch matches what the row claims.
3. If missing or wrong: `git worktree remove --force` + `git worktree add -b <branch>
   <path> <pr_remote>/<branch>`. (Branch must exist remotely; if not, fall through to
   PENDING and let planning rerun.)

Already partially exists at `task_worker.py:282–307`. Audit and harden — current code
allows divergence-recovery but not "branch has been deleted upstream by another reset".

## B. Resume markers can encode stale planner output

When DOING_SUBTASK → PENDING + resume marker, the marker says "skip planning, jump
into subtask N". But if the planner output stored in the DB references files that no
longer exist (because main moved on while the daemon was dead), the doer prompt will
have stale `files_to_touch`. The doer creates new files at the wrong paths; checker
fails.

**Fix.** On resume, validate stored planner output against the current `origin/main`:
for each subtask's `files_to_touch`, if the file doesn't exist locally, log a warning
and either (a) re-plan, or (b) remove that file from the list. Conservative default:
re-plan when more than 25% of subtasks have stale file references.

## C. Container ID is cleared but subtask DB rows aren't

`recover_orphan_tasks()` sets `container_id=NULL`. But the `subtasks` table has
per-attempt rows with their own state (DONE / IN_PROGRESS / FAILED). On resume, the
worker reads "this subtask is IN_PROGRESS" and assumes it should resume from where
the doer left off — but the doer's container is gone, so any partial doer state is
already lost.

**Fix.** Recover semantics for subtask rows: any subtask in IN_PROGRESS at recovery
time gets reset to PENDING (or whatever the canonical "ready to attempt" state is for
subtasks). Already partially handled at store_tasks.py:157 — confirm.

## Tests

- Crash-restart with one task in DOING_SUBTASK, attempt 5. After recovery:
  - task state = PENDING
  - subtask row reset to PENDING
  - worktree+branch intact (or reconstructed if missing)
  - resume marker present, skip planning
- Crash-restart with worktree manually deleted between crash and restart → assert
  worker reconstructs from remote branch.
- Crash-restart where `origin/main` advanced past stored planner output → assert
  re-plan triggered when stale-ratio threshold exceeded.

## Risk

Hardening orphan recovery is high leverage (every daemon restart goes through it) but
also touchy — wrong logic here corrupts state for every in-flight task at once. Land
with thorough test coverage; consider a pre-merge "shadow-run" mode that reports what
recovery would do without applying.
