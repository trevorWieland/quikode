# 2026-05-07 — container-vanished cascade recovery

A host-wide docker incident at ~00:58–01:02 UTC simultaneously took out 16
of the workspace's dev containers. Compounded by an orchestrator-race
condition (a second daemon's `cleanup_all_quikode` running while prior
worker threads were still alive), this left:

- **12 tasks BLOCKED** at the per-subtask 50-attempt hard ceiling,
  retry-cause histograms dominated by `container_vanished=30..44`.
- **4 tasks FAILED** with `InvalidTransition` errors from FSM events
  fired against rows that orphan-recovery had concurrently mutated.

Plan 20 ships the four patches that prevent this from recurring. This
checklist walks the operator (or a future agent under operator
authorization) through recovering the 16 affected tasks **without losing
committed work**.

## Pre-flight

1. Confirm the four patches are deployed:
   ```bash
   bash /home/trevor/github/quikode/scripts/reinstall.sh --skip-tests
   qk --version
   ```
2. Confirm the daemon is stopped (recovery actions modify state):
   ```bash
   cd /home/trevor/github/quikode-runs/tanren
   qk daemon status   # expect "no daemon" or "daemon dead"
   ```

## Recovery loop (per affected task)

For each task in the table below:

```bash
cd /home/trevor/github/quikode-runs/tanren
qk unblock <TASK>           # read-only forensics — confirms current state
# apply worktree intervention if the table calls for one
qk reset-retries <TASK>     # zero retries on every blocked subtask
qk resume <TASK>             # BLOCKED/FAILED → PENDING with resume marker
```

After all 16 are processed, restart the daemon:

```bash
cd /home/trevor/github/quikode-runs/tanren
qk daemon start --detach --max-parallel 16 --retry-failed
qk daemon status   # expect "daemon alive ... heartbeat fresh"
qk briefing | head -30
```

## Per-task table

Drawn from the 16 parallel subagent investigations on 2026-05-07. Each row
names the blocked subtask + any worktree-level intervention required
**before** running `qk reset-retries`. Tasks with `none` need no manual
worktree edits — the new prompts + container recreation give them a clean
shot.

| Task | Blocked subtask | Worktree intervention |
|---|---|---|
| R-0002 | `F-1-13-openapi-typescript-org-contracts` | none — doer's next attempt should add the 4 missing imports for `OrganizationView`, `OrganizationMembershipView`, `OrgPermission`, `OrganizationAdminOperation` per attempt-32 triage |
| R-0003 | `S-03-service` | none — first attempt was real work; attempts 2-50 were pure container-vanished noise |
| R-0005 | `S-10-bdd-B-0044` | inspect `<wt>/tests/bdd/features/B-0044*.feature`; B-0044 may need by-hand attention given the 42-attempt history before the FSM crash |
| R-0006 | `F-1-12-account-mutation-idempotency` | none — last real triage (attempt 12) was converging on a rustfmt-only blocker |
| R-0007 | `S-07-web` | none — attempts 1–18 were progressing on real Turbopack fd-exhaustion + Playwright hydration mismatch |
| R-0008 | `F-1-1-bdd-step-and-perimeter-witnesses` | none — attempt 24 surfaced a real scope-reviewer rejection on MCP contract overreach; doer should narrow the diff |
| R-0009 | `S-02-session-revocation` | none — consider `cfg.subtask_doer_timeout_s` bump (current 1200s was insufficient for this 16-file slice on opencode/glm) |
| R-0015 | `S-03-service-posture` | none — first 13 attempts were progressing |
| R-0019 | `F-1-14-service-owned-project-provider-boundary` | none — was at attempt 25 of legitimate work when the FSM race fired |
| R-0020 | `F-1-6-remove-project-test-hook-http` | none — was looping on rc=124 transient-retry, no real failure to address |
| R-0021 | `F-1-10-project-flow-observability` | none — attempt 13's triage cited a single concrete acceptance gap that should close |
| R-0023 | `F-1-4-source-control-backed-install-delivery` | none — single CLI branch-prep gap remained; doer can address |
| R-0024 | `F-2-6-performance-and-gate-cleanup` | **`git checkout HEAD --` 5 stale F-2-5 spillover files**: `crates/tanren-bdd/src/steps/install_drift.rs`, `crates/tanren-testkit/src/harness/install_drift.rs`, `docs/behaviors/B-0069-detect-installer-drift.md`, `docs/roadmap/dag.json`, `tests/bdd/features/B-0069-detect-installer-drift.feature`. Without this, the doer keeps re-introducing them and scope-reviewer keeps rejecting. |
| R-0025 | `F-1-9-standards-failure-contract` | inspect attempt-10 worktree edit on `crates/tanren-api-app/src/lib.rs` — the `StandardsFailureBody` re-export was scope-rejected; either justify in next doer summary or revert |
| R-0026 | `F-1-4-vite-web-routing` | none — first doer attempt (vite migration) timed out after 1263s leaving the container dead; subsequent 49 attempts were noise |
| R-0027 | `F-1-1-storybook-react-vite` | inspect `<wt>/pnpm-lock.yaml:1777` deprecated metadata — pnpm 10.x writes deprecation entries on touched packages; either adjust acceptance criterion or accept the metadata refresh |

## Verification after recovery

After the daemon is back up:

```bash
qk briefing                            # state distribution: 0 blocked, 0 failed
grep -c "InvalidTransition" .quikode/logs/daemon.log    # expect 0 new
grep -c "quota exhausted" .quikode/logs/daemon.log      # plan 19A active
grep -c "dev container recreated" .quikode/logs/daemon.log  # plan 20 1B active
```

Soak for at least 30 minutes, watching `qk briefing`'s recent transitions
and per-task `retries` counters. If any task's retries climb >5/min for
the same subtask, that's a regression — kill the daemon and investigate
before continuing.

## Why no full wipes

`qk retry <task>` clears the worktree + branch + every subtask row, so
all already-committed work is discarded. Each affected task here has
6–20 hours of legitimate doer time committed (S-01..S-N or F-1-1..F-1-N),
and 13–24 of those committed subtasks/fixups in each task represent real
problem-solving. Reset-retries + resume preserves all of that and gives
the failing subtask a clean budget; the container recreation patch (1B)
ensures the next attempt actually runs against a live container.
