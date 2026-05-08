# Plan 36 — SELF_AUDIT denial-false-positive in `gate_standards`

## Diagnosis

Plan 33's deterministic short-circuit (`quikode/self_audit.py:_scan_risk_tokens`)
scans `RISK|STUB|TODO|FIXME|XXX` (case-insensitive, word-boundary) across every
rubric / standards / behavior row body and FAIL_FASTs on any hit.

On 2026-05-08, R-0023 / S-03-cli-install-entrypoint reached attempt 7
(retries=6, over the soft cap of 5) because its `gate_standards` row read:

```
profiles/rust-cargo/rust/error-handling.md§Error Handling: aligned
  (thiserror in library crate; #[non_exhaustive] on all error enums;
   no unwrap, expect, panic, todo, unimplemented ...
```

The doer is **denying** the deny-listed tokens — listing them to confirm
absence — but the regex catches the literal `todo` and short-circuits with
`failure_layer="self_audit_mismatch"`. The triage agent itself flagged it:
"the audit treats as a risk/stub signal even when phrased as a denial."

This recurs structurally on any subtask whose standards profile lists banned
tokens. `error-handling.md` is widely-applicable across rust subtasks, so
this is going to keep firing. The doer cannot avoid mentioning deny-listed
tokens when citing a standard whose Rules paragraph names them.

The same regex appears nowhere else in the codebase (`grep` confirms
`_RISK_TOKEN_RE` is unique to `self_audit.py`); `pre_pr_audit.py` does no
RISK/STUB/TODO scanning on doer output. Single-site fix.

## Decision

**Option 5: skip the risk-token scan on `gate_standards` rows where
`aligned=True`; keep scanning rubric rows, behavior rows, and *drifted*
standards rows unchanged.**

Rationale:
- An aligned standards row by construction is a denial — the doer is asserting
  the diff conforms. Listing deny-listed tokens to deny them is the canonical
  shape, not an admission. The LLM checker (which still runs after the parse)
  catches a falsely-claimed-aligned row whose body actually admits drift.
- Drifted standards rows are scanned unchanged: a doer admitting "drifted: still
  need to remove the TODO" is a real signal worth catching cheaply.
- Rubric rationale/evidence and behavior witnessed_by/output_excerpt are
  scanned unchanged: a doer writing "predicted_score=8 because we left a TODO"
  or "output_excerpt=TODO add real assertion" is a real admission. Those are
  the channels the scan was designed for.

Rejected alternatives:
- *Tighten the regex* (require `TODO:` / `TODO(`): brittle and applies to all
  sections including the channels where the current matcher is correct.
- *Drop the scan on `gate_standards` entirely*: loses the drift-admission
  signal for free.
- *Negation-aware regex*: very brittle.
- *Skip on `aligned=True` everywhere*: only `gate_standards` has aligned/drifted
  semantics; the field doesn't exist on rubric / behavior rows.

The `StandardsRow.aligned` flag already exists (`self_audit.py:96`,
parsed at `_parse_standards_row:277`), so this is a one-line predicate
change in `_scan_risk_tokens`.

## Design

```python
# quikode/self_audit.py:_scan_risk_tokens (~L450)
for key, srow in parsed.gate_standards.items():
    if srow.aligned:
        # An aligned standards row is, by construction, a denial of drift —
        # the doer typically lists deny-listed tokens (no unwrap, no todo,
        # no panic) to assert absence. The literal mention is not an
        # admission. The LLM checker verifies aligned claims against the
        # diff, so a falsely-aligned row with a real TODO admission still
        # fails downstream.
        continue
    if srow.body and _RISK_TOKEN_RE.search(srow.body):
        return f"gate_standards[{key}]: {srow.body[:120]}"
```

Update the module docstring §Short-circuit (L48-57) and §6.4 mention to note
the `aligned`-row carve-out so future readers don't reintroduce the false
positive.

## File list

- `quikode/self_audit.py` — `_scan_risk_tokens` carve-out (~5 LoC) + docstring
  update (~3 LoC).
- `tests/test_self_audit.py` — two new tests:
  - `test_short_circuit_risk_token_in_aligned_standards_row_is_proceed`
    — exact R-0023 fixture (aligned row body listing `todo`, `panic`,
    `unwrap` to deny them) → asserts `ShortCircuit.PROCEED`.
  - `test_short_circuit_risk_token_in_drifted_standards_row_still_fail_fast`
    — regression: drifted row body containing `TODO` still FAIL_FAST with
    `failure_layer="self_audit_mismatch"`.
  - Existing rubric/behavior/word-boundary tests stay green unchanged
    (verifies the carve-out is scoped correctly).

## PR sizing

Single PR, ~10 LoC of code change + ~30 LoC of test fixtures. No schema
change, no contract change, no prompt change, no migration. Subtask-doer
prompt is intentionally NOT touched — the structural fix lives in the parser
where the false-positive originated, not in the agent contract.

## Deploy

No `qk retry` required. In-flight tasks pick up the new parser on daemon
restart; worktree state and plans remain valid (parser-only behavior change,
no data shape change). Queue:

1. Land PR.
2. `bash scripts/reinstall.sh --skip-tests`.
3. `qk daemon stop && qk daemon start --detach --max-parallel 12 --retry-failed`.
4. R-0023 / S-03 picks up the fixed parser on next attempt.

## Validation

- `ruff check` + `ruff format --check` + `ty check` + `pytest tests/ -q` all
  green.
- New tests assert both directions (aligned-denial PROCEEDS; drifted-admission
  FAIL_FASTs).
- Existing parametrized risk-token tests
  (`test_short_circuit_risk_token_in_rationale`,
  `test_short_circuit_risk_token_in_behavior_excerpt`,
  `test_short_circuit_word_boundary_does_not_match_substring`) stay green
  unchanged.

## Confidence

**High.** The defect, the fix, and the blast radius are all small and
verifiable. The carve-out preserves the deny-list signal in every channel
where it's correct (rubric, behavior, drifted-standards) and removes it only
in the channel where it's structurally a denial (aligned-standards). The LLM
checker remains the authoritative verifier of aligned-row claims, so a doer
who falsely claims `aligned` to bypass the scan still fails downstream.
