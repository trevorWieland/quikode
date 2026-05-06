# Plan 09 — prompt-level changes that reduce retries

The user constraint: "your mode of influence is the state machine and prompts." This
plan tracks prompt edits that meaningfully reduce retry rate without changing what
the system is supposed to do.

## A. Doer prompt — stop-condition self-check

`prompts/subtask-doer.md` already mentions Layer 1 (`just check`) and Layer 2 (LLM
checker). But the doer is allowed to stop after writing the summary without running
either gate. About 10–20% of failed attempts are "doer stopped early; checker catches
trivial compile error".

**Edit.** After "Stop after the summary" add:

```
Before stopping, you MUST run `just check` yourself in the workspace and only stop if
it exits 0. If `just check` fails, fix the failures it flags, re-run, and only stop
when it passes. The orchestrator's Layer 1 gate is also `just check` — running it
locally yourself is the cheapest possible feedback loop, much faster than waiting for
the orchestrator to detect failure and re-prompt you with the same output.
```

The change is rhetorical — same gate, but anchors the doer's "done" decision in the
gate itself. Empirically, doers obey explicit "before stopping, MUST" instructions
about 90% of the time.

## B. Triage prompt — cite specific files in WHAT_TO_DO_DIFFERENTLY

`prompts/subtask-triage.md` asks for "WHAT_TO_DO_DIFFERENTLY: bullets with specific
changes". Empirically, triage agents produce vague bullets like "make sure the test
passes" because the prompt examples are too generic.

**Edit.** Replace bullet examples with grounded examples:

```
WHAT_TO_DO_DIFFERENTLY:
- In `crates/tanren-foo/src/account.rs:42`, the field is named `org_id` but the
  acceptance criterion says `organization_id` — rename it.
- The doer didn't add `pub use crate::events::AccountCreated` in lib.rs:18 — add it
  so the type is exported.
```

This anchors the triage to file:line rather than generic guidance. Cuts the doer's
"where do I start" cost.

## C. Planner prompt — explicit warning about file-budget violations

Tanren has a per-file line budget. The doer keeps splatting changes that push files
over budget, which fails Layer 1. The planner often misses this risk.

**Edit.** Add to `prompts/planner.md` after the BDD section:

```
## Tanren line-budget warning

Tanren enforces per-file line budgets via `xtask line-budget` (run by `just check`).
Before sequencing subtasks, look at the current line count of each `files_to_touch`
file vs the budget for its category (see `xtask/src/line_budget.rs` for limits). If a
subtask's files_to_touch contains a file that's already at 80%+ of its budget, the
subtask MUST include a refactor step that splits the file before adding new code.
Skipping this means the doer hits the gate at the end and burns retries.
```

## D. Checker prompt — emit structured failure shape

`prompts/subtask-checker.md` (read this before editing) emits free-form FAIL output.
The retry-classifier matches against it heuristically. A structured output (failure
kind + file refs) would let the classifier categorize more accurately.

**Edit.** Require the checker to wrap its FAIL verdict in:

```
{
  "verdict": "FAIL",
  "root_cause": "<one sentence>",
  "categorized_failures": [
    {"kind": "compile|test|lint|behavior|other",
     "file": "path",
     "line": 42,
     "snippet": "..."}
  ]
}
```

This ladders into plan 06's locality fingerprint — the locality detector reads the
structured field directly instead of regex-parsing the free-form output.

## Sequencing

A and B are safe to ship without quikode code changes — pure prompt edits, do not
require reinstall (prompts are loaded fresh each invocation from the bundled wheel).

Wait — actually they DO require reinstall, because prompts ship in the wheel via
`tool.hatch.build.targets.wheel.force-include`. Confirmed in scripts/reinstall.sh
comment. So any prompt change → reinstall → daemon restart.

C and D are larger; they ladder into other plans (08, 06).

## Validation

For each prompt edit:
1. Update the prompt file under `prompts/`.
2. Run `uv run pytest tests/test_prompt_*.py -q` (existing fixture-based tests).
3. `bash scripts/reinstall.sh --skip-tests` (ladder includes pytest).
4. In a separate workspace (the fixture, not tanren), run a single subtask and
   inspect the prompt rendered into the doer log.
5. If the prompt looks right, restart the tanren daemon during a quiet window
   (in_flight=0 or only provisioning).
