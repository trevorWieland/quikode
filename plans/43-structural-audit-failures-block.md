# Plan 43: Structural Audit Failures Block

## Trigger

After adding output retries, a pre-PR behavior audit returned no structured
output during daemon shutdown and was forwarded to the fixup planner as
`behavior:parse_failure`.

That is the wrong failure mode. A target-repo fixup subtask cannot repair an
auditor parse failure, transport failure, render failure, or config/bootstrap
failure. Letting the state machine continue creates toxic plans and obscures
the quikode/runtime bug that needs operator attention.

## Change

Pre-PR audit failures now split into two classes:

- Content findings: rubric, standards, architecture, or behavior findings that
  describe real target-repo work. These still go through the release valve or
  fixup planner.
- Structural findings: `parse_failure`, `transport`, `infra`, `config_error`,
  `render_failure`, or `bootstrap_error`. These BLOCK the task immediately
  after the retry budget is exhausted and persist the audit report as a
  forensic artifact.

Daemon shutdown also sets a process-local shutdown flag. If an audit stage
returns after shutdown has been requested, the worker discards that partial
stage result instead of recording it as a failed audit.

## Tests

- Structural findings return a block report and content findings do not.
- Release valve still refuses structural findings.
- Pre-PR audit stage results returned after shutdown are not persisted.
