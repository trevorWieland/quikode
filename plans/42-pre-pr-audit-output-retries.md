# Plan 42: Pre-PR Audit Output Retries

## Trigger

R-0023 and R-0019 reached the pre-PR gauntlet with local CI passing, then hit
rubric/standards schema-validation failures:

- `rubric audit response failed schema validation — failing closed (parse_failure)`
- `standards audit response failed schema validation — failing closed (parse_failure)`

These are output-format violations from the audit agent, not evidence that the
task diff is bad. Treating them as ordinary audit findings can send the system
into a fixup/block cycle even though retrying the same audit is the right move.

## Change

`pre_pr_audit._invoke_audit` now has a driver-level output retry budget:

- Config knob: `pre_pr_audit_output_retries`
- Default: `5`
- Scope: retry only malformed/missing structured audit output after the
  JsonAgent layer has already attempted its own repair.
- Non-retried failures: agent nonzero rc, transport/infra failures, and
  registry/schema mismatches.

If every retry still fails validation, the stage emits the existing synthetic
`parse_failure` outcome. That keeps the no-fabrication invariant intact while
making BLOCKED the escape hatch instead of the first response.

## Tests

Added coverage that:

- Rubric audit retries a parse failure and succeeds on the next structured
  response.
- Rubric, standards, behavior, and architecture parse failures exhaust the
  configured retry budget before returning synthetic `parse_failure`.
- `load_config` reads the new retry knob.

## Deployment Notes

Live workspaces should set:

```toml
pre_pr_audit_output_retries = 5
```

On daemon restart, failed pre-PR stages are rerun while already-passed stages
are reused, so this patch allows R-0023/R-0019 to rerun the malformed audit
stage instead of treating the parse failure as a real code finding.
