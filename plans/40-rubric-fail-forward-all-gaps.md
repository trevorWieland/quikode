# Plan 40 — rubric fail forwards all cleanup gaps

## Problem

The rubric audit prompt asks the evaluator to enumerate every gap that
prevents a category from reaching 10/10. Runtime handling only forwarded
findings for categories below `pre_pr_rubric_min_score`, so when the rubric
failed overall, above-threshold categories could still carry ignored cleanup
work.

That made fixup cycles optimize for the minimum passing score instead of using
the failed cycle to raise the whole diff toward the stated rubric bar.

## Change

When the rubric stage fails overall, quikode now forwards:

- `rubric_below_threshold` findings for categories below the configured
  threshold.
- `rubric_reach_ten_gap` findings for above-threshold categories that still
  reported concrete `gaps_to_reach_ten`.

Fully passing rubric audits remain unchanged: they pass without emitting fixup
findings, even when a category scored below 10.

## Validation

- `uv run ruff check quikode/pre_pr_audit.py tests/test_pre_pr_audit.py`
- `uv run ruff format --check quikode/pre_pr_audit.py tests/test_pre_pr_audit.py`
- `uv run ty check quikode/pre_pr_audit.py tests/test_pre_pr_audit.py`
- `uv run pytest tests/test_pre_pr_audit.py -q`
