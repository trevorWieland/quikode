# Plan 47 — Retire `DoerEnvelope`

## Why

Plan 38 introduced `DoerEnvelope` as a "bookkeeping-only" JSON object the
doer emits alongside its file edits. The stated intent (orientation §7,
`subtask-doer.md` §1): doer writes files, also self-reports what it did;
the bookkeeping is for briefing/TUI surfacing but never graded.

In practice this contract has been the dominant blocker on tanren since
the May 8 sweep deploy:

- Proxy-routed write transports (z.ai, Wafer) frequently emit malformed
  JSON envelopes after a real diff lands, or emit a valid envelope with
  no diff, or emit neither.
- Three damage-control commits stacked up keeping envelope failures
  non-fatal: `06ff2b7 fix: keep doer envelope parse failures non-fatal`,
  `5c9d480 fix: add codex last-resort doer fallback`,
  `db2b168 fix: reprompt empty codex native output`. None removed the
  envelope as a cliff — they softened it.
- Current workspace state: 8 of 9 BLOCKED tasks share signature
  `category='doer_output_invalid' signature='rc=0'` from
  same-signature stop-loss after 5 consecutive envelope-shape failures
  (R-0002, R-0003, R-0008, R-0015, R-0019, R-0024, R-0025, R-0041). The
  9th (R-0040) is a related progress flatline.

If we accept that the envelope can fail and we have to fall back, then
**the fallback IS the system.** The envelope is dead weight: a parallel
self-narration channel that

1. adds a failure mode (parse/schema) with zero grading value — the diff
   and witness commands are the evidence,
2. spends doer tokens narrating what it did instead of doing more, and
3. forces every writes-files transport to produce structured output
   on top of `apply_patch`, reliably the worst part of the request
   shape on client-side transports.

The doer should DO. The orchestrator runs `git diff HEAD` and the
witness commands; the LLM checker grades the diff; triage explains
failures. None of that needs the envelope.

`ConflictResolverEnvelope` stays — `gave_up: bool` is load-bearing
(decides whether the worker BLOCKs). Only the doer envelope retires.

## What ships

### Schema / role layer

- Delete `DoerEnvelope` class from `quikode/agent_schemas.py` and remove
  it from `__all__`.
- Drop `DoerEnvelope` import from `quikode/agent_registry.py`.
- In `agent_registry.ROLES["subtask_doer"]`: keep `writes_files=True`,
  drop the output schema. Make `RoleSpec.output_schema` typed
  `type[BaseModel] | None`.
- `make_agent`: when a writes-files role has no output schema, construct
  a `WritesFilesAgent` without an envelope schema (see next).

### `WritesFilesAgent`

Currently always validates `envelope_schema`. Make schema optional:

- `envelope_schema: type[BaseModel] | None`.
- When `None`: invoke the transport in its **non-JSON / non-schema-enforced**
  path (no `--json-schema`, no `--output-schema`, no pydantic re-prompt
  loop). Return a `JsonAgentResult` with `structured=None`, `parse_errors=()`,
  `raw_text` carrying whatever stdout came back. Other fields (rc, duration,
  tokens, transient) populated normally so the worker can still record the
  agent call and tell `transient` apart from `success`.
- When set (conflict_resolver): unchanged behavior.

If the existing transports' `invoke` shape requires a schema, plumb a
"raw / no-schema" variant through. The `agents/` shims may need a new
method or a flag on the existing one. Each transport (codex_direct,
codex_litellm, claude) needs a path that just runs the CLI in
apply-patch mode without JSON output enforcement. Look at how the legacy
(pre-plan-38) doer ran for the shape — but DO NOT bring back the legacy
shims that PR-B.7 deleted; build the no-schema path on top of the
existing transport classes.

### `quikode/workers/subtask_execution.py`

This is the bulk of the cleanup:

- Remove `_DoerCallResult.envelope` and `_DoerCallResult.parse_errors`
  fields. Either reduce the dataclass to just `raw_text`, or inline the
  capture and drop the dataclass entirely — your call.
- Delete `_last_doer_envelope` and `_last_doer_parse_errors` ClassVars.
- Delete `_fallback_doer_envelope` method entirely.
- Delete `_fetch_prior_doer_envelope` method entirely. Plan-22
  carry-forward via envelope is gone — the next attempt already receives
  `triage_notes` (the structured triage output's
  `teaching_narrative` + cites) and that is the canonical carry-forward.
- Delete `_synthesize_parse_failure_outcome` method entirely (already
  unused — `_check_subtask` never calls it).
- `_run_doer_agent`: invoke the agent; persist `result.raw_text` (or a
  truncated tail of it) as the `subtask_doer:<subtask_id>` artifact for
  briefing/log purposes. No envelope, no parse_errors, no fallback. The
  artifact is now plain text (not JSON).
- `_cache_doer_state`: compute the diff (`_compute_subtask_diff_excerpt`)
  and run scoped witnesses. No envelope to cache.
- `_run_llm_subtask_checker`: drop the `doer_envelope` argument to the
  checker prompt entirely. Checker grades diff + witness results.
