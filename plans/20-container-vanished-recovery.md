# Plan 20 — container-vanished retry cascade + orchestrator-race fix

## Why this plan exists

On 2026-05-07 at ~00:58–01:02, dev containers across the quikode workspace
died simultaneously and the orchestrator's response stalled the entire run.
**12 tasks ended BLOCKED** at the per-subtask 50-attempt hard ceiling and
**4 tasks ended FAILED** with `InvalidTransition` errors. Only 3 of 16
worker slots were active. Sixteen parallel investigation subagents converged
on a single dominant root cause plus one compounding race condition; this
plan ships the four narrow patches that close both, adds a `qk reset-retries`
CLI for surgical recovery, and walks the affected tasks back to PENDING
without losing committed work.

## What went wrong

**Bug A — container-vanished retry cascade.** When a dev container died, the
orchestrator detected the failure but kept re-issuing `docker exec` against
the same dead container instead of recreating it. Each retry took ~1 s,
returned rc=1 with stderr `Error response from daemon: No such container`,
and **`workers/subtask_execution.py:_run_subtask_check_command` did not
classify that stderr as transient** — even though the doer/triage path in
`agents/base.py:_exec` already does (via `_TRANSIENT_STDERR_MARKERS`). So
objective-check failures against a corpse container charged the per-subtask
retry counter, burning the 50-attempt hard ceiling in 60–90 s. Subagent
forensics on every blocked task showed the same fingerprint:
`container_vanished=30..44` in `qk show <task>`'s retry-cause histogram.

**Bug B — orchestrator race.** A second `qk run` (or daemon restart)
called `cli_core._prepare_run_workspace`, which runs `cleanup_all_quikode`
(kills containers) and `recover_orphan_tasks` (flips active rows to
PENDING). The prior daemon's worker threads were still alive and still
firing FSM events against those rows: `enter_doing_subtask` from
`provisioning`, `doer_done` from `provisioning`, `subtask_failed` from
`pending`. Each raised `InvalidTransition`. The exception handler then
called `crash_current`, which fired CRASH from the row's now-FAILED state —
also invalid — cascading a second `InvalidTransition` and masking the
original error.

## What changes

Four narrow patches (one commit), one new CLI, and a per-task recovery
checklist for the immediate operator follow-up.

### 1A. Objective-check stderr classified against transient markers

`quikode/workers/subtask_execution.py:_run_subtask_check_command` now calls
`agents.base._is_transient_container_failure(rc, stderr)` on the gate's
exit. When True, returns `_CheckerOutcome(transient=True, ...)`; the
existing `_record_transient_subtask_failure` path (`workers/subtasks.py:317`)
decrements the attempt counter. Net effect: a vanished-container gate
failure is a free retry, not an attempt-counter increment.

### 1B. `ensure_dev_container_running` helper, called per attempt

`quikode/docker_env.py` gains `is_dev_container_running(handle)` (one
`docker inspect`, ~50 ms) and `ensure_dev_container_running(handle, cfg,
worktree_path, label=None)` — idempotent: when the container is alive,
short-circuits with no recreation; when dead/missing, tears down stale
state and recreates the postgres + dev container + waits for ready.

`quikode/workers/subtasks.py:_attempt_subtask_until_settled` calls the
helper at the top of each attempt loop iteration, before
`enter_doing_subtask`. A consecutive-recreation cap of 3 prevents a
permanently-broken provisioning path from pinning the worker.

This is option (b) from the design exploration. We rejected option (a)
(adding `needs_reprovision` to `AgentResult`) because `TaskContainer`
intentionally lacks `cfg/wt_path/uid/gid`, so `_exec` cannot recreate
containers itself — recreation belongs at the worker layer where that
state is already accessible.

### 1C. `crash_current` guarded against terminal states

`quikode/workers/task_worker.py` gains `_safe_crash_current(err)`, which
reads `current_state` and skips the `crash_current` call if the row is
already in `fsm.TERMINAL_STATES`. Both crash sites (line 147–149 main
exception handler and line 228 multi-parent merge-base failure) now
route through it. The helper additionally swallows any inner exception
from `crash_current` itself — the caller is already in an exception
handler and can't usefully propagate further.

### 1D. Singleton orchestrator lock

`quikode/cli_core.py` acquires an exclusive non-blocking `fcntl.flock` on
`<state_dir>/orchestrator.lock` BEFORE `cleanup_all_quikode`. On
`BlockingIOError`, prints a friendly message naming the lock file and
exits with code 2. The handle is held in module state for the daemon's
lifetime; released by an extension to the existing `cleanup_pid` atexit
hook. flock is advisory and kernel-released on FD close (incl. SIGKILL),
so a hard-killed daemon never leaks the lock.

### 2. `qk reset-retries <task_id> [<subtask_id>]`

New CLI command in `quikode/cli_lifecycle.py`. Refuses (exit 2) on tasks
not in BLOCKED or FAILED. Without `subtask_id`, targets every subtask in
state `blocked`; with `subtask_id`, targets exactly that subtask. Per
target, zeroes `retries`, `transient_retries`, `flatline_count`, clears
`last_error`, and (if previously `blocked`) flips state back to `pending`.
Does NOT fire FSM events on the task row — `qk resume <task_id>` is the
follow-up that drives the row back to PENDING.

## What this plan intentionally does NOT do

- **Reset retries on every BLOCKED row at orphan-recovery time.** That
  would auto-clear retries on legitimately-stuck tasks too, inviting a
  cascade in the opposite direction (real failures retried 50× after
  every restart).
- **Tighten the consecutive-transient cap below 5.** That cap is the
  existing fail-stop; lowering it would catch real transient noise. The
  fix is the per-attempt re-provisioning, which converts noise into
  free retries with a clean container.
- **Add a `qk force-state` admin command.** State surgery via raw SQL or
  bypass commands is the wrong tool — `qk reset-retries` + `qk resume`
  is the principled path.

## Verification

- `uv run ruff check quikode tests` — clean.
- `uv run ruff format --check quikode tests` — clean.
- `uv run ty check quikode tests` — clean.
- `uv run pytest tests/ -q` — clean. Five new test files cover the four
  patches plus the new CLI:
  - `tests/test_subtask_execution_transient.py`
  - `tests/test_ensure_dev_container.py`
  - `tests/test_terminal_crash_guard.py`
  - `tests/test_orchestrator_lock.py`
  - `tests/test_reset_retries_cli.py`
- Functional verification on the live workspace: after reinstall + walk of
  `docs/incident-2026-05-07-recovery.md`, `qk briefing` shows zero
  BLOCKED, zero FAILED tasks; previously-stuck tasks resume from their
  last real attempt with a clean container.

## Status

**Shipped** in this commit on `optimizations`.
