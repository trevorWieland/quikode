# Overnight run status — May 6 morning

Run started at 2026-05-05 23:08. Snapshot below at ~07:25 next morning (≈8h elapsed).

## DAG progress

3/233 merged (the 3 seeded). Zero new merges yet — every task is grinding through
subtasks + fixup cycles. First PR open expected within the next hour from R-0006,
R-0021, or R-0019.

## In-flight (7/7 slots active)

| Task | Subtasks | Current | Notes |
|---|---|---|---|
| R-0002 | 5/9 | S-06 (r:2) | normal |
| R-0004 | 9/15 | F-1-2 (r:0) | passed audit cycle 1, 7 fixups planned, on #2 |
| R-0006 | 7/8 | S-08 (r:1) | last spec subtask, close to fast-forward |
| R-0008 | 0/n | provisioning | just picked up, replaced R-0005 slot |
| R-0019 | 11/22 | F-1-2 (r:1) | passed audit cycle 1, 12 fixups planned, on #2 |
| R-0020 | 6/9 | S-07 (r:9) | BDD subtask grinding |
| R-0021 | 7/8 | S-08 (r:5) | BDD subtask grinding, on attempt 6 |

## Blocked

**R-0005** — subtask `S-07-web-surface` flatlined 3× at attempts 9/12/15. Root cause
from triage: `cargo run -q -p tanren-cli -- migrate up` panics during Playwright
global setup. The doer can't fix the panic because tanren-cli is outside the
subtask's `files_to_touch`. This is a **planning-scope issue**, not a poisoned-doer
issue.

Options:
1. `qk retry R-0005` — fresh planning. May or may not include a CLI prerequisite
   subtask.
2. Manually fix `tanren-cli migrate` in main, then `qk resume R-0005`. The
   migration panic is real upstream code, worth investigating regardless.
3. Skip R-0005 for now — children depend on it, but stacking can wait.

I left it blocked for your inspection. Slot was freed and R-0008 picked up.

## Quikode bugs found and fixed during the run

Two cascading FSM bugs hit R-0004 around 05:14 and 05:27. Both fixed in the source,
reinstalled, daemon restarted (twice). Tests added.

**Fix 1** — `workers/pre_pr.py:_commit_push`. With v3 per-subtask commits, the
last subtask leaves the task in PUSHING; the existing code unconditionally fired
`SUBTASK_PASSED` from PUSHING → KeyError → FAILED just before the PR. Added
`_fast_forward_to_local_ci_if_subtasks_done` helper that walks the legal event
chain to LOCAL_CI_CHECKING, covering PUSHING (normal end), PLANNING (full
resume), and DOING_SUBTASK (partial resume) entry states.

**Fix 2** — `fsm_runtime.enter_local_ci_checking` was non-idempotent (unlike
sibling helpers `enter_pre_pr_auditing` etc.). After fix 1, the worker hit
`_run_pre_pr_pipeline` cycle 1 which re-calls `enter_local_ci_checking` — invalid
from the LOCAL_CI_CHECKING state we'd just entered. Added the same
already-in-state guard the other helpers use.

Both fixes are tested (`tests/test_worker_helpers.py` +3 tests) and shipped.

## Cost (~8h)

- codex: 6h05m / 7.4M tokens
- opencode: 43h13m / 40.9M tokens (across 7 parallel slots)

## Plans written tonight

`/plans/00-INDEX.md` indexes 11 plans, ordered by leverage. Highlights:

- 02 net-retry-backoff
- 04 supervision-stall-event
- 05 poisoned-worktree-wipe
- 06 progress-check-signal-quality (annotated with R-0002 false-positive evidence)
- 11 stream-agent-output-to-log

The R-0004 crash is itself a reason to add **plan 12: FSM-runtime helpers should
all share an idempotency policy.** Half the helpers are idempotent (don't re-fire
when already in target state), half aren't. Inconsistency caused fix 2.

## Resource state

- 78GB RAM, 20GB used, 58GB available — fine.
- 232GB disk used / 1TB. sccache 5.5GB, worktrees 5.2GB, logs 28MB.
- max container RSS hit 7.5GB (under 8GB cap). No OOM events.

## Daemon

Running. Heartbeat fresh. Monitor armed (task `bl7pf3gtv`) on
ERROR/InvalidTransition/state-transitions-of-interest patterns.
