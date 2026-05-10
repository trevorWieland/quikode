# Plan 48 — fix stop-loss signature granularity + resume retry reset

## Why

Plan 47 retired the doer envelope, removing a major source of variation
in retry signatures. In the post-plan-47 contract, every checker FAIL
produces a structurally similar outcome — and quikode's
`retry_classify._CHECKER_VERDICT_RE` regex is hunting for a JSON-shaped
`"verdict": "FAIL"` in `outcome.checker_text`, but the worker has been
passing it the *rendered* `VERDICT: FAIL\nROOT_CAUSE: ...` text since
plan 38. The regex never matches → the path falls through to
`("doer_output_invalid", "rc={N}")` for *every* non-transient retry.

Under plan 47's uniform diff-only contract, this means 5 consecutive
attempts on a subtask all record IDENTICAL `(doer_output_invalid, rc=0)`
signatures regardless of whether the actual failure layer was
`local_ci`, `behavior`, `rubric`, `standards`, or `architecture`. The
plan-23 same-signature stop-loss trips immediately.

Hard evidence (workspace at /home/trevor/github/quikode-runs/tanren,
2026-05-10 08:20):

```
R-0040 / F-CI-1: 5/5 entries (doer_output_invalid, rc=0)   [actually local_ci]
R-0008 / F-4-1:  5/5 entries (doer_output_invalid, rc=0)
R-0015 / F-3-1:  5/5 entries (doer_output_invalid, rc=0)
R-0024 / S-04:   5/5 entries (doer_output_invalid, rc=1)
R-0025 / S-03:   5/5 entries (checker_fail, verdict=FAIL) [no layer info]
R-0003 / F-2-12: 6/6 entries — first checker_fail, rest doer_output_invalid
```

5 tasks blocked in the first ~20 minutes after the plan-47 daemon
restart. Yesterday under plan 38 (with envelopes) this was not happening
because the envelope-shape variation produced enough signature variation
that 5 consecutive *identical* signatures was rare. Plan 47 removed the
variation; the bug is now exposed.

Concurrent secondary complaint (user-stated): `qk resume` after a block
preserves `retry_reasons`, so the resumed attempt counts toward the
stop-loss against history that the operator has explicitly said to
disregard. This is wrong — resume should clear stop-loss state.

## What ships

### Fix 1: classifier reads structured verdict, not rendered text

`quikode/retry_classify.py`:

- Update `classify_retry` to accept an optional `verdict: str | None`
  parameter (`"PASS"|"FAIL"|None`). When `hint="checker"` and `verdict`
  is provided, classify directly:
  - `verdict == "FAIL"` → `("checker_fail", "verdict=FAIL")`
  - `verdict == "PASS"` → not a retry; caller should never reach here
- Drop `_CHECKER_VERDICT_RE`. Stop scraping the rendered text.
- Keep the rest of the pattern dictionary (oom, vanished, network,
  pre_commit, timeout) — those still operate on rc/stderr/stdout.
- `_classify_retry_hint` simplifies: hint="checker" → defer to verdict
  caller plumbed in the line above; hint="pre_commit" / "network" stay
  as-is.

`quikode/workers/subtasks.py:_append_retry_reason`:

- Accept `verdict: str | None` (and `failure_layer: str | None`, see
  Fix 2). Pass them through to `classify_retry`.
- The caller (`_record_subtask_triage`) has the structured outcome and
  the structured triage output — pass `verdict=outcome.verdict.name`
  (or equivalent) and `failure_layer=triage.failure_layer`.

### Fix 2: signature includes failure_layer when known

`quikode/retry_classify.py`:

- `classify_retry` accepts an optional `failure_layer: str | None`
  argument. When provided AND the resulting category is `checker_fail`
  or `doer_output_invalid` (i.e. work-content failures), embed it in
  the signature: `f"verdict=FAIL,layer={failure_layer}"`.
- `failure_layer=None` → preserve the prior signature shape (no layer
  suffix). This keeps non-checker retry paths unaffected.

