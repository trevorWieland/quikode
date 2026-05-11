# Plan 58 — flatten FSM, unify fixup flow, add lifecycle phases/cycles

## Why

Today's FSM has two umbrella states that mask multi-agent activity:
- `PRE_PR_AUDITING` covers the 5-stage audit gauntlet (local_ci, rubric,
  standards, architecture, behavior) plus fixup-planner emission and
  per-subtask doer/checker/triage loops
- `ADDRESSING_FEEDBACK` covers post-PR fixup work (CI failures, review
  feedback) using the same inner machinery but a divergent worker
  driver

The audit (audits/fsm-state-flattening-audit.md) confirmed the inner
machinery IS unified (`_run_fixup_round` → fixup planner →
`_run_subtask_set` → triage). The divergence is purely at the outer
wrapping: pre-PR re-runs the gauntlet after fixups; post-PR returns
to PENDING_CI.

Plan 58 collapses the umbrellas into clean atomic states + unifies
the worker drivers + adds an explicit lifecycle phase/cycle layer
that gives operator visibility into "where in the broader lifecycle
is this task."

## What ships

### A. State enum: remove umbrellas, add audit-stage states

**Remove from `State` enum:**
- `PRE_PR_AUDITING`
- `ADDRESSING_FEEDBACK`

**Add to `State` enum** (5 new audit-stage states):
- `AUDIT_LOCAL_CI`
- `AUDIT_RUBRIC`
- `AUDIT_STANDARDS`
- `AUDIT_ARCHITECTURE`
- `AUDIT_BEHAVIOR`

**Keep + reuse for fixup work regardless of trigger:**
- `FIXUP_PLANNING` (already exists; now serves all triggers)
- `DOING_SUBTASK` / `CHECKING_SUBTASK` / `TRIAGING_SUBTASK` (already exist)
- `COMMITTING` / `PUSHING` (already exist)

The plan-54 hack where `enter_doing_subtask/etc.` no-op'd when parent
was `ADDRESSING_FEEDBACK` goes away. The doer/checker/triage states
are now valid first-class transitions from `FIXUP_PLANNING` or from
audit-stage states, regardless of why the fixup is happening.

### B. Worker driver consolidation

Replace the divergent flows:
- `workers/pre_pr.py:_run_pre_pr_pipeline` (today: handles pre-PR
  gauntlet + fixup cycles)
- `workers/pr_lifecycle.py:_run_ci_fix_response` (today: handles
  post-PR CI-failure fixup)
- `workers/feedback.py:run_changes_requested_response` (today:
  handles post-PR review-feedback fixup)

…with ONE driver method `_run_audit_cycle(trigger_source)`. The
method:
1. Enters the first audit stage (`AUDIT_LOCAL_CI`) and walks all 5
   stages sequentially, firing the matching audit agent at each.
2. If all 5 pass cleanly → exit audit cycle to next phase (push or
   merge depending on trigger).
3. If any stage fails → enter `FIXUP_PLANNING`, fixup planner emits
   subtasks, loop through `DOING_SUBTASK → CHECKING_SUBTASK →
   TRIAGING_SUBTASK` per subtask. When all fixup subtasks pass,
   re-enter `AUDIT_LOCAL_CI` for the next cycle.
4. Release valve at cycle 5 (configurable via existing
   `pre_pr_release_valve_after_cycles`) — same threshold per series.

The trigger_source parameter:
- `INITIAL_AUDIT` — pre-PR first-time audit after initial subtasks done
- `CI_FAILURE` — post-PR; GitHub CI flagged failures
- `REVIEW_FEEDBACK` — post-PR; CHANGES_REQUESTED review came in

The OUTER wrapping (what state the task lands in after audit settles)
branches on trigger_source:
- `INITIAL_AUDIT` clean → `PUSHING` → `PENDING_CI` → ...
- `CI_FAILURE` clean (re-audit passes) → `PUSHING` → `PENDING_CI` (let
  GitHub re-grade; GitHub is truth)
- `REVIEW_FEEDBACK` clean → `PUSHING` → `PENDING_CI` (same; the human
  reviewer + GitHub CI re-evaluate)

### C. Lifecycle phase + cycle columns on tasks

Three new columns on the `tasks` table:

- `phase`: enum {`INITIAL`, `PRE_PR_REVIEW`, `PR_REVIEW`}
- `cycle_in_phase`: int (resets to 1 at phase transitions)
- `pr_review_trigger`: enum {`NONE`, `CI_FAILURE`, `REVIEW_FEEDBACK`}
  — meaningful only when `phase = PR_REVIEW`

**Phase semantics:**

| Phase | When the task is in it | Cycle increments when |
|---|---|---|
| `INITIAL` | task created → first audit | (never; INITIAL is always 1 cycle by definition) |
| `PRE_PR_REVIEW` | first audit start → PR opens on GitHub | each new fixup cycle (after fixup planner emits) |
| `PR_REVIEW` | PR opens → MERGED / CLOSED | each new CI-failure-triggered or review-feedback-triggered fixup cycle |

**Phase transitions are explicit FSM events:**

- `INITIAL → PRE_PR_REVIEW` fires when all initial-cycle subtasks
  reach `done` and the worker enters the first `AUDIT_LOCAL_CI`. Set
  `phase = 'PRE_PR_REVIEW', cycle_in_phase = 1`.
- `PRE_PR_REVIEW → PR_REVIEW` fires on `PR_OPENING` (or whenever the
  PR is first opened to GitHub). Set `phase = 'PR_REVIEW',
  cycle_in_phase = 0` (incremented to 1 when the first PR-review
  fixup trigger fires).

