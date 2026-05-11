# Plan 60 — fixup_ci gate-promotion + Claude fallback chains + TUI/data-model bug bundle

## Why

Five real bugs uncovered today, all priority-shipping-tonight:

1. **Workers run the wrong gate on `fixup_ci` subtasks.** The objective
   gate uses `cfg.subtask_check_command` (`just check`) for everything
   except Z-99 stabilization. For a `fixup_ci` subtask — whose entire
   purpose is "fix what GitHub CI broke" — this is too narrow. R-0019
   F-3-1: `just check` passes locally, but `just ci` (which the
   GitHub runner runs) fails 7 Playwright BDD chromium tests with
   specific assertion mismatches. The doer correctly diagnosed "local
   gate green, empty diff" and the worker correctly surfaced
   `cannot_reproduce`, but the diagnosis was upstream-wrong: the local
   gate wasn't running the failing tests. Result: real GitHub CI bugs
   labeled as environmental drift. Operator can't fix this with any
   primitive.

2. **Claude-tier roles have no fallback chain.** Today's overnight
   Claude auth outage (00:18 CDT → AM) fast-failed 145 checker calls
   on `claude-sonnet-4-6` and stalled 13 tasks for hours. The GLM
   doer's `GLM-zai → GLM-wafer → claude-sonnet-4-6 → gpt-5.3-codex`
   chain handled provider failure cleanly. But `claude-sonnet-4-6`
   and `claude-opus-4-7` themselves had no fallbacks → when Claude
   died, they died. Operator currently has them reverted to OpenAI
   direct as a workaround.

3. **Cross-cycle subtask ID collisions.** Fixup planner emits subtasks
   numbered F-N-M-* per its OWN cycle. R-0019 has TWO `F-2-3-*`
   subtasks: cycle 3 fixup `F-2-3-closed-error-taxonomy-contracts`
   (done) and cycle 8 fixup_ci `F-2-3-rust-bdd-scenario-localize-and-
   fix` (currently doing). Both appear in the subtasks table; operator
   can't tell at a glance which the doer is on.

4. **TUI subtasks table doesn't scroll to bottom.** R-0041 has 44
   subtasks; the panel's DataTable shows the top rows but the
   operator can't reach F-6-3 or below. All rows ARE loaded (no
   LIMIT in the query); widget-height/overflow bug.

5. **Stale `last_error` after retry/reset.** R-0027/R-0047 hit retry
   that wiped their subtask rows; the task-level `last_error` field
   stayed populated ("exhausted hard ceiling of 50 attempts"), so TUI
   and briefing showed stale messages about non-existent subtasks.

## What ships

### Fix 1: fixup_ci gate promotion

`quikode/workers/subtask_execution.py:_run_subtask_check_command`:

Today's logic:
```python
if subtask.id == STABILIZATION_SUBTASK_ID:
    cmd_str = cfg.local_ci_command  # just ci
else:
    cmd_str = cfg.subtask_check_command  # just check
```

New logic: promote `fixup_ci` (and `fixup-ci`) kind subtasks to use
`local_ci_command` too. The subtask kind comes from the
fixup-coverage / fixup-ci planner emission paths. Both `Subtask.kind`
value forms exist in the wild (the `_is_fixup_ci_subtask` helper in
plan 53 already handles both — reuse it).

```python
def _gate_command(self, subtask):
    if subtask.id == STABILIZATION_SUBTASK_ID or _is_fixup_ci_subtask(subtask):
        return cfg.local_ci_command, cfg.local_ci_timeout_s
    return cfg.subtask_check_command, cfg.subtask_check_timeout_s
```

This fixes the R-0019 class: fixup_ci subtasks now objectively
reproduce the GitHub CI failure (or genuinely don't reproduce, in
which case plan 53's `cannot_reproduce` signal IS legitimate
environmental drift).

**Side effect:** fixup_ci subtasks will take significantly longer per
attempt (full `just ci` vs lightweight `just check`). Acceptable —
this is the right cost for correctness. Operator-tuneable via
`cfg.local_ci_timeout_s` (default 1800s; tanren currently 3600s).

### Fix 2: Claude fallback chains

`quikode/model_registry.py`: add `quota_fallbacks` to both Claude
models.

```python
ModelSpec(
    name="claude-opus-4-7",
    transport="claude",
    schema_enforcement="cli_native",
    claude_model_id="claude-opus-4-7[1m]",
    quota_fallbacks=("claude-sonnet-4-6", "gpt-5.5"),  # NEW
),
ModelSpec(
    name="claude-sonnet-4-6",
    transport="claude",
    schema_enforcement="cli_native",
    claude_model_id="claude-sonnet-4-6",
    quota_fallbacks=("gpt-5.5", "gpt-5.3-codex"),  # NEW
),
```

