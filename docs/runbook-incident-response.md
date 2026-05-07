# Incident Response

Start with:

```bash
quikode daemon status
quikode briefing
quikode show <task-id>
quikode subtasks <task-id>
quikode tail <task-id>
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
