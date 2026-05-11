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

Recovery: if parent-base deletion caused the close, use the rebase path. If a human closed the PR intentionally, decide between `abort`, `replan-cycle`, `retry`, or local branch inspection. Prefer `replan-cycle` when the closure was triggered by issues localized to a non-initial planning cycle (post-PR replan / fixup) — earlier-cycle commits stay intact.

## Rebase Conflict Unresolved

First look: task worktree, conflict markers, and conflict resolver artifact.

Recovery: resolve locally, commit as needed, then `resume`. If the branch is not usable, escalate per orientation §3.4: `replan-cycle` when a non-initial cycle is the toxic one, `retry` only as a last resort when the foundation itself was wrong-shape.

## Feedback Cap Hit

First look: review rounds, unresolved thread list, CI failures, and latest fixup artifacts.

Recovery: operator chooses one of: merge externally, post guidance, `resume`, `replan-cycle` (re-decompose just the failing fixup / replan cycle without losing earlier-cycle commits — plan 52), or `retry` (last resort: discards all committed work; reserved for tasks where even the initial plan was wrong-shape).

## Seed Evidence Mismatch

First look: DAG metadata, configured base-branch commit subjects, and any explicit evidence file used with `seed-from-base`.

Recovery: correct the evidence source and rerun seeding in a fresh workspace.

## Quota cascade (plan 59 model)

Plan 19A's in-transport sleep-and-retry is RETIRED. With plan 59 the fallback chain is fast-fail at the transport layer and the worker layer owns re-attempt cadence.

**What the operator sees when all providers in a chain are exhausted:**

- `qk show <task-id>` shows recent transient outcomes with `category=quota_exhausted`.
- `qk briefing` shows no forward progress for any in-flight task across multiple poll ticks — the workers are sleeping at the worker layer, not stuck.
- The `agent_calls` table's most-recent rows have `status=running` but rc set + a `quota_exhausted` outcome; new agent_call rows aren't being created because workers are between attempts.
- Daemon log: `category=quota_exhausted` + the worker layer logging the configured sleep duration (`cfg.transient_retry_delays_s["quota_exhausted"]`, default 600s).

**Diagnose:**

1. `qk show <task-id>` for any in-flight task and confirm the most recent transient outcomes are `category=quota_exhausted` across multiple agent_calls.
2. Check the chain configuration: `model_registry.MODELS["GLM-5.1-zai"].quota_fallbacks` should list `["GLM-5.1-wafer", "gpt-5.3-codex"]` (or current chain). Confirm every link's `quota_max_total_wait_s=0` (plan 59 fix A) by inspecting `agent_registry._build_base_transport` call sites.
3. If a single 429 takes minutes instead of seconds to propagate through the chain, the in-transport sleep crept back in — re-check `agents/json_protocol.py:_run_with_retry` to confirm the quota retry loop is GONE (only container-vanished and auth-refresh loops remain).

**Intervene:**

- If subscriptions are genuinely exhausted across all providers, let the worker-layer cadence ride for one cycle (default 10 min). If the chain re-attempt then succeeds (e.g. a usage window reset), normal operation resumes.
- If wait is excessive and the operator has a backup provider, tune `cfg.transient_retry_delays_s["quota_exhausted"]` lower (e.g. 300) or swap the doer model temporarily to a non-quota-limited path (`subtask_doer_model = "gpt-5.3-codex"`), `qk daemon stop` + restart.
- If the cascade is masking a real bug (e.g. all transports falsely returning quota), pull the transient outcomes from `agent_calls` and inspect the underlying stderr/stdout.

## Audit gauntlet stuck on a cycle (plan 58 vocabulary)

Post-plan-58, "stuck on the gauntlet" means a phase is on `cycle_in_phase ≥ 5` with the release-valve criteria not being met. The release valve fires when local CI + behavior pass and only deferable quality stages remain failing — it does NOT fire when a non-deferable stage (local_ci / behavior / config / transport / parse / critical findings) is failing.

**Symptom:** `qk briefing` shows `PRE_PR_REVIEW cycle 5+` or `PR_REVIEW cycle 5+` for the same task across multiple poll ticks; the task is in `FIXUP_PLANNING` or `AUDIT_LOCAL_CI` and not advancing.

**Diagnose:**

