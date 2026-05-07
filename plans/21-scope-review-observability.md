# Plan 21 — scope-review observability

## Symptom

R-0020/F-1-7's triage notes named "scope reviewer" as the failure layer
("scope review rejected commit as overreach", with a list of rejected
files), but `agent_calls` and the artifacts table contained zero
`subtask_scope_review` rows for that subtask — or for any subtask
across the entire workspace. Operator running `qk show` could not see
what the reviewer decided, only inferred it from the `commit_subtask`
output that triage happened to relay.

`scope_review.py:121` calls `agent.run()` directly without recording
the call into the store. Every other agent role (planner, doer,
checker, triage, fixup_planner, progress) records both an `agent_calls`
row and an artifact body. Scope review was the lone exception, even
though it's the gatekeeper for whether out-of-lane edits land.

## Why it matters now

After the cascading-failure recovery (plan 20), four soft-cap signals
fired in the first hour. Diagnosing whether the deadlock was at
doer / scope-review / checker required reading the store. With scope
review invisible, diagnosis required reading raw daemon logs and prose
inside other agents' triage notes. Scope-review rejections are also
how cross-file gate-fixes (plan 13's invariant) succeed or fail —
"why isn't this commit landing" is unanswerable without the verdict.

## Fix

1. `quikode/scope_review.py`: extend `ScopeReviewResult` with two
   optional fields — `agent_run: AgentResult | None` and
   `role_used: AgentRole | None`. Populate them on every code path
   that actually invoked the agent (success, agent rc!=0,
   unparseable output). Leave None on the cheap-path subset
   early-return and on prompt-render failure where no agent ran.
2. `quikode/workers/subtask_completion.py`: in the `_lane_review`
   closure, after calling `review_scope_drift`, hand off to
   `_record_scope_review(...)` which:
   - emits `record_agent_call(phase="subtask_scope_review", ...)`
     with the cli/model/rc/duration/tokens/cost from the captured
     run, mirroring the doer/checker/triage record sites
   - persists a `subtask_scope_review:<subtask_id>` artifact whose
     body contains the verdict, reason, declared lane, actually
     touched, out-of-lane diff, accepted lane post-review, and the
     full agent stdout

No behavior change. Pure additive observability.

## Why not log inside scope_review.py directly

`scope_review.py` doesn't import `Store`, and threading store + node
id in would couple it to the worker context. Returning the captured
`AgentResult` keeps scope_review pure (it adjudicates; the worker
records). The pattern matches how `commit_subtask` returns
`CommitResult` and lets the worker decide what to log.

## Validation

- `uv run pytest tests/ -q` — 854 passed.
- After ship: every subsequent doer commit with out-of-lane drift
  produces a `subtask_scope_review:<id>` artifact visible in
  `qk show <task>` and an `agent_calls` row visible in
  `qk briefing`'s cost histogram.
