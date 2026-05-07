# Operations Runbook

## Start

```bash
quikode doctor
quikode seed-from-base
quikode daemon start --detach --max-parallel 3
quikode daemon status
```

Use `quikode run` for foreground debugging.

## Monitor

```bash
quikode status
quikode briefing
quikode show <task-id>
quikode subtasks <task-id>
quikode tail <task-id>
```

## State Meanings

`pending_ci`: PR is open; CI and review-thread state are being polled.

`awaiting_review`: CI is green and no actionable thread is currently known.

`merge_ready`: CI, review threads, and settle window are clean.

`triaging_feedback`: deterministic CI/thread classification is running.

`addressing_feedback`: fixup planning and subtask work are addressing actionable feedback.

## Interventions

`retry <id>` starts a task over.

`resume <id>` resumes current progress from stored subtasks.

`abort <id>` stops a task and marks it terminal.

`unblock <id>` prints forensics and local context for a blocked task.

`mark-merged <id>` marks already-landed upstream work as merged.

## Overnight Checklist

Run the validation ladder, initialize a fresh workspace, run `seed-from-base`, confirm already-landed nodes are `merged`, then start the daemon with the intended parallelism.
