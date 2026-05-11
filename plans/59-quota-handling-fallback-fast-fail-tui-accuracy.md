# Plan 59 — quota handling refactor: drop in-transport sleep, fallback fast-fail propagation, agent_call backoff visibility, TUI pending parity

## Why

Tonight's tanren run surfaced a critical class of bugs around quota
handling + TUI accuracy that the plan 58 implementation agent
incorrectly deferred. These are NOT orthogonal — they're the
operational consequence of plan 58's "TUI must reflect reality"
principle and the chain-walk semantics plan 58 introduced. They MUST
ship before the plan 58 deploy.

The four issues are tightly coupled to the same operator concern
("don't lie to me; reflect actual provider/scheduler state") and
should land together as one cohesive plan.

## What ships (all four fixes — none can be deferred)

### Fix A: fallback chain fast-fail propagation

**File:** `quikode/agent_registry.py`

**Current bug** (lines 219-220):
```python
primary = _build_base_transport(spec, quota_max_total_wait_s=0)
fallbacks = tuple(_build_base_transport(MODELS[name]) for name in spec.quota_fallbacks)
```

The primary fast-fails on quota (good); the fallbacks use the
DEFAULT cumulative wait (~8 hours). When the secondary fallback is
ALSO quota-exhausted, `_run_with_retry` sleeps + retries within
THAT fallback for hours, never reaching tertiary/quaternary. This
is exactly what stalled the 10 in-flight workers tonight on
`GLM-zai → GLM-wafer` after wafer was also exhausted.

**Fix:** propagate `quota_max_total_wait_s=0` to all-but-the-last
fallback. Only the chain floor uses the default (or — per fix E' —
NONE of them sleep in-transport at all and the chain returns fast).

After fix E' lands the cumulative-wait knob becomes irrelevant
everywhere. So practically: pass `quota_max_total_wait_s=0` to
EVERY transport. The cumulative wait is dead.

### Fix B: agent_call backoff visibility

**Current bug:** `_run_with_retry` in `agents/json_protocol.py` sleeps
for backoff duration WITHIN the agent_call record's lifetime. No
finish_update fires during the sleep, so `Store.agent_in_flight_status`
sees the start_marker indefinitely and reports "running" — when the
worker is actually idle in `time.sleep()`. TUI says "subtask_doer in
flight 45m12s" when the worker has been doing nothing for 45 minutes.

**Fix shape:** with fix E' removing in-transport quota sleep entirely,
this issue's QUOTA dimension goes away — the chain returns fast,
agent_call gets a normal finish_update with rc=quota signal, TUI is
honest. For the REMAINING legitimate sleeps (container-vanished
retry, auth-refresh retry — both in-transport and brief), add an
`agent_call_status` column on `agent_calls` table:
- `running` (default — subprocess actually executing)
- `backoff_auth` (during 60s auth-refresh sleep)
- `backoff_container` (rare; container vanished retry)

`Store.agent_in_flight_status` returns the status alongside phase +
age. TUI / detail panel render "subtask_doer backoff_auth 45s" so
the operator sees what's actually happening.

NOTE: with fix E' the QUOTA case never enters the in-transport sleep
path, so no `backoff_quota` status is needed there. Quota delays
live entirely at the worker layer.

### Fix C: TUI pending_eligible parity with scheduler

**Current bug:** `quikode/tui/controllers/pending_eligibility.py`
mirrors `is_parent_stack_ready` semantics but skips stack-depth +
stack-breadth + would-form-cycle bounds + plan-30's
`prefer_primary_candidates` tiering. The displayed "pending N" count
is an upper bound, not a reflection of what the scheduler would
actually pick up. Tonight the operator saw `pending 5` when the
scheduler probably had fewer (or zero) genuinely-eligible candidates.

**Fix:** call `collect_pick_candidates` from
`quikode/orchestration/scheduler.py` directly from the TUI controller.
Single source of truth — the same function the orchestrator's
`_pick_next` uses. The TUI count is by definition exact.

The helpers (`stack_depth_fn`, `stack_root_fn`,
`stack_size_under_root_fn`, `would_form_cycle_fn`) are currently
Orchestrator methods. Refactor them out into a standalone module
(`quikode/orchestration/stacking_helpers.py` or similar) that takes
`store` + `dag` as args — no Orchestrator instance needed. Both the
orchestrator and the TUI controller import from there.

