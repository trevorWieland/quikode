# Stacked diffs — comprehensive fix design

> **Status: IMPLEMENTED.** This document is kept as architectural reference for the design decisions.
> Current architecture lives in [`architecture.md`](architecture.md). Operational guidance is in
> [`runbook-operations.md`](runbook-operations.md). The v3 lessons section in
> [`lessons-learned.md`](lessons-learned.md) summarizes what each fix unlocked.

## What broke in the v3 E2E

Run 1 of the v3 fixture validation surfaced several real bugs in the stacked-diff path. T-003 (depends_on=[T-001]) was scheduled while T-001 was AWAITING_MERGE; when T-001 merged mid-flight, a cascade of failures hit:

1. **`git rebase --continue` failed: "Terminal is dumb, but EDITOR unset"** — git wants an editor for the resolved commit's message; containers have no TTY/EDITOR. Patched with `git -c core.editor=true rebase --continue`.

2. **Multi-conflict rebases needed an iteration loop** — a 4-commit rebase can hit conflict on commit 1, then ANOTHER on commit 2 after `--continue`. Original code returned after the first `--continue`. Patched in `_spawn_conflict_resolver` to loop on `_rebase_in_progress()` until rebase completes or iteration cap hit.

3. **Parent merges mid-flight on a stacked child**: T-003 was in DOING/CHECKING/AWAITING_MERGE when T-001 merged. The orchestrator's `_schedule_rebases_for_merged_parent` skipped T-003 because it was in the active futures set. T-003 finished its work, tried to `gh pr create --base quikode/t-001-XXX` — that branch was deleted by GitHub on T-001's `--delete-branch` merge. PR create failed.

4. **Plain `git rebase origin/main` on a stacked child re-applies parent's commits** — they're already in main as a squash. Causes duplicate-commit conflicts on every line the parent and child both touched.

5. **Detached HEAD post-rebase** — after some failure paths, the worktree was left in a state where `gh pr create` reported "could not determine current branch."

6. **Orphan tasks on daemon SIGTERM** — child orchestrator SIGTERM stops the worker mid-step. Task remains in DOING_SUBTASK / CHECKING / etc. On daemon restart, `_pick_next` only picks PENDING; the orphan sits forever.

7. **No-conflict false-positive in resolver loop** — `git diff --name-only --diff-filter=U` returned empty mid-rebase, the new iteration code aborted with "no conflicted files surfaced."

The user's diagnosis: "Anything that you had to manually intervene with, you should now architect a long term automatic solution for."

## Proposed architecture

### 1. Use `git rebase --onto` for child-on-merged-parent rebases

When parent's branch is squash-merged to main, the parent's individual commits are folded into one squash on main. A child stacked off the parent's branch has those individual commits in its history. Plain `git rebase origin/main` replays them onto main → conflicts with the squash.