**Important:** the `_is_quota_exhausted` detector in
`quikode/agents/transient_quota.py` currently scans for explicit
quota signals (429, "usage limit", "rate-limit"). Claude CLI's auth
failure produces a DIFFERENT signal — typically rc=1 with auth-shaped
stderr ("Invalid API key", "authentication failed", "session
expired"). Today's outage fast-failed rc=1 in 3-4s with no
quota-style text. The fallback chain only walks on quota signals; it
would NOT have helped today even if the Claude models had been
chained.

So fix 2 has TWO parts:
- Add `quota_fallbacks` (as above)
- Extend `_is_quota_exhausted` to ALSO match Claude auth-failure
  signals: rc=1 + stderr containing any of `"Invalid API key" |
  "authentication" + ("failed" | "error") | "session expired" |
  "401 Unauthorized" | "403 Forbidden"`. Rename to
  `_is_quota_or_provider_unavailable` or add a sibling
  `_is_provider_unavailable` checked alongside.

Today's overnight outage would then have: checker fast-fails on
Claude with auth error → chain walks to gpt-5.5 → checker call
succeeds → no operator pain.

### Fix 3: planning_cycle prefix on fixup subtask IDs

`quikode/workers/fixup_coverage.py` (fixup planner emit path):

When the fixup planner returns subtasks named `F-N-M-...`, prefix
them with the actual planning_cycle the worker is about to stamp.
New ID shape: `F-c<CYCLE>-N-M-...` (e.g., `F-c8-2-3-rust-bdd-...`).

This makes the cycle-of-origin visible in the ID itself. The internal
N (within-cycle index) stays for fixup planner output stability.

For `fixup_ci` subtasks (different planner path), same treatment.

Migration: existing subtask rows keep their current IDs (no rewrites
to history). Only NEW emissions get the prefix. Operator may see a
mix during transition; that's fine, the prefix is purely cosmetic.

### Fix 4: TUI subtasks DataTable scroll

`quikode/tui/widgets/detail_panel.py`:

The DataTable in `subtasks-tab` doesn't scroll to all rows. Investigate
the DataTable's `max_height` / container constraints. Likely fix is
setting `max_height: 100%` on the table OR wrapping in `VerticalScroll`
so the host TabPane scroll handles overflow.

If textual DataTable has a known viewport-clipping behavior, the fix
might be `with VerticalScroll(): yield DataTable(...)` in the
`compose()` method. Test by manually verifying the operator can reach
the last row of a 44-subtask task.

### Fix 5: clear last_error on retry/reset

`quikode/cli_lifecycle.py:retry` and the related `_reset_to_pending`
helper:

When a task's subtask rows are wiped (retry primitive) OR when
operator-driven reset happens (any path that clears subtasks), the
task's `last_error` field should be cleared too. The current logic
preserves it for "forensic context" but that context is misleading
once subtasks are gone.

Add `last_error=NULL` + `failure_reason=NULL` to the column updates
in retry. Same for the `force_recover_to_pending_ci` escape hatch
added in plan 58 if it doesn't already clear them.

### Tests

- **Fix 1**: subtask with kind="fixup-ci" uses local_ci_command; with
  kind="fixup" (regular fixup) uses subtask_check_command;
  STABILIZATION_SUBTASK_ID still uses local_ci_command; default cases
  unchanged.
- **Fix 2**: 
  - claude-opus-4-7 + claude-sonnet-4-6 have the new fallback chains
  - `_is_quota_exhausted` (or sibling) returns True for Claude auth
    failure signatures
  - End-to-end: Claude transport returns auth-shaped rc=1 → fallback
    chain walks to next provider → call succeeds
- **Fix 3**: fixup planner emit path stamps `F-c<N>-` prefix; existing
  S-* and F-1-* tests adjusted; ID parsing in `_infer_planning_
  provenance` updated to recognize both old + new shapes.
- **Fix 4**: TUI smoke test that a task with 50+ subtasks renders all
  rows visible (programmatically iterate via the DataTable cursor).
- **Fix 5**: `qk retry R-XXXX` → task row's last_error is NULL after.

### Plans index + orientation

- Add plan 60 row to `plans/00-INDEX.md`.
- `orientation.md` §7 invariants: new bullet noting fixup_ci subtasks
  use full local_ci_command as their objective gate (one-line);
  Claude tier now has fallback chains parallel to GLM (one-line);
  planning_cycle prefix on fixup subtask IDs (one-line).

## Operational followup (manager handles)

After agent ships:
1. Validation ladder green
2. Commit + push + reinstall
3. Daemon stop → daemon start (no schema migration needed for plan 60)
4. Revert workspace config to use claude-opus-4-7 + claude-sonnet-4-6
   on their previously-assigned roles (the fallback chain is now safe)
5. Watch the next fixup_ci cycle — it should run `just ci` and either
   actually reproduce the GitHub failure or genuinely surface drift.

## Out of scope

- Renaming existing subtask rows with the new ID prefix scheme —
  cosmetic-only, mid-flight tasks keep their current names.
- A more nuanced quota/auth-failure distinction (today fix 2 lumps
  them; if it becomes important we can separate the categories
  later).
- Retroactive R-0019 cleanup — operator-mediated worktree fix per
  orientation §5.5 once fix 1 is live (workers will re-attempt with
  `just ci` and find the real test failures).

# CRITICAL: scope discipline

This plan has 5 fixes. ALL ship in one commit. None deferred. Plan 58
agent earlier today deferred refinements claiming they were
"orthogonal" — they weren't, and the deferral caused operational
pain. Plan 59 followed up to ship them. Plan 60 must NOT repeat that
pattern. If a fix feels orthogonal in isolation, it isn't — they're
all the operational consequence of bugs surfaced in the same
investigation cycle.

If something is genuinely impossible, surface via SendMessage. Do not
unilaterally drop scope.