**Cycle increments within `PR_REVIEW`:**
- CI failure detected → cycle_in_phase += 1, pr_review_trigger =
  CI_FAILURE
- CHANGES_REQUESTED review received → cycle_in_phase += 1,
  pr_review_trigger = REVIEW_FEEDBACK

### D. Release valve re-keyed by phase

Today's `pre_pr_release_valve_after_cycles = 5` applies only to the
pre-PR umbrella. After plan 58, the same threshold applies per-phase:
- `PRE_PR_REVIEW` cycle 5 → release valve (open PR with deferred
  findings)
- `PR_REVIEW` cycle 5 (per trigger source) → release valve (push +
  let GitHub + reviewer decide)

Rename the config field for clarity:
- `pre_pr_release_valve_after_cycles` → `release_valve_after_cycles`
  (applies to both phases)
- `pre_pr_release_valve_defer_stages` → `release_valve_defer_stages`
- `pre_pr_release_valve_max_critical_findings` →
  `release_valve_max_critical_findings`

Plan 50's audit-warns will catch the rename if any old TOML key
sticks around in a workspace config. Operator's `qk-tanren-runs`
workspace will need a one-line edit alongside the migration.

### E. TUI + state_log rendering

The header `WorkspaceHeader` and the task table render `phase ·
cycle X · state` everywhere. Example detail panel for a task in PR
review fixing a CI failure:
```
state:  doing_subtask
phase:  PR_REVIEW · cycle 2 · trigger=CI_FAILURE
agent:  subtask_doer in flight 18s on F-CI-2-...
```

State-log entries include phase + cycle alongside state for full
historical context.

`qk briefing` adds a phase tier breakdown: "INITIAL 218 · PRE_PR_REVIEW
4 · PR_REVIEW 3" so the operator sees lifecycle depth at a glance.

### F. Hard cutover migration SQL

Ships at `plans/58-migration.sql`. Operator runs:
```
qk daemon stop
sqlite3 .quikode/quikode.db < /path/to/plans/58-migration.sql
qk daemon start --detach --max-parallel 12
```

The SQL:
1. Backs up the existing `tasks` table to `tasks_backup_plan58`.
2. Adds `phase`, `cycle_in_phase`, `pr_review_trigger` columns.
3. Derives phase from existing state per the rules above:
   - state IN (pending, provisioning, planning) AND
     no done subtasks → INITIAL, cycle 1
   - state IN (pre_pr_auditing, fixup_planning, local_ci_checking)
     AND pr_number IS NULL → PRE_PR_REVIEW, cycle = MAX(planning_cycle)
     from non-initial subtasks
   - state IN (pending_ci, awaiting_review, addressing_feedback)
     OR pr_number IS NOT NULL → PR_REVIEW, cycle = MAX from
     PR_REVIEW-phase subtasks
4. Maps deprecated states:
   - pre_pr_auditing → pending (with resume_from_existing_subtasks=1)
   - addressing_feedback → pending (with resume_from_existing_subtasks=1)
   Worker re-enters the unified driver fresh.

### G. Tests

New + updated:
- `tests/test_workers_unified_audit_driver.py` (new) — covers the
  consolidated `_run_audit_cycle` for INITIAL_AUDIT / CI_FAILURE /
  REVIEW_FEEDBACK triggers; asserts state walks AUDIT_LOCAL_CI →
  AUDIT_RUBRIC → ... cleanly + falls through to FIXUP_PLANNING on
  finding, etc.
- `tests/test_workers_phase_transitions.py` (new) — INITIAL →
  PRE_PR_REVIEW transition fires correctly, cycle increments on
  fixup-planner emit, PR_REVIEW cycle resets and re-increments
  correctly.
- `tests/test_fsm_runtime_phase_transitions.py` (new) — covers the
  explicit phase-transition FSM events from plan 57's typed-guard
  framework.
- Updated tests: anything previously asserting `PRE_PR_AUDITING` or
  `ADDRESSING_FEEDBACK` state → updated to the new state names +
  phase semantics. Migration test covers the SQL.
- TUI rendering tests: header + detail panel render phase + cycle
  alongside state.

### H. Plans index + orientation

- Add plan 58 row to `plans/00-INDEX.md`.
- `orientation.md` §3 + §7 updated significantly:
  - §3 escalation primitives table: `qk replan-cycle` semantics
    re-explained against phase/cycle vocabulary
  - §7 invariants: bullets covering the unified audit driver, the
    phase/cycle lifecycle, the release valve per-phase

## Operational followup (manager handles)

1. Validation ladder green.
2. Commit + push.
3. **Pre-deploy:** daemon stop. Apply migration SQL. Verify task rows
   look right (`qk briefing` should show phase tier breakdown).
4. Daemon start. Watch first few transitions for any unexpected
   InvalidTransition skip-logs (plan 57's guards mean we won't crash
   but will log; if there's a high-frequency unexpected skip, something
   in the FSM event table needs adjusting).

## Out of scope

- Migrating apply_event raise behavior; plan 57's typed guards live
  only on the helper layer.
- Renaming `planning_kind` / `planning_cycle` on subtasks — they're
  semantically per-subtask emission-source metadata; phase + cycle on
  tasks is the higher-level concept. Both stay.
- Moving any judgment to fewer agents — model assignment stays per the
  tier split.
- A `qk replan-phase` primitive — possibly useful but not required by
  this plan; sketched for later.