**Fix**: rebase with `git rebase --onto origin/main <parent_branch_local_ref>`. This drops commits between `parent_branch` and HEAD (the parent's contribution) and replays only what's exclusive to the child. The local `parent_branch` ref persists in the repo even after the remote branch is deleted by `--delete-branch`.

**Where**: `worker.py:run_rebase_to_main` and `worker.py:_rebase_to_base_branch` (the helper my E2E patch added in `_open_pr`).

**Algorithm**:
```bash
# Capture parent ref while local copy still exists (it persists post-deletion)
PARENT_SHA=$(git rev-parse --verify <parent_branch> 2>/dev/null || echo)
git fetch origin main
if [ -n "$PARENT_SHA" ]; then
    git rebase --onto origin/main "$PARENT_SHA"
else
    # Fallback: parent ref gone (shouldn't happen but be safe)
    git rebase origin/main
fi
git push --force-with-lease origin <branch>
gh pr edit <pr_number> --base main
```

### 2. Mid-flight parent-merge detection via flag + worker checkpoints

The current `_schedule_rebases_for_merged_parent` skips children already in the futures dict. That's wrong — those children NEED rebasing too, just not via a separate worker future.

**Fix**: separate the two concerns:
- For non-active children: schedule a `_run_rebase_to_main_one` future as today.
- For active children: set a flag `tasks.needs_parent_rebase=1`. The worker checks this flag at safe checkpoints and runs the rebase inline before continuing.

**New schema**: `ALTER TABLE tasks ADD COLUMN needs_parent_rebase INTEGER DEFAULT 0;`

**Worker checkpoints** that read + handle the flag:
- After each subtask in `_subtask_loop` (before moving to next subtask)
- Entry to `_final_check_loop`
- Entry to `_commit_push`
- Entry to `_open_pr`
- Each iteration of `_poll_pr_loop` (post-AWAITING-MERGE for legacy paths)

When flag set, the worker:
1. Saves any uncommitted state (`git add -A && git stash` if anything dirty).
2. Captures `parent_sha = git rev-parse <parent_branch>` (local ref).
3. `git fetch origin main`
4. `git rebase --onto origin/main <parent_sha>` (with `core.editor=true`)
5. On conflict: invoke `_spawn_conflict_resolver` (which already loops).
6. `git push --force-with-lease`
7. If `pr_number` exists: `gh pr edit <pr> --base main`
8. Set `parent_branch=NULL`, `parent_pr_branch=NULL`, `needs_parent_rebase=0`.
9. `git stash pop` if anything was stashed.
10. Continue from where the worker was.

This handles cases 1-3 in one mechanism. The flag pattern mirrors the existing `needs_intent_review` plumbing.

### 3. Startup orphan-recovery in `quikode run`

On every `quikode run` startup (before the orchestrator's main loop begins), scan all tasks. Any task in an active state has nothing actively working on it (we just started). Reset state per a state-specific recovery table:

| Current state | Recovery action |
|---|---|
| `pending` | leave alone |
| `provisioning` | clear branch/worktree_path/container_id, reset to `pending` |
| `planning` | if plan_text exists → `pending` + `resume_from_existing_subtasks=1`; else clear plan fields, reset to `pending` |
| `doing_subtask` / `checking_subtask` / `triaging_subtask` | `pending` + `resume_from_existing_subtasks=1` |
| `doing` / `checking` / `triaging` | `pending` + `resume_from_existing_subtasks=1` |
| `final_checking` | `pending` + `resume_from_existing_subtasks=1` |
| `committing` / `pushing` | `pending` + `resume_from_existing_subtasks=1` (worker will re-do commit/push, idempotent) |
| `pr_opening` | if `pr_number` set → `awaiting_merge`; else `pending` + resume marker (worker will re-attempt PR open) |
| `polling_ci` | `awaiting_merge` if `pr_number` set, else `pending` + resume |
| `responding_to_review` | `awaiting_merge` (let watcher re-detect open threads on next tick) |
| `rebasing_to_main` / `conflict_resolving` | abort any pending rebase in worktree, then `awaiting_merge` if `pr_number` else `pending` + resume |
| `intent_reviewing` | `awaiting_merge` if `pr_number` else `pending` + resume |
| `awaiting_merge` / `merged` / `blocked` / `failed` / `aborted` | leave alone (terminal-ish) |

Reset retry counters in all recovery transitions.

**Where**: new helper `quikode/state.py:Store.recover_orphan_tasks()` + invocation from `quikode/cli.py:run` after the workspace-container cleanup, before orchestrator construction.

**Safety**: this is invoked at orchestrator startup. If a daemon is running with an active orchestrator child, the new `quikode run` would be a second instance — we'd want to detect that and abort. Already handled by daemon's pid-file check. But for direct `quikode run`, add a similar check (refuse to start if `orchestrator.pid` is fresh).

### 4. Worktree state hygiene

After any failure path that involves rebase, ensure the worktree is left on a named branch (not detached). The `git rebase --abort` should restore HEAD; verify after.

Add `_ensure_on_branch()` helper that runs `git symbolic-ref HEAD refs/heads/<branch>` if HEAD is detached. Call from orphan-recovery and after `_rebase_in_progress() == True` aborts.

### 5. Improve `_rebase_in_progress()` accuracy

Use `git rev-parse --git-path rebase-merge` + check directory existence. More reliable than REBASE_HEAD which only exists for some rebase types.

```python
def _rebase_in_progress(self) -> bool:
    rc, out = self._git_in_workspace(["rev-parse", "--git-path", "rebase-merge"])
    if rc == 0 and out.strip():
        # Check if the dir actually exists
        path = out.strip().splitlines()[-1]  # last line = path
        rc2, _ = exec_in(self._h, ["bash", "-lc", f"test -d {path}"], log_path=self.log_path)
        if rc2 == 0:
            return True
    rc, out = self._git_in_workspace(["rev-parse", "--git-path", "rebase-apply"])
    if rc == 0 and out.strip():
        path = out.strip().splitlines()[-1]
        rc2, _ = exec_in(self._h, ["bash", "-lc", f"test -d {path}"], log_path=self.log_path)
        return rc2 == 0
    return False
```

### 6. Resolver iteration loop edge cases

The "no conflicted files surfaced" abort fires when `git diff --name-only --diff-filter=U` is empty mid-rebase. This can happen if the previous `--continue` succeeded for the current step but git is between commits. Fix: in the loop, if no conflicts AND `_rebase_in_progress()` still true, run another `--continue` directly (no agent). If that fails too, then abort.

```python
def _resolve_one_conflict_step(...):
    ...
    if not conflicted:
        # Mid-rebase but no conflicts? Try a simple --continue first.
        rc, out = self._git_in_workspace(["-c", "core.editor=true", "rebase", "--continue"])
        if rc == 0:
            return None  # caller's loop will check _rebase_in_progress
        # Still failing — abort
        ...
```

### 7. Defensive flag-clearing

The `parent_branch` / `parent_pr_branch` fields should be cleared not just on rebase success, but also when the orchestrator detects parent transitioned to a state where stacking no longer makes sense (MERGED, ABORTED, FAILED). The current code only clears on rebase completion. If the parent ABORTS (closed without merge), the child's `parent_pr_branch` still points to the closed branch.

Add to `_poll_review_threads` MERGED/CLOSED handlers: also clear children's stale `parent_pr_branch` references when parent closes.

## Tests to add

| Test | What it validates |
|---|---|
| `test_rebase_onto_drops_parent_commits` | `--onto` with a synthetic 3-commit-stacked branch produces a final state with only the child's 1 commit when rebased onto main. |
| `test_mid_flight_parent_merge_sets_flag` | Orchestrator sets `needs_parent_rebase=1` for in-flight children; doesn't submit a duplicate worker future. |
| `test_worker_checkpoint_handles_parent_merge` | Worker reads flag, runs rebase + retarget + clears flag, continues. |
| `test_orphan_recovery_doing_subtask` | Task in DOING_SUBTASK on startup → reset to PENDING with resume marker. |
| `test_orphan_recovery_pr_opening_with_pr` | Task in PR_OPENING with pr_number → AWAITING_MERGE. |
| `test_orphan_recovery_responding_to_review` | Task in RESPONDING_TO_REVIEW → AWAITING_MERGE (watcher re-detects). |
| `test_rebase_in_progress_accurate` | `_rebase_in_progress()` correctly detects rebase-merge and rebase-apply states. |
| `test_resolver_loop_no_conflicts_tries_continue` | When loop sees no UD files but rebase still in progress, runs `--continue` directly. |
| `test_parent_close_without_merge_clears_child_parent_branch` | Sibling task ABORT clears children's `parent_pr_branch`. |

## Schema migration

Single new column:
```sql
ALTER TABLE tasks ADD COLUMN needs_parent_rebase INTEGER DEFAULT 0;
```

Add to `Store._migrate()` extension dict.

## Rollout

1. Land the schema migration first (additive, idempotent).
2. Land `--onto` rebase + checkpoints + flag in one batch (worker + orchestrator changes are coupled).
3. Land startup orphan-recovery as a separate batch (it's standalone).
4. Re-run fixture E2E. Should require zero manual intervention.

## What this design does NOT solve

- **Repeated rebase loops if parent + child both fail tests after rebase**. The conflict_resolver might give up on a semantic conflict the substring-test fix doesn't help with. Out of scope; the existing `quikode unblock` flow handles it.
- **Stack depth >2**. T-001 ← T-003 is depth 1. T-001 ← T-003 ← T-005 would compound the issues. Existing `cfg.stack_max_depth=2` cap prevents this. Future work.
- **Force-push race between worker and orchestrator**. If both try to push the child branch concurrently, `--force-with-lease` should fail one. Acceptable.
