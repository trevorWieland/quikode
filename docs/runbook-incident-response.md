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

First look: DAG metadata, `origin/main` subjects, and any explicit evidence file used with `seed-from-main`.

Recovery: correct the evidence source and rerun seeding in a fresh workspace.
