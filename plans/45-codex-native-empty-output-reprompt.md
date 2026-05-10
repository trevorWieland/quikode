# Plan 45 — Codex Native Empty Output Re-prompt

## Problem

During the 2026-05-10 Tanren run, several Codex-direct writes-file roles exited
`rc=0` after doing useful investigation or edits, but left
`--output-last-message` empty. The JSON transport treated that as a normal
doer output violation, so repeated empty native payloads burned subtask retry
budget and eventually hit same-signature stop-loss.

That is an agent-output protocol failure, not evidence that the target-repo
code is wrong.

## Fix

- When a `cli_native` transport returns `rc=0` with no structured payload,
  issue one immediate schema repair prompt inside the JSON protocol layer.
- Preserve the original prompt context and include the target schema in the
  repair prompt.
- If the second native payload is still missing or invalid, surface parse
  errors normally so the worker can block after the configured retry budgets.

## Validation

- `uv run pytest tests/test_subtask_execution_transient.py tests/test_json_protocol.py -q`
- `uv run ruff check quikode/agents/json_protocol.py tests/test_json_protocol.py`
