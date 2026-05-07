# Plan 26 — TUI gauntlet staleness + Z-99 resume guard + scroll fix

## Symptoms (all from R-0021 forensics, post-plan-24 ship)

1. **TUI shows stale pre-PR audit cycle 1 alongside `doing_subtask`
   running Z-99-stabilize-spec-gate.** The audit panel was rendering
   a 25.6-hour-old cycle 1 summary (local_ci ✗, rubric ✗, standards
   queued, behavior queued) while the task was actually back in the
   spec-subtask phase running my newly-injected stabilization
   subtask. Operator can't tell whether the audit failures are
   current or historical.

2. **Z-99 stabilization subtask started running mid-fixup-loop on
   resume.** R-0021 had completed cycle 1 of the audit, the fixup
   planner had emitted 11 `F-1-*` subtasks, 10 had landed, F-1-11
   was still in flight. Daemon restarted; my plan-24 injection
   re-parsed `plan_text` and added Z-99. The worker started Z-99
   *before* F-1-11 finished. Z-99's gate check can't possibly pass
   while F-1-11's fix isn't in the worktree, so Z-99 burns retries
   on a guaranteed-fail gate — exactly the cycle-1-failure cascade
   plan 24 was supposed to *prevent*, just relocated.

3. **Subtask-detail pane content gets clipped at the bottom of the
   panel.** Multi-line phase content (gauntlet block + phase label +
   container stats + state-long-description) overflows the static
   widget's allocated height and the operator can't scroll to see
   the rest.

## Fix 1 — TUI gauntlet hide-when-not-pipeline-relevant

`detail_panel.py:_gauntlet_block` previously rendered the gauntlet
panel iff a `pre_pr_audit_summary` row existed. New rule: also
require the task's current FSM state to be in
`_GAUNTLET_RELEVANT_STATES`:

```
{pre_pr_auditing, local_ci_checking, fixup_planning, committing,
 pushing, pending_ci, awaiting_review, merge_ready, merged,
 blocked, failed}
```

States explicitly excluded:
`pending, planning, provisioning, doing_subtask, checking_subtask,
triaging_subtask` — i.e. spec-subtask or fixup-subtask phases. When
the task is back in subtask work, the persisted summary represents a
prior cycle's findings, not the current concern; rendering it
alongside `doing_subtask` misleads the operator.

Doesn't address the underlying state-machine question of whether
the summary itself should be cleared at appropriate transitions —
that's plan 25 territory. This fix is purely the display layer
treating "task state" as the source of truth for "is this panel
about *now*?"

## Fix 2 — Z-99 injection guard on resume when fixups already exist

`workers/subtasks.py` resume path (`_run_planner` / parse from
saved `plan_text`) now queries the DB before parsing: if any
`kind="fixup-…"` subtask exists for this task, pass
`spec_gate_command=None` to suppress Z-99 injection. Rationale:

- Z-99 is a stabilization slot for *spec-phase completion*. Its
  purpose is to ensure the gate is green before the audit runs.
- Once cycle 1 audit has run and produced fixups, Z-99's job is
  effectively superseded by the in-flight fixups. The fixups are
  the gate-stabilization for that cycle.
- Re-injecting Z-99 mid-fixup creates a second stabilization slot
  that can't pass until the fixups land — wasting retries on a
  scheduled-too-early gate check.

New helper: `TaskWorker._has_existing_fixup_subtasks() -> bool`.
Lightweight DB lookup; called once per resume parse.

For tasks born after plan 24 ships, the normal flow is:

1. Planner emits subtasks → parse-time injection adds Z-99.
2. Spec subtasks run; Z-99 runs last; gate green; pre-PR pipeline
   starts.
3. If audit fails, fixup planner emits F-* — task continues.
4. Daemon restarts at any point: `_has_existing_fixup_subtasks` is
   `False` while in spec phase (no F-* yet), `True` after audit
   produced any. So Z-99 keeps being re-injected pre-cycle and
   stops being re-injected post-cycle. Correct in both directions.

Doesn't break: tasks already past spec phase get no retroactive
Z-99 (correct, would only conflict with their existing fixups);
tasks fresh in spec phase get Z-99 idempotently across resumes.

## Fix 3 — DetailPanel phase scrolling

`compose()` now wraps the `#detail-phase` Static in a
`VerticalScroll(id="detail-phase-scroll")` container so long phase
content (multi-cycle gauntlet, container stats, etc.) becomes
scrollable rather than clipped.

## Validation

- `uv run pytest tests/ -q` — all suites pass (only the pre-existing
  daemon.py budget guard fails, which is unrelated to this plan).
- Manual TUI verification post-ship: R-0021 detail panel no longer
  shows the stale gauntlet block while in `doing_subtask`, and the
  phase content is scrollable.

## Out of scope (queued)

- **Plan 25 (resume-resilience for partial cycles)**: clearing
  `pre_pr_audit_summary` at appropriate FSM transitions. Plan 26
  fixes the display symptom; plan 25 should fix the data lifecycle.
- **Storing per-cycle history**: the current schema overwrites the
  summary each `begin_pre_pr_audit_cycle`. A list of completed
  cycles would let `qk show` render full audit history. Not
  prioritized.
