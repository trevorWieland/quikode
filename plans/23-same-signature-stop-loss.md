# Plan 23 — same-signature stop-loss

## Symptom

R-0005/S-10-bdd-B-0044 burned 47 retries (~17 hours of subscription
time on one parallel slot) without the BLOCKED-on-flatline detector
ever firing. Histograms:

- `flatline_count = 1`
- `progress_check_count = 14`
- `retries = 47`

The flatline detector relies on the progress-check agent's verdict
(`progressing | uncertain | flatlined`). Each attempt produced
different-but-equally-invalid output (different files investigated,
different rationales), so the agent rated it "progressing" or
"uncertain" — even though every single attempt's `retry_reasons` row
landed with the same `(category, signature)` tuple
`("doer_output_invalid", "rc=0")`.

The flatline detector is a fine *qualitative* signal but it can't
catch retry storms with stable failure fingerprints when the verdict
agent can't see the fingerprint stability.

## Fix

A second, deterministic block path that fires on
`(category, signature)` repetition in `retry_reasons`. Independent of
progress-check verdicts; transient retries excluded so infra noise
doesn't trip it.

1. `quikode/config.py`: new `subtask_same_signature_block_count`
   (default 5, range 2–20). Default chosen to be permissive — five
   identical-signature failures in a row is unambiguous deadlock,
   five non-transient retries with the same `(category, signature)`
   tuple after triage feedback means triage isn't moving the doer.
2. `quikode/store_forensics.py`: new `last_n_retry_signatures(task_id, subtask_id, *, limit)`
   reads the `retry_reasons` JSON column, filters out transient
   entries, returns the trailing N as `(category, signature)`
   tuples.
3. `quikode/workers/subtasks.py`: new `_maybe_signature_stop_loss(subtask)`
   returns a block reason string when the trailing N tuples are all
   identical. Called from `attempt_loop` immediately after
   `_record_subtask_triage`, before `_maybe_record_progress_block`,
   so the cheaper deterministic check wins the race when both would
   fire.

## Coexistence with the flatline detector

The flatline detector still runs. The two block paths cover
non-overlapping failure modes:

- **Flatline (existing):** progress-check agent sees no narrowing
  across attempts. Subjective.
- **Same-signature (new):** classifier sees identical
  `(category, signature)` tuples regardless of progress agent
  verdict. Objective.

R-0005/S-10 would have hit the same-signature stop-loss at attempt
~15 (the first 5 post-recovery `doer_output_invalid rc=0` retries),
which surfaces it to the operator ~12 hours sooner than the actual
hard ceiling.

## Why not bake same-signature into the flatline detector

The flatline counter resets on any non-flatlined progress verdict.
Folding signature stability into it would require either: (a) running
the progress agent every attempt (expensive — currently every 3rd),
or (b) ignoring the progress agent on signature ties (kills the
existing detector's value). Keeping them separate is cheaper and lets
each speak with its own voice.

## Excluded by design

- Transient retries (container OOM, infra glitches) — they describe
  the environment, not the doer's output. Already captured as
  `transient=True` in retry_reasons; the new query filters them out.
- The first N attempts (when there are fewer than N non-transient
  reasons in history). The function returns whatever it has; the
  caller short-circuits when `len(sigs) < n`.

## Validation

- New tests:
  - `test_same_signature_stop_loss_blocks_after_n_identical_failures`
    — uniform rc=0 + checker FAIL → block at the Nth retry.
  - `test_same_signature_stop_loss_excludes_transient_reasons` —
    transient entries don't count.
- All existing progress-check fixtures updated to set
  `subtask_same_signature_block_count=20` (effectively disabled) so
  their uniform-signature setups still test the flatline path they
  were written for.
- Full suite: `uv run pytest tests/ -q` — 854 passed.
