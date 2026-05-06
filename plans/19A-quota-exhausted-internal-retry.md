# Plan 19A — quota-exhausted internal retry inside `_exec`

## Why this plan exists

The full plan-19 design (queued in `19-quota-cascade-prevention.md`) lays out
the long-term goal: real-signal monitoring and graceful pause without
approximating subscription buckets from local accounting. This plan ships the
**minimum** subset that closes the bleed today: when any agent CLI returns a
quota-exhausted signal, sleep with backoff inside the agent layer and retry
the same call instead of surfacing a failure.

Live-fire context: with 16 parallel doers all routed to opencode/zai-coding-plan,
the operator was projected to exhaust GLM's 5-hour bucket in ~1.5 h of a 5 h
window. Without this fix, exhaustion would manifest as a doer 429, which the
FSM unconditionally follows up with a checker call (on an empty diff,
guaranteed FAIL) and a triage call ("explain" the empty doer), each of which
might quota its own CLI. The 2–3× extra agent calls per quota-failed attempt
multiply across 16 slots, shred the remaining buckets across all three CLIs,
and produce no forward progress.

## What the fix does

Inside `quikode/agents/base.py:_exec` — the single chokepoint every per-CLI
wrapper goes through — wrap the existing exec/transient/result flow with a
retry loop that:

1. Detects quota exhaustion via a permissive pattern set covering
   - Claude Code's `You've hit your session/weekly/Opus limit` stderr
   - Codex's `rate_limit_exceeded` (stderr or JSONL `turn.failed`/`error` body)
   - Generic 429 / "rate-limit / quota / usage-limit … exceeded/reached/
     exhausted/hit" / "too many requests" / "insufficient quota"
   Only fires when `rc != 0` so a successful agent call that mentions "429"
   in its output (e.g. discussing rate-limit handling code) is not
   misclassified.

2. Sleeps with exponential backoff (5 min initial → doubles → 30 min cap).
   Tunable via env vars `QUIKODE_QUOTA_BACKOFF_INITIAL_S` and
   `QUIKODE_QUOTA_BACKOFF_MAX_S` for testing.

3. Logs the wait to both the daemon log (`quikode.agents` logger) and the
   per-task agent log file. Operator can grep for `quota exhausted` to find
   any task currently waiting.

4. Caps cumulative wait at 8 h (`QUIKODE_QUOTA_MAX_TOTAL_WAIT_S`). Past
   that — almost certainly a misconfigured auth token rather than a real
   bucket reset — the call surfaces the failure to release the worker
   slot, so a single broken role can't pin a slot indefinitely.

5. On retry success, the call returns a normal `AgentResult` with
   `duration_s` reflecting the total wall-clock (including waits). The
   FSM never sees a quota-exhausted state.

## Why this is the right minimum

Wrapping `_exec` is the smallest possible insertion point. It:

- Touches **one function** in the codebase (`_exec`).
- Requires **zero schema changes** (no new field on `AgentResult`, no new
  table column, no FSM state).
- Requires **zero caller changes** — every per-CLI wrapper already calls
  `_exec`; every role (planner/doer/checker/triage/scope/progress) already
  flows through it.
- Protects every role, not just the doer. A quota'd checker now also
  internally retries instead of returning FAIL, so the cascade can't start
  from any direction.
- Doesn't approximate subscription buckets — strictly reactive. It detects
  what the CLI actually told us (per the project's recorded
  no-approximation rule, memory: project_quota_measurement_constraint).

Things this plan deliberately does NOT do, deferred to plan 19B/19C:

- No `qk usage` command or briefing line for "currently waiting on quota."
  The log is the source of truth for this round; UI surfacing can come later.
- No model rotation / fallback chain. A waiting role just waits — it does
  not switch to a different CLI. Rotation is plan 19C.
- No FSM-level "waiting" state. A quota'd worker holds inside `_exec`
  rather than transitioning to a new state. Existing orphan recovery on
  daemon restart already cleanly resets the task to PENDING if needed.
- No update to `retry_classify.py`'s post-facto `agent_cli_rate_limit`
  category. That continues to fire for failures that escape the wait
  budget (8 h cap), which is correct: at that point the failure DID need to
  be classified for the audit trail.

## Watchdogs stay orthogonal — quota wait does NOT erode hung-agent kill

Two failure modes that look superficially similar but must be handled
separately, and are:

| Failure mode | Detection | Handling |
|---|---|---|
| Agent CLI hangs (no response within per-call `timeout`, e.g. 1800s for the doer) | `subprocess.TimeoutExpired` raised inside `exec_in()` | Existing path — return `rc=124, transient=True`. The FSM treats it as a transient retry. UNCHANGED. |
| Agent CLI returns quickly with `rc != 0` and a quota-pattern message | New `_is_quota_exhausted` check after `exec_in()` returns | Sleep, retry inside `_exec`. The FSM never sees a quota-exhausted result. NEW. |

The two paths never overlap: quota detection only fires on a *clean, fast*
`exec_in` return — i.e., the agent CLI itself surfaced a quota error
before its own timeout. A genuinely-hung agent doesn't take this path; it
takes the existing `TimeoutExpired` path.

The `time.sleep` for the quota wait is **outside** the per-call timeout
budget. Each retry iteration calls `exec_in(..., timeout=timeout)` fresh,
so a hung agent on the very next retry attempt is still killed correctly.
Confirmed there is no other wall-clock watchdog that could kill a paused
worker:

- `subtask_hard_max_attempts` is an attempt *count*, not a time budget;
  quota retries don't increment it.
- `recover_orphan_tasks` runs only at daemon startup, not during normal
  operation.
- The "in-state >30 min" yellow/red coloring in the briefing / TUI is
  display-only — no automatic action.
- No `ThreadPoolExecutor.wait_for`, `signal.alarm`, or `threading.Timer`
  wraps `agent.run()` calls anywhere in the worker / orchestrator code.

## Operational characteristics

- 16 parallel doers all hit GLM 429 → all 16 workers sleep inside `_exec`.
  No checker calls fire. No triage calls fire. The slots are held but
  cost nothing in tokens.
- 5h GLM bucket resets → next retry succeeds → all 16 workers naturally
  resume where they left off. Wall-clock of affected attempts grows by
  the wait time; no work is lost.
- If the operator stops the daemon while a worker is waiting, SIGTERM
  interrupts `time.sleep`, the agent call returns prematurely, the worker
  exits, orphan recovery resets the task on restart.

## Validation

- `uv run ruff check quikode tests` — clean.
- `uv run ruff format --check quikode tests` — clean.
- `uv run ty check quikode tests` — clean.
- `uv run pytest tests/test_agents.py -q` — clean (new test cases cover
  Claude session/weekly/Opus, Codex rate_limit_exceeded, generic 429,
  quota-exceeded phrasings, plus negative cases for rc=0 and
  unrelated failures).
- Functional verification on the live overnight run: when GLM exhausts in
  ~1.5 h, doer agent logs show `quota exhausted (rc=N); retry 1, sleeping
  300s ...` instead of triggering checker. Tasks remain in
  `doing_subtask` rather than oscillating through triage.

## Status

**Shipped** in this commit on `optimizations`.
