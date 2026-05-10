# Plan 44 — Codex Auth Refresh Race Retry

## Problem

During the 2026-05-10 Tanren run, multiple concurrent Codex-direct JSON agent
calls failed with:

- `token_revoked`
- `refresh_token_reused`
- `Encountered invalidated oauth token`
- `Your access token could not be refreshed because your refresh token was already used`

Those failures are transport/auth refresh races, not task implementation
failures. Before this fix they surfaced as normal `rc=99` agent failures,
which caused planner failures, checker/triage parse cascades, and blocked
subtasks such as R-0027 and R-0040's CI-fix slice.

## Fix

- Classify the known Codex OAuth refresh-race signatures as transient agent
  auth failures.
- Retry those failures inside the JSON transport with a short exponential
  backoff, controlled by:
  - `QUIKODE_AUTH_BACKOFF_INITIAL_S` (default `15`)
  - `QUIKODE_AUTH_BACKOFF_MAX_S` (default `120`)
  - `QUIKODE_AUTH_MAX_TOTAL_WAIT_S` (default `900`)
- If the auth race does not clear within the wait cap, surface `rc=124` with
  `transient=True` so worker-level retry paths treat it as transport noise.
- Add `planner_retries_on_transient` (default `3`) so initial planning retries
  transient agent failures instead of crashing directly to `failed`.
- Make subtask triage `rc != 0` produce a deterministic
  `failure_layer=transport` artifact instead of pretending the triage response
  was a schema/parse failure.

## Non-goals

- Do not classify every HTTP 401 as transient. A bare unauthorized response can
  mean the operator needs to log in again.
- Do not approximate subscription quota from token counts. Quota handling still
  uses provider error text only.

## Validation

- `uv run pytest tests/test_subtask_execution_transient.py tests/test_json_protocol.py -q`
- `uv run ruff check ...`