Apply `prefer_primary_candidates` to the resulting candidate list
(plan 30's tiering) — the TUI's count is the number that
`prefer_primary_candidates(collect_pick_candidates(...))` returns.

### Fix E' (replaces E): drop in-transport quota sleep entirely

**Current bug:** `_run_with_retry` in `agents/json_protocol.py`
implements exponential-backoff sleep-and-retry on quota detection
(legacy plan 19A design from before the fallback chain existed). The
in-transport sleep blocks the worker for up to 8 hours (default cap)
while the SAME provider is being re-tried. With a 4-link fallback
chain in place, this redundant sleep is the root cause of tonight's
stuck-doer behavior.

**Fix:** remove the quota retry loop from `_run_with_retry`. Quota
detection at the transport level returns IMMEDIATELY with a
transient result carrying `category="quota_exhausted"`. The chain
walk in `QuotaFallbackJsonAgent` cascades through providers in
seconds. If ALL providers return quota, the transport returns
transient — and the WORKER LAYER handles the re-attempt cadence.

Keep the container-vanished and auth-refresh retry loops in
`_run_with_retry` — those are legitimately in-transport. ONLY the
quota retry path is removed.

**Worker-layer quota-class sleep:** in `workers/subtasks.py`
`_record_transient_subtask_failure`, today's `time.sleep(15)` is
generic. Make it sensitive to the transient's category via a new
config field `cfg.transient_retry_delays_s: dict[str, int]` (default:
`{"quota_exhausted": 600, "container_vanished": 15, "auth_refresh":
60}`). The worker looks up the category from the outcome and sleeps
the appropriate duration.

This gives the operator a single knob to tune quota-retry cadence
without touching transport internals. 10 min default for quota is
the operator's preferred re-attempt cadence ("just wait 10 minutes
between attempts and try the whole chain again").

Also: the `QUIKODE_QUOTA_MAX_TOTAL_WAIT_S` environment variable +
the `_quota_max_total_wait_s` helper can be removed entirely. No
longer used.

### Tests

Each fix gets concrete tests:

- (A): construct a `QuotaFallbackJsonAgent` with 3 mock transports
  where the first two return quota; assert the third is actually
  invoked. Today this would hang on the second's retry loop.
- (B): mock a worker hitting auth-refresh; assert the agent_call
  row's `status` column transitions to `backoff_auth` during sleep
  and back to `running` on retry.
- (C): seed a store + DAG where the upper-bound count differs from
  the real `collect_pick_candidates` count (e.g., stack-breadth cap
  hit); assert the TUI controller's pending_eligible matches the
  real count, not the upper bound.
- (E'): mock a worker hitting quota; assert the transport call
  returns within seconds (no minute-long sleeps), the outcome
  carries `category="quota_exhausted"`, the worker's transient
  handler sleeps 600s per the default config.

### Plans index + orientation

- Add plan 59 row to `plans/00-INDEX.md`.
- `orientation.md` §7 invariants: quota handling is now
  "chain-walk-fast + worker-layer category-aware sleep," NOT
  "in-transport sleep+retry." Plan 19A's design is RETIRED.

## Operational followup (manager handles)

After agent ships:
1. Validation ladder green.
2. Commit + push.
3. Deploy with plan 58: daemon stop (already stopped) → run
   `plans/58-migration.sql` → daemon start.
4. The combined plans 57 + 58 + 59 deliver: typed FSM guards + state
   flatten + worker driver unify + phase/cycle layer + fast-fail
   chain + honest agent_call status + accurate pending count + clean
   quota handling.

## Out of scope

- Anything not in fixes (A) (B) (C) (E'). Don't expand scope.
- Don't refactor the transport classes' invoke shapes — fix (A) is a
  one-line change in agent_registry.py; fix (E') is a delete in
  json_protocol.py.
- Don't reshape ModelSpec's quota fields — those go away entirely
  under E'.

# CRITICAL: scope discipline

**The implementation agent for this plan must NOT defer ANY fix in
this scope.** Plan 58's implementation agent deferred (A)(B)(C)(E')
calling them "orthogonal" — they're not, and that decision caused
operational pain. Your scope is exactly these four fixes. All four
land in one commit. If any seems orthogonal in isolation, it isn't
— they're the operational consequence of plan 58's "reflect reality"
principle.

If something is genuinely ambiguous, ask via SendMessage. If you
discover unexpected complexity, surface it — but don't unilaterally
drop pieces of scope. The deploy depends on all four shipping.
