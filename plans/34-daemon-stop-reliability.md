# Plan 34 — daemon-stop reliability + orphan detection

## The bug, in production

Tonight (2026-05-08) `qk daemon stop` hit the same pattern as the prior
plan-26 supervisor incident, but worse:

- Supervisor pid=105364 received SIGTERM and exited.
- Child pid=105367 (`python3 -m quikode.cli run --max-parallel 12 --log-level INFO --retry-failed`)
  survived as an orphan, reparented to PID 1.
- The orphan kept ticking for **12 minutes**: spawning new docker
  exec subprocesses, recreating dev containers we'd just deleted,
  advancing task state in SQLite, and writing fresh heartbeats to
  `orchestrator.heartbeat`.
- `qk daemon status` reported "daemon not running" but the heartbeat
  age was 756s — confusing and ambiguous (a child still owned the
  workspace).
- `qk reset` saw 2 of 8 containers (orphan kept making more behind it)
  and only stopped what it could see; the user nearly went into a
  destructive operation against a half-cleaned workspace.
- The user had to `kill -9 105367` manually to break the cycle.

The pre-plan-34 stop logic SIGTERM'd only the supervisor pid, waited
30s, SIGKILL'd if still alive — and never inspected the child tree.
The supervisor's own SIGTERM handler is *supposed* to forward to the
child + a failsafe-kill timer fires SIGKILL (plan 26's regression
test), but in practice that path can be lost: if the supervisor itself
dies before its forwarding handler runs (or the failsafe timer thread
gets stuck), the child is orphaned and we never notice.

## Three fixes

### 1. `qk daemon stop` walks the full child tree

`stop_daemon(cfg, *, timeout_s, log_fn)` (now in
`quikode/daemon_shutdown.py`):

1. Read supervisor pid from `daemon.pid`.
2. Discover its full descendant tree via `/proc/*/stat` ppid scans
   (stdlib only — no `psutil` dep).
3. SIGTERM the supervisor AND each descendant explicitly. The
   supervisor's own SIGTERM handler also forwards, but we no longer
   trust that path alone.
4. Wait up to `timeout_s` (default 30s) for everyone to exit; emit a
   progress line every 5s with surviving pids + remaining budget.
5. SIGKILL anything still alive: supervisor first, then ordinary
   children, then docker-exec descendants last (gives `dockerd` the
   best shot at clean reaping).
6. Wait `SIGKILL_GRACE_S` (5s) for kernel reaping.
7. **Always** remove `daemon.pid` + `orchestrator.heartbeat` on exit,
   even if SIGKILL fired. Clean files == "no daemon" — the contract
   `qk daemon status` and `qk reset` depend on. We delete *before* the
   final SIGKILL so a late heartbeat write from the dying child can't
   resurrect a stale view, then delete again afterward as a backstop.
8. If anything is *still* alive at 35s total, log loud `ERROR` with
   each pid + cmdline and return False; `cli_daemon` surfaces this as
   exit nonzero so the operator can `kill -9` manually.

Per-pid log lines (visible to the operator running `qk daemon stop`):

- `daemon stop: SIGTERM supervisor pid=X`
- `daemon stop: SIGTERM child pid=Y cmdline='...'`
- `daemon stop: still waiting on N pid(s): [...] (Ms remaining)`
- `daemon stop: SIGKILL pid=Y (didn't exit in 30s)`
- `daemon stop: clean — pid+heartbeat files removed`

### 2. `qk daemon status` detects orphans

`detect_orphan_quikode_runs(cfg)` scans `/proc/*/cmdline` for processes
matching the inner-run pattern (`quikode.cli` + `run` + a run-mode flag
like `--max-parallel`). Filters out the supervisor itself and the
calling process.

`qk daemon status` adds a WARNING block when:

- supervisor pid file says dead (or missing) but live `quikode.cli run`
  children exist on the host;
- heartbeat is fresh but no supervisor in pid file (same disease,
  different symptom).

Exit code 2 in these cases so scripts can detect.

### 3. `qk reset` refuses with a live process

Entry-time guard: if `detect_orphan_quikode_runs` returns a live
supervisor or any orphan child, `qk reset` prints which pid + cmdline
and exits 2 without touching containers/worktrees/state. New `--force`
flag bypasses the check (last-resort recovery; "use only after manual
`kill -9 <pid>`").

## Implementation notes

- `/proc/<pid>/stat` parsing anchors on the final `)` of the comm
  field so processes with spaces in their command name don't fool us.
- `process_alive()` filters zombies via `/proc/<pid>/stat` field 3 —
  `kill(pid, 0)` returns success for zombies, which would have stalled
  the busy-wait loop indefinitely under tests where the test runner is
  the parent.
- Code split: the new shutdown logic lives in
  `quikode/daemon_shutdown.py` (stop / orphan detection) +
  `quikode/process_tree.py` (the `/proc` walker). `daemon.py` re-exports
  the public surface for backward compat, keeping the module under the
  600-line architecture budget.
- Linux-only: per the project's Linux-only deployment, no Windows/macOS
  abstraction layer; WSL2's `/proc` works without modification.

## Validation

- New `tests/test_daemon_lifecycle.py` (15 cases). Highlights:
  - `daemon stop` terminates a real supervisor + Popen-forked child
    pair; verifies pid+heartbeat removal.
  - `daemon stop` SIGKILLs a `signal.signal(SIGTERM, SIG_IGN)` helper
    within budget and still cleans lifecycle files.
  - `daemon stop` with a dead pid file just cleans files (no false
    "stopped" claim).
  - Synthetic argv-tail processes match the orphan-detection regex.
  - `qk reset` refuses with exit nonzero when a live supervisor exists;
    `--force` lets it through.
- Existing daemon-supervisor tests (27) continue to pass.
- Validation ladder green: `ruff check`, `ruff format --check`,
  `ty check`, `pytest tests/ -q` — 922 passed.

## Edge cases not exercised

- A multi-level tree (grandchildren of the supervisor) is parsed
  correctly by `discover_descendants` but the test suite only covers
  one fork generation directly. The BFS code is dead-simple and
  covered indirectly via `find_orphan_quikode_runs`.
- `qk daemon status --json` schema gains an `orphan_quikode_runs`
  array; no test exercises the JSON path.
- Real docker exec descendants (the kill-order rationale) are
  simulated via a monkeypatched `read_cmdline`; we don't spawn
  containers in tests.
