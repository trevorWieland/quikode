# Plan 06 — progress-check signal quality

## Today

`workers/subtask_progress.py` runs a small agent every `subtask_progress_check_every`
attempts after `subtask_progress_check_after`. The agent sees the last few attempts'
checker root causes and triage notes, returns `progressing | flatlined | uncertain`.
Three consecutive flatlines → BLOCKED.

It works, but two failure shapes leak through:

1. **Same root cause, different *cause-of-cause*.** The doer is making progress on
   subproblems, but the surface-level error message doesn't change. e.g. checker keeps
   saying "test_foo failing" — but each attempt fixes a different inner reason. Agent
   sees "test_foo failing" three times → FLATLINE → BLOCKED. Real state: progressing
   on a hard test.

2. **Different root causes that are all variants of the same broken assumption.** e.g.
   attempt 1 fails compile in fileA, attempt 2 fails compile in fileB after partial
   fix, attempt 3 fails on a test. Agent sees three different root causes, returns
   PROGRESSING → keeps going until hard-max → BLOCKED with much wasted budget.

## What to feed the agent (in priority order)

1. **Diff-of-diffs.** For each consecutive attempt pair, the diff between attempt N's
   diff and attempt N-1's diff. If the doer is bouncing between two states (revert +
   re-edit), that's flatlined. If the diff is monotonically growing toward the spec,
   that's progressing.

2. **Failure-locality fingerprint.** Hash the (file, line) tuples mentioned in checker
   output. If the set is shrinking attempt-over-attempt, that's progressing even if
   any one error message repeats. If the set is stable or growing, that's flatlined.

3. **Doer's self-rationale shift.** If the doer's commit messages or stdout summaries
   shift from "fixing X" to "actually let me try Y" that's a heuristic for the doer
   recognising the dead end on its own — slightly bullish for progress.

(1) and (2) are deterministic and cheap. They don't require the agent at all. The agent
should run only as a tiebreaker when deterministic signals are mixed.

## Proposed flow

Replace the single agent call with:

```python
def progress_verdict(attempts) -> Verdict:
    locality = _failure_locality_change(attempts[-3:])
    if locality.shrinking:
        return Verdict.PROGRESSING
    if locality.stable_with_unchanged_root_cause:
        return Verdict.FLATLINED
    if locality.growing:
        return Verdict.FLATLINED  # diff growing without convergence = poison; see plan 05
    # Mixed signal — fall back to the agent for tiebreak
    return progress_agent_check(attempts)
```

## Wiring

`workers/subtask_progress.py` becomes a thin shim. Most of the deterministic logic
lives in a new pure-python `progress_signal.py` so it's unit-testable without docker
or agents.

## Tests

- 3 attempts touching the same set of files, root cause unchanged → FLATLINED.
- 3 attempts, error count shrinking 5 -> 3 -> 1 → PROGRESSING (no agent call).
- 3 attempts, error count growing 1 -> 3 -> 6 → FLATLINED (also feeds plan 05's
  poison detector).
- 3 attempts, mixed → fall through to agent stub.

## Tunables

- `progress_signal_min_attempts_for_locality_check = 3` — don't try to detect
  locality on a single attempt.
- `progress_signal_files_outside_scope_count_as_growth = true` — out-of-scope edits
  always count toward "growing" even if same total count.

## Risk

The locality fingerprint relies on `parse_ci_failure()` (triage.py:113) being good
enough at extracting (file, line). If it whiffs on a checker output format, locality
is unknown — fall through to the agent. Existing pattern catalog covers cargo/clippy/
ruff/pytest/generic, which is most of tanren's output.

## Empirical evidence (May 6 overnight)

R-0002 S-03 hit the progress check at attempt 6, which returned FLATLINED with the
rationale: "attempt 2 said organization event-kind coverage was deferred, and attempt
5 restates that `organization_created`/`organization_creati[on]` ..." — the agent
focused on the apparent root-cause repetition.

But the doer **did succeed** on attempt 7 (the next attempt). The "flatline" was a
false positive — the doer was iterating closer to the answer; the agent saw repeated
rhetorical phrasing and conflated that with no progress. Concrete evidence that an
agent-only progress signal misclassifies real progress when the failure narrative
sounds similar but the underlying diff is converging.

Land plan 06 (deterministic locality fingerprint) and the agent verdict becomes a
tiebreak only when locality signals are mixed.
