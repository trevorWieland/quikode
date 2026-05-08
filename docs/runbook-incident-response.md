# Incident Response

Start with:

```bash
quikode daemon status
quikode briefing
quikode show <task-id>
quikode subtasks <task-id>
quikode tail <task-id>
quikode monitor --since 1h
```

## Invalid Transition

First look: current task state, attempted event, and recent `state_log`.

Recovery: fix caller code to emit the correct event or add a deliberate FSM transition with a test. Do not direct-write around the FSM in runtime code.

## Blocked Task

First look:

```bash
quikode unblock <task-id>
```

Recovery: inspect block forensics, latest checker output, retry categories, progress checks, and worktree state. Fix locally and `resume`, or intentionally `retry`.

## Container-Vanished Retry Cascade

**Detection signature.** Multiple tasks blocking near-simultaneously with
all of the following:

- Identical retry-cause histogram fingerprint in `qk show <task>`:
  `container_vanished=N` (typically 30–44) for the blocking subtask.
- Per-attempt durations of <2 seconds across the last 30+ attempts.
- Daemon log shows repeated `objective check FAILED (rc=1, 119 bytes of output)`
  or `(rc=1, 69 bytes of output)`, where the body is `Error response from
  daemon: No such container: ...` or `... is not running`.
- Hitting the 50-attempt hard ceiling in 60–90 seconds of wall-clock.

**Root cause** is documented in plan 20: `agents/base.py:_TRANSIENT_STDERR_MARKERS`
already classifies these patterns as transient on the doer/triage path,
but the objective gate runner (`workers/subtask_execution.py:_run_subtask_check_command`)
historically did not — so vanished-container gate failures were charged
against the per-subtask attempt counter. Plan 20 ships the fix:
container recreation per attempt (`docker_env.ensure_dev_container_running`)
and stderr-marker classification on the gate path.

**Recovery template** (post-fix-deploy):

1. Confirm the four plan-20 patches are deployed: `bash scripts/reinstall.sh --skip-tests`.
2. Stop the daemon: `qk daemon stop`.
3. For each affected task:
   ```bash
   qk unblock <task-id>          # forensics: confirm container_vanished histogram
   qk reset-retries <task-id>    # zero retries on every blocked subtask
   qk resume <task-id>           # → PENDING with resume marker
   ```
4. Restart daemon: `qk daemon start --detach --max-parallel <N> --retry-failed`.
5. Soak ≥30 minutes; watch `qk briefing` for new `container_vanished` patterns.

For batch incidents where a single root-cause docker event takes out many
tasks at once, write a per-incident recovery doc under `docs/incident-<date>-*.md`
listing each task + any task-specific worktree intervention needed before
the resume (per plan 20's checklist for the 2026-05-07 incident).

## Failed Invariant

First look: task log, artifact stream, and the state transition immediately before failure.

Recovery: preserve artifacts, add a direct regression test, and fix the invariant at the service boundary.

## Stale Heartbeat

First look:

```bash
quikode daemon status
```

Recovery: stop the daemon. The next start runs crash recovery through the FSM. If this repeats, inspect the active task log and subprocess waits.

## PR Closed

First look: `quikode show <id>`, GitHub PR state, and parent branch metadata.

Recovery: if parent-base deletion caused the close, use the rebase path. If a human closed the PR intentionally, decide between `abort`, `retry`, or local branch inspection.

## Rebase Conflict Unresolved

First look: task worktree, conflict markers, and conflict resolver artifact.

Recovery: resolve locally, commit as needed, then `resume`. If the branch is not usable, `retry`.

## Feedback Cap Hit

First look: review rounds, unresolved thread list, CI failures, and latest fixup artifacts.

Recovery: operator chooses one of: merge externally, post guidance, `resume`, or `retry`.

## Seed Evidence Mismatch

First look: DAG metadata, configured base-branch commit subjects, and any explicit evidence file used with `seed-from-base`.

Recovery: correct the evidence source and rerun seeding in a fresh workspace.

## Fruit-of-rotten-tree wipe

Triggered after **any change that tightens the stacking gate** — flipping `cfg.stacking_readiness` from `"speculative"` to `"settled"`, raising `cfg.review_ready_settle_s`, or shipping a plan that adds a new readiness predicate.

The new gate only governs FUTURE picks. Tasks already on disk with worktrees + branches were forked under the OLD looser gate, often off parents that were:

- Still in PROVISIONING / PLANNING / DOING_SUBTASK (no real branch yet)
- In PENDING_CI but with CI not yet green
- In ADDRESSING_FEEDBACK with the parent's behavior actively churning

Children that built atop those foundations were building on broken, half-formed, or actively-shifting bases. Every behavior they wired up against the parent's incomplete contract may need to be redone once the parent settles. Symptoms include doer attempts that struggle with "the type/method I need doesn't exist" or "this trait shape contradicts what I'm assembling" — patterns that no amount of doer-prompt strengthening can fix, because the foundation is wrong.

**The canonical follow-up to a stacking-gate tightening: identify and wipe every PENDING task that has a worktree but whose parent isn't merged.**

### Identifying the wipe set

```python
import json, sqlite3
dag = json.load(open('<repo>/docs/roadmap/dag.json'))
nodes = {n['id']: n for n in dag['nodes']}
merged = {<the merged-set, from `qk status` or store.in_state(MERGED)>}
con = sqlite3.connect('file:<state_dir>/quikode.db?mode=ro', uri=True)
con.row_factory = sqlite3.Row
running_states = {
    'doing_subtask', 'checking_subtask', 'triaging_subtask', 'committing',
    'pushing', 'pr_opening', 'planning', 'provisioning', 'fixup_planning',
    'addressing_feedback', 'pre_pr_auditing', 'local_ci_checking',
    'rebasing_to_main', 'conflict_resolving', 'pending_ci', 'awaiting_review',
}
for r in con.execute("SELECT id, state, branch FROM tasks").fetchall():
    if r['state'] in running_states or r['state'] != 'pending' or not r['branch']:
        continue
    deps = nodes.get(r['id'], {}).get('depends_on') or []
    unmet = [d for d in deps if d in nodes and d not in merged]
    if unmet:
        print(r['id'], 'forked on un-merged:', unmet)
```

### Wipe sequence

`qk retry` requires BLOCKED/FAILED/ABORTED. PENDING tasks need `qk abort && qk retry` to apply the worktree + branch + subtask cleanup:

```bash
for t in R-0004 R-0005 R-0006 ...; do
  qk abort "$t"
  qk retry "$t" --reason "fruit-of-rotten-tree wipe: forked off non-merged parent under prior gate"
done
```

`qk briefing` should show the in-flight count unchanged (active tasks aren't touched) and the pending-with-branch count drop to zero. The wiped tasks now have no branch / worktree / subtask rows; they will be re-planned from scratch the next time the scheduler picks them — which under the tightened gate will happen only after their parent reaches the new readiness signal.

### Don't wipe

- Tasks currently in flight (interrupts active work).
- Tasks whose parent is now merged (their foundation is sound; let them keep their progress).

### Why doing the wipe matters

Without it, the daemon keeps re-running stacked tasks with `qk resume` semantics — the doer's prior-output carry-forward (plan 22) and the worktree state both reference the rotten foundation. Doer attempts will keep producing the same shape of failure no matter how strong the prompts are. Only `qk retry` clears the carry-forward + worktree state cleanly.
