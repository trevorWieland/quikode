# Plan 51 — empty doer diff is a transport-class failure, not a content-class failure

## Why

2026-05-10 morning: GLM-5.1-zai (and Wafer in the fallback chain)
silently returned rc=0 in 91s avg with empty stdout and zero
worktree edits across 433 doer calls in 6h. Every checker call after
that graded an empty diff, voted FAIL, the triage classified the
failure layer as `local_ci`/`rubric`/`transport` depending on the
content of the (empty) artifact, and same-signature stop-loss
tripped after 10 attempts. The cascade looked like quikode-internal
bugs (and we shipped plans 48, 49, 50 chasing it) but the upstream
cause was the doer not doing its job.

The current pipeline doesn't distinguish between:

1. **Content failure**: doer made a real diff that fails one of the
   pre-PR gauntlet stages. Productive iteration: triage tutors, next
   attempt has new information, eventually converges or a real
   non-convergence signal blocks.
2. **Transport failure (null diff)**: doer returned rc=0 but
   `git status --porcelain` is empty. No work product. Triage has
   nothing to teach against. Each retry is identical to the last by
   construction; convergence is impossible because nothing happened.

These are categorically different and deserve different treatment:

- Content failure → grade, triage, retry. Same-signature stop-loss
  is the right cap (now 10).
- Transport failure → fast-fail. The doer transport is broken; no
  amount of retry on the same transport will fix it. The cap should
  be aggressively low (2 or 3), the failure_layer should be
  `transport` regardless of what the LLM checker says about an empty
  diff, and ideally the system should auto-swap the doer model
  in-flight via the existing fallback-chain primitive.

## What ships

### Worker-level empty-diff detection

`quikode/workers/subtask_execution.py`:

After `_run_doer_agent` returns and before `_check_subtask` runs,
inspect the captured diff (`self._last_diff_text` populated by
`_cache_doer_state`). If the diff is empty AND `git status
--porcelain` returned no entries, the doer produced no work.

Define `_classify_empty_diff_outcome(self, subtask)`:

- Synthesize a `_CheckerOutcome` with:
  - `verdict = Verdict.FAIL`
  - `checker_text = "VERDICT: FAIL\nROOT_CAUSE: doer produced no diff
    (transport-class failure). git status --porcelain returned 0
    entries. Skipping LLM checker; this is not a content-grading
    failure."`
  - `transient = False` (it's a deterministic miss, not a flake)
  - `rc = None`, `stderr = ""`
- Persist a dedicated artifact `subtask_empty_diff:<subtask_id>` so
  the operator can see the empty-diff history in `qk show`.

Then in `_check_subtask`:

- After running the objective check (`just check`) and BEFORE
  invoking the LLM checker, check the diff. Empty → return the
  synthesized empty-diff outcome (skip the LLM checker call to save
  tokens; the diff is empty, the checker has nothing to read).

### Triage classification

`quikode/workers/subtasks.py:_record_subtask_triage` already plumbs
`failure_layer` from triage. When the failure is empty-diff, the
triage agent doesn't run productively against an empty diff —
override the failure_layer at the call site:

- If `outcome.checker_text.startswith("VERDICT: FAIL\nROOT_CAUSE:
  doer produced no diff")`, skip the triage call entirely. Synthesize
  a `(text, "transport")` tuple where text is a brief operator-facing
  note.
- Pass `failure_layer="transport"` into `_append_retry_reason`. The
  signature becomes `verdict=FAIL,layer=transport`.

### Aggressive transport stop-loss

`quikode/workers/subtasks.py:_maybe_signature_stop_loss`:

The existing logic compares last N retry signatures. Add a parallel
check: when the last K retry signatures are ALL transport-class
(`category in {checker_fail, doer_output_invalid}` AND signature
includes `,layer=transport`), trip a transport stop-loss with a
distinctly worded message:

```
transport stop-loss: last K non-transient retries had empty diffs
(layer=transport). The doer model is not producing work product.
Check `cfg.<role>_model` and the underlying transport (litellm
bridge, codex profile, provider quota). Operator action:
swap subtask_doer_model to a known-reliable model and resume.
```

K should be smaller than the same-signature N — default 3.
Configurable via new `cfg.subtask_transport_stop_loss_count` (Field
default 3, ge=2, le=10). PLUMB IT IN load_config (audit warns are
in place after plan 50, so missing this would surface).

### Tests

- `tests/test_subtask_execution_new_loop.py` (or similar):
  - New: doer returns with empty diff → `_check_subtask` short-circuits
    to the synthesized empty-diff outcome without invoking the LLM
    checker.
  - New: triage skip when checker_text indicates empty-diff transport
    failure.
- `tests/test_workers_subtasks.py`:
  - New: 3 consecutive `(checker_fail, layer=transport)` retries trip
    the new transport stop-loss with the distinct message.
  - New: 3 consecutive `(checker_fail, layer=local_ci)` retries do NOT
    trip the transport stop-loss (only same-sig at the higher cap).
- `tests/test_retry_classify.py`: no changes (the layer suffix logic
  already handles `transport` correctly).
- `tests/test_config_loader_audit_log.py`: new test that
  `subtask_transport_stop_loss_count` override takes effect.

### Plans index

Add plan 51 row to `plans/00-INDEX.md`.

## Operational followup (manager handles)

After agent ships:

1. Validation ladder green.
2. Commit + push.
3. Reinstall + daemon restart.
4. The transport stop-loss now fires faster on empty-diff cascades; if
   GLM/Wafer regresses again, operator notification arrives within 3
   attempts instead of 10.

## Out of scope

- Auto-swap doer model in-flight when transport stop-loss trips
  (plan 52 candidate — needs the model registry's quota fallback
  chain to be repurposed for transport failures, with care not to
  burn through the chain on a real bug).
- Investigating GLM/Wafer transport reliability itself (separate
  agent dispatched 2026-05-10 with sandbox in fixture project).
- Changing the same-signature stop-loss for non-transport layers.