`quikode/workers/subtask_execution.py:_triage_subtask`:

- Currently returns a single `str` (the rendered triage text). Change
  to return a tuple `(text, failure_layer)` where `failure_layer` is
  `result.structured.failure_layer` when a `SubtaskTriageOutput` was
  produced, else `None` (transport / parse failure paths).
- Fix all callers to unpack the tuple.

`quikode/workers/subtasks.py:_record_subtask_triage`:

- Receives the `(text, failure_layer)` tuple from `_triage_subtask`.
- Returns the text (unchanged outward shape).
- Passes `failure_layer` plus the structured `outcome.verdict` into
  `_append_retry_reason`.

### Fix 3: resume clears retry_reasons on blocked subtasks

`quikode/cli_lifecycle.py:resume`:

- After the existing FSM event + subtask re-pend, also clear
  `retry_reasons=None` and `retries=0` for every subtask whose state
  was `"blocked"` at the moment of resume. This is the user's mental
  model: a manual resume means "give this a fresh budget."
- Do NOT clear retry_reasons for `"done"` subtasks (that's the audit
  trail of legitimately-converged work) or `"pending"` subtasks
  unblocked-by-association (they have no history to clear).
- The pre-existing `reset-retries` command's behavior is unchanged —
  it remains the explicit primitive for clearing without state change.
- Document the new behavior in the resume command's docstring + the
  resume CLI help text.
- Update the orientation-described semantics implicit in §3.1's
  primitives table: `qk resume` now also discards retry_reasons of
  blocked subtasks (still preserves: worktree, branch, all subtask
  rows that were already done, retry counters of done subtasks, plan).
  Don't update orientation.md as part of this PR — that's a follow-up.

### Tests

- `tests/test_retry_classify.py`:
  - New: when `verdict="FAIL"` and `hint="checker"`, returns
    `("checker_fail", "verdict=FAIL")` regardless of stdout shape.
  - New: when `failure_layer="local_ci"`, signature includes
    `,layer=local_ci`.
  - Update existing tests that asserted rendered-text regex matching —
    the regex is gone.
- `tests/test_workers_subtasks.py` (or wherever signature stop-loss
  tests live):
  - New: 5 retries with different `failure_layer` values do NOT trip
    same-signature stop-loss.
  - New: 5 retries with the same `failure_layer` DO trip stop-loss.
- `tests/test_cli_resume.py` (or equivalent):
  - New: `qk resume` on a task with one blocked subtask carrying
    populated retry_reasons sees that subtask's retry_reasons cleared.
- Update fixtures and any tests that constructed a retry_reasons
  signature assertion against the old shape.

### Plans index

Add plan 48 row to `plans/00-INDEX.md`.

## Operational followup (manager handles, not the agent)

After the agent ships:

1. Validation ladder green (ruff check + ruff format check + ty check
   + pytest).
2. Commit + push to `optimizations`.
3. `bash scripts/reinstall.sh --skip-tests`.
4. `qk daemon stop && qk daemon start --detach --max-parallel 12`.
5. Recover the 6 currently-blocked tasks per §3 escalation, preferring
   `qk rewind` for tasks with many done predecessors:
   - R-0003 (24 done): `qk rewind R-0003 F-2-12-...`
   - R-0040 (34 done): `qk rewind R-0040 F-CI-1-...`
   - R-0008 (40 done): `qk rewind R-0008 F-4-1-...`
   - R-0015 (26 done): `qk rewind R-0015 F-3-1-...`
   - R-0019 (rewind already used → assess; possibly retry)
   - R-0025 (2 done): rewind or reset+resume — small enough to redo
6. Watch first wave under the new signature shape.

## Out of scope

- Whether blocked tasks should yield their slot to PENDING tasks
  (separate plan 49 candidate — investigate scheduler behavior under
  block).
- Tuning `subtask_same_signature_block_count` from 5 to higher; the
  layer-included signature should make this unnecessary, but if it
  isn't, raise in a follow-up.
- Orientation §3.1 primitives table update; do that in a docs sweep.