- `_triage_subtask`: drop the `doer_envelope` argument.
- `_run_scoped_witnesses`: drop the
  `fallback_commands=...envelope.witness_commands_run` argument to
  `run_scoped_witnesses`. The witness runner already reloads the
  worktree DAG before declaring `NO_COMMAND` per orientation §7 — that
  is the canonical source. If a witness has no command, that is real
  signal, not something to paper over with envelope claims. Update
  `quikode/workers/witness_runner.run_scoped_witnesses`'s signature if
  `fallback_commands` becomes orphan; if other call sites still need
  it (none expected outside this path) preserve it but don't pass any.

### `quikode/prompts.py`

- Remove `from .agent_schemas import DoerEnvelope` import.
- Remove `prior_doer_envelope` parameter from `subtask_doer_prompt`.
- Remove `doer_envelope` parameter from `subtask_checker_prompt`.
- Remove `doer_envelope` parameter from `subtask_triage_prompt`.

### Prompt files (Markdown)

`prompts/subtask-doer.md`:

- Strip §1's "Your final JSON envelope is a short bookkeeping record"
  and the surrounding framing about the envelope. Replace with a clear
  statement: "The diff is the evidence. After you finish editing,
  running the per-subtask gate, and running witnesses, stop. The
  orchestrator grades your diff."
- Strip §5a entirely (Prior attempt — your own doer envelope).
- Strip §8 entirely (Output schema (REQUIRED — bookkeeping only)). Also
  strip the trailing "The envelope is bookkeeping, not evidence"
  paragraph.
- Renumber remaining sections so the headings stay sequential.

`prompts/subtask-checker.md`:

- Remove the entire `{% if doer_envelope %}...{% else %}...{% endif %}`
  block ("Doer's self-report — INFORMATIONAL ONLY (do not grade
  against this)" section). Checker reads diff + witness output, period.
- Remove any other inline references to "the doer's envelope" /
  "self-report".

`prompts/subtask-triage.md`:

- Remove the `{% if doer_envelope %}...{% endif %}` "doer's self-report"
  section.
- The `parse_failure` enum value in the triage schema's `failure_layer`
  STAYS — it still legitimately covers checker/triage agent output that
  fails JSON validation (which is enforced for those roles). Just
  ensure the prompt's `parse_failure` description no longer references
  the doer envelope as a source.

### Other files referencing the envelope

- `quikode/config_descriptions.py` — drop the line "DoerEnvelope JSON
  before SIGTERM." (or rephrase to remove the envelope reference).
- `quikode/agents/json_protocol.py` — `WritesFilesAgent` change above.
- `quikode/agent_schemas.py` __all__.
- Anything else `grep -rn DoerEnvelope quikode/ tests/ prompts/` turns
  up — sweep them all.

### Tests

- Delete tests asserting envelope construction, fallback, parse
  failure, `prior_doer_envelope` carry-forward, schema-validation
  re-prompt on doer outputs.
- Update test fixtures and helpers that synthesize a `DoerEnvelope` for
  the worker — strip the envelope construction; the worker no longer
  consumes one.
- Update mock transports / fakes used in subtask-execution tests so
  the doer call returns plain text instead of validated structured
  output.
- Conflict-resolver tests stay — unrelated.
- ALL TESTS MUST CONTINUE TO PASS. The validation ladder is non-negotiable.

### Plans index

- Add row to `plans/00-INDEX.md` for plan 47 with one-line description.

## Operational followup (manager handles, not the agent)

After the agent ships:

1. Manager validates the ladder green:
   `uv run ruff check quikode tests`
   `uv run ruff format --check quikode tests`
   `uv run ty check quikode tests`
   `uv run pytest tests/ -q`
2. Manager commits + pushes to `optimizations`.
3. Manager runs `bash scripts/reinstall.sh --skip-tests` from
   `/home/trevor/github/quikode`.
4. Manager runs `qk daemon stop && qk daemon start --detach --max-parallel 12`
   from `/home/trevor/github/quikode-runs/tanren`.
5. Manager runs `qk reset-retries <id> && qk resume <id>` for each of
   the 9 BLOCKED tasks (R-0002, R-0003, R-0008, R-0015, R-0019, R-0024,
   R-0025, R-0040, R-0041) so the same-signature stop-loss clears and
   they retry under the new doer shape.
6. Manager watches `qk monitor` for first-wave behavior under the new
   contract. Watch in particular: `progress check flatlined` (means
   doer is producing empty diffs even without envelope), checker FAIL
   convergence, triage `failure_layer` distribution.

## Out of scope

- `ConflictResolverEnvelope` stays — load-bearing (`gave_up: bool`).
- `retry_classify._CHECKER_VERDICT_RE` mismatch with the rendered
  `VERDICT: FAIL` artifact text (causing false-positive
  `doer_output_invalid` classification when checker explicitly emits a
  FAIL verdict) — separate bug, observe whether it surfaces in the new
  shape and address in a follow-up plan if so.
- Quota fallback (plan 46) — orthogonal.
- Workspace config swap of doer model (proxy → direct codex) — this
  plan removes the failure mode entirely; the proxy-routed doer should
  now succeed at writing files because that's the only thing it has to
  do.