1. `qk show <task-id>` to see the latest fixup-planner output + which audit stages are passing vs. failing.
2. Check the per-stage outcomes (`pre_pr_audit_summary` or the per-stage artifacts). Identify the lowest-numbered failing stage.
3. If the failing stage is `local_ci`, `behavior`, or a critical finding from `rubric`/`standards`/`architecture` — the valve is correctly refusing to fire. The task needs an actual fix.
4. If the failing stage is medium/low severity in a deferable category — the valve SHOULD have fired. Check `cfg.release_valve_after_cycles`, `cfg.release_valve_defer_stages`, `cfg.release_valve_max_critical_findings`. Confirm the post-plan-58 rename took effect (a stale `pre_pr_release_valve_*` key will be surfaced by plan 50's `_warn_orphan_overrides` audit at daemon start).

**Intervene:**

- **`qk replan-cycle <task>`** when the cycle's fixup-planner decomposition itself is wrong-shape (over-scoping the findings, asking the doer for too much per subtask). Earlier cycles' commits survive; the fixup planner re-fires for THIS cycle with the same audit findings as input, and the new emission may decompose more granularly. This is the default escalation for cycle-level non-convergence in a non-initial cycle.
- **`qk retry <task>`** only when the failing subtask is in cycle 1 (initial planner output) AND the diagnosis is that the very first plan was wrong-shape. For fixup cycles, prefer `replan-cycle` — `retry` would torch every passing earlier-cycle commit.
- **Plan-50 audit-warns first.** If you see WARN logs at daemon start about `release_valve_*` orphans, the rename didn't fully take effect and the valve config isn't actually being honored. Fix the workspace TOML and restart before intervening.

## Plan 58 cutover incident (rollback path)

The plan-58 migration ships hard cutover via `plans/58-migration.sql`:
1. Backs up the existing `tasks` table to `tasks_backup_plan58`.
2. Adds `phase`, `cycle_in_phase`, `pr_review_trigger` columns.
3. Derives phase from existing state.
4. Maps deprecated states `pre_pr_auditing` / `addressing_feedback` to `pending` with a resume marker.

**If anything looks wrong post-cutover** — phase derivation produces unexpected counts, a task's cycle_in_phase doesn't match its history, the daemon refuses to start due to schema inconsistency — the backup table is the safety net.

**Inspect first:**

```bash
sqlite3 .quikode/quikode.db
.headers on
SELECT id, state, phase, cycle_in_phase, pr_review_trigger FROM tasks WHERE id = 'R-NNNN';
SELECT id, state FROM tasks_backup_plan58 WHERE id = 'R-NNNN';
```

**Restore from backup** (destructive — only after the daemon is stopped):

```bash
qk daemon stop
sqlite3 .quikode/quikode.db <<'SQL'
DROP TABLE tasks;
ALTER TABLE tasks_backup_plan58 RENAME TO tasks;
SQL
```

The restore reverts the schema reshape. The daemon at HEAD will then refuse to start because the `tasks` table no longer has `phase` etc. — that's the expected guardrail. To run the post-plan-58 daemon you MUST re-apply `plans/58-migration.sql`; the restore path is for diagnosing the migration itself, not for running long-term on the legacy schema.

**Drop the backup** once the cutover has been verified across several tasks moving through transitions cleanly:

```bash
sqlite3 .quikode/quikode.db 'DROP TABLE IF EXISTS tasks_backup_plan58;'
```

## Plan 59 transient_retry_delays_s tuning

`cfg.transient_retry_delays_s: dict[str, int]` is the worker-layer cadence for retryable transient failures. Default: `{"quota_exhausted": 600, "container_vanished": 15, "auth_refresh": 60}`.

**When to adjust which category:**

- **`quota_exhausted`** — the operator's preferred re-attempt cadence when all providers in the fallback chain return quota. 600s (10 min) is the default; tune down to 300 if the operator wants more aggressive retries on quota recovery (e.g. Z.ai's 5-hour usage window reset is observably faster than 10 min), tune up to 1800 if the operator wants to conserve API calls while quota windows are wide open.
- **`container_vanished`** — the very-short cadence for retrying after a Docker host hiccup. 15s default is correct under normal load; bump to 30 if the host is consistently slow to re-attach a container post-incident (e.g. WSL hypervisor under heavy churn).
- **`auth_refresh`** — the cadence between auth-refresh-race retries (plan 44). 60s default is correct for codex-direct OAuth refresh; the codex CLI typically re-uses the new token on the next invocation, so the operator rarely needs to tune this.

**How to tune:**

Edit `.quikode/config.toml`:

```toml
[transient_retry_delays_s]
quota_exhausted = 300
container_vanished = 30
auth_refresh = 60
```

Restart the daemon. Plan 50's orphan-field audit catches typos at daemon start.

**Don't** add categories beyond the three above unless `subtasks._record_transient_subtask_failure` actually classifies a fourth category — the dict is consulted by category name, and a misspelled or extraneous key is silently ignored (but does generate a plan-50 orphan-warning if it's a top-level TOML key, which is the wrong shape anyway — this is a nested dict).

## Fruit-of-rotten-tree wipe

Triggered after **any change that tightens the stacking gate** — flipping `cfg.stacking_readiness` from `"speculative"` to `"settled"`, raising `cfg.review_ready_settle_s`, or shipping a plan that adds a new readiness predicate.

The new gate only governs FUTURE picks. Tasks already on disk with worktrees + branches were forked under the OLD looser gate, often off parents that were:

- Still in PROVISIONING / PLANNING / DOING_SUBTASK (no real branch yet)
- In PENDING_CI but with CI not yet green
- Mid-audit-cycle (any of the AUDIT_LOCAL_CI / AUDIT_RUBRIC / AUDIT_STANDARDS / AUDIT_ARCHITECTURE / AUDIT_BEHAVIOR states, or in FIXUP_PLANNING) with the parent's behavior actively churning

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
    'audit_local_ci', 'audit_rubric', 'audit_standards', 'audit_architecture',
    'audit_behavior', 'local_ci_checking',
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
