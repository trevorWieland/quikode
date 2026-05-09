# Plan 39 — fixup output repair budget

## Incident

On 2026-05-09, multiple tanren tasks reached `blocked` after the
pre-PR fixup planner returned useful JSON plans that failed quikode-side
validators. The plans were not empty and were not obviously unusable:
most failures were validator over-strictness around extra stage-typed
claims or generic audit section labels such as `§Rules`.

That violated the operating rule for the runner: bad structured output is
the responsibility of the same agent call to repair, not a reason to block
the whole task immediately.

## Fix

- `validate_finding_coverage` no longer rejects extra rubric or behavior
  claims that are not exact audit finding ids. It still requires every
  expected finding to be covered and still rejects duplicate ownership.
- Fixup-plan standards and architecture citation validation remains strict
  on document bucket resolution, but is lenient on section aliases. Spec
  planner validation remains section-strict.
- The fixup planner now has a configurable output repair budget,
  `fixup_planner_output_retries` (default `5`), used for both schema
  violations and runtime validator violations before `blocked` is used as
  the final escape hatch.

## Validation

- `uv run ruff check quikode/planner_validators.py quikode/workers/fixup_coverage.py quikode/config.py quikode/config_loader.py tests/test_planner_validators.py tests/test_fixup_coverage.py`
- `uv run ruff format --check quikode/planner_validators.py quikode/workers/fixup_coverage.py quikode/config.py quikode/config_loader.py tests/test_planner_validators.py tests/test_fixup_coverage.py`
- `uv run pytest tests/test_planner_validators.py tests/test_fixup_coverage.py tests/test_config_schema.py -q`
