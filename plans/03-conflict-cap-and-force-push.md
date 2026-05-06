# Plan 03 — wire `conflict_max_resolve_attempts` to the loop, retry force-push

## Bug 1: hard-coded conflict cap ignores config

`workers/rebase_conflicts.py:73` literally says `max_iterations = 6`, ignoring
`cfg.conflict_max_resolve_attempts`. Tanren config sets it to 5, but the loop runs to 6.
Confusing for operators — the knob doesn't do what it says.

**Fix.** One line:

```python
max_iterations = self.cfg.conflict_max_resolve_attempts
```

Add a config validator: `conflict_max_resolve_attempts >= 1`. Update the comment in
the conflict-resolver agent prompt to reference the actual cap.

## Bug 2: force-push has no retry on transient

Three force-push call sites:
- `rebase_conflicts.py:118–128` — after auto-resolution
- `rebase_conflicts.py:55–65` — after no-conflict rebase
- `rebase_branch.py:179–190` — initial rebase push

Each is a single attempt. A transient SSH or 500 from GitHub → BLOCKED. Operator must
`qk retry` — which throws away the rebase work and starts the whole task over.

**Fix.** After plan 02 lands, replace the bare `git push --force-with-lease` calls with
`net_retry.run_with_backoff(...)`. Backoff schedule: 2s, 6s, 15s. Force-push is rare —
3 retries is plenty.

Also: when force-push fails *because* the remote moved (lease violation), don't retry —
that's a real conflict, not a transient. The classifier should treat
`stale info` / `failed to push some refs` (without 500/timeout) as hard.

## Bug 3: `git rebase --abort` followed by `block_current` loses the worktree state

When the conflict cap fires (`rebase_conflicts.py:81`), we abort the rebase and block.
Operator runs `qk unblock` — the worktree is back to pre-rebase state, so they can't
see *what* went wrong. The artifact `post_rebase_ci_log` survives, but the actual
mid-rebase conflict files are gone.

**Fix.** Before calling `git rebase --abort`, capture:
- `git status` output
- The first 200 lines of each conflicted file showing the markers
- Recent rebase-todo log if any

Persist as `conflict_state_snapshot` artifact. Operator gets enough forensics to decide
between manual fix vs. wipe-and-retry.

## Tests

- Stub conflict_resolver to return None for 5 iterations; verify cap respected
  (currently it would only fire at 6).
- Stub `git push` to return rc=1 once + rc=0 twice; verify retry succeeds (after plan
  02 + this).
- Stub `git push` returning "stale info" → no retry, BLOCKED immediately.

## Risk / scope

Tiny diff per fix. Plan 02 is a prerequisite for fix 2.

## Why this matters during the tanren run

Tanren has 70-wide layer 6. Once stacking reaches that layer, **rebase-on-parent-merge
will be a hot path** and force-push will fire often. Today, one transient → one BLOCKED
task. After this plan, transient → 1-3 retries.
