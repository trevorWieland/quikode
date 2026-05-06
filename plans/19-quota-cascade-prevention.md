# Plan 19 — graceful 429 handling, NO subscription approximation

**Status: queued, draft.** Not shipped. Captures the design constraints from the
2026-05-06 quota-handling discussion so the next time we touch this it doesn't
get redesigned from scratch.

## The hard constraint that scopes the entire plan

> **Tokens (ccusage cost) cannot be mapped to subscription bucket usage.**

The user's reasoning, recorded verbatim because it should bind any future work
on this:

- An identical token spend can consume 5× more or 5× less of the subscription
  bucket depending on time-of-day (peak vs off-peak), day-of-week, current
  server load on the provider's side, and undisclosed runtime knobs.
- Providers may silently widen the bucket as a promotion or tighten it under
  load with no advance notice.
- Therefore any orchestrator-side code that estimates "X tokens spent ⇒ Y%
  of weekly quota consumed" will be **wrong by an unbounded factor** and is
  worse than no number at all (because it lulls callers into trusting it).

**Implementation rule:** quota numbers must come from the provider's own
signal stream — not from local accounting. If we cannot read a real signal,
we report `unknown`, not an estimate.

## Reference for "real signal" monitoring

[codexbar](https://github.com/) (and the broader quotio / opencode-quota family
documented in the prior discussion) — a desktop app that monitors quota by
parsing the CLI's own emitted signals (status outputs, OAuth-endpoint
responses, error message classes). That's the model. We don't build a
quota-cost mapping; we passively observe what the CLI tells us, and react.

## Priority 1: prevent the 429 → checker → triage cascade

The user's stated top concern, in their words:

> If we are hitting 429 / usage halts / etc, we don't want an unhandled and
> drag-down retry cycle, as that could lead to an implementation agent
> 429'ing causing the checker agent to repeatedly check dozens of times in a
> row then burning tokens and 429'ing.

What the FSM does today (`workers/subtasks.py:_subtask_loop`):

```
do_subtask          # may 429 — agent call returns rc != 0
  ↓ unconditional
check_subtask       # runs even though the doer wrote nothing — burns CLI #2
  ↓ verdict=FAIL (no diff to verify)
triage_subtask      # runs to "explain" a non-failure — burns CLI #3
  ↓ next attempt
do_subtask          # 429s again
  ...
```

The cascade is: **one quota'd CLI causes 2× to 3× extra agent calls per
"failed" attempt**, each of which can in turn quota the next CLI. With three
roles using three different CLIs, the budget shreds 6×–9× faster than a
clean-failure mode would.

### The fix (when we ship it)

Detect the 429 / usage-halt class at the point of the agent call returning,
**before the FSM transitions to `checking_subtask`**. New retry classification:

| Outcome | Existing behavior | New behavior |
|---|---|---|
| `agent_cli_rate_limit` (the existing class in `retry_classify.py`) | Counted as a real failure; consumes attempt budget | Treated like a transient: hold the task in `doing_subtask`, do **not** fire the checker, do **not** fire triage, do **not** decrement the attempt budget. Re-attempt on the next scheduler tick after a backoff. |
| `agent_cli_quota_exhausted` (new — distinguishable from rate-limit by stderr pattern) | n/a | Same as rate-limit but with a longer hold (until the provider's stated reset, parsed from stderr if present, else conservative default). |

Per-CLI signal patterns the detector should watch (recorded so we don't have
to research them again):

- **claude**: `You've hit your session limit · resets H:MMam/pm`,
  `You've hit your weekly limit · resets <Day> H:MMam/pm`, `You've hit your
  Opus limit ...`, `Server is temporarily limiting requests` (existing
  transient).
- **codex**: `rate_limit_exceeded` in stderr; JSONL `turn.failed`/`error`
  with `code: "rate_limit_exceeded"`.
- **opencode**: provider-dependent. The doer (zai-coding-plan) typically
  forwards a 429 message; opencode's "prettify" can mangle the upstream
  message — match conservatively.

The extra resilience: if the **checker's** CLI is itself the one quota'd,
the doer attempt is held without ever running the checker. The cascade
cannot start.

## Priority 2 (later): real-signal monitoring

Once the cascade is closed, the open question is "do we even need quota
visibility?" Possibly not. Tokens are tokens; faster vs slower burn at the
same overall token volume doesn't materially change outcome — it just
changes wall-clock per node. The reactive fix in Priority 1 already
prevents the pathological cascade.

If we do want a `qk usage`-equivalent, the implementation must:
- Read what the provider tells us, in the form they tell us.
- For claude: the OAuth `usage` endpoint returns `5h` and `7d` buckets with
  `used` / `limit` / `resets_at` — record those as-reported.
- For codex / opencode-with-opaque-providers: report `unknown`, with the
  last-seen quota event (most-recent 429 / reset-at if known) attached as
  the only ground truth.
- **Never** synthesize a percentage from local consumption.

## Order of operations when we ship

1. Plan 19A: cascade prevention only (this plan's Priority 1). New
   classification, new FSM behavior on classification, no UI changes, no
   probes. Smallest possible PR that closes the bleed.
2. Plan 19B (optional): `qk usage` command + briefing line, only with
   real-signal sources, gracefully reporting `unknown` for opaque CLIs.
3. Plan 19C (optional): rotation chain config (`AgentRole` becomes a
   priority list). Only worthwhile after 19A makes "quota'd" a real
   distinguishable state.

## Why this is queued, not shipped

The R-0005 / R-0020 oscillation that consumed earlier today's attention is
fixed (plans 17–18). The current overnight run isn't currently bleeding on
quota cascades that we can see in `qk briefing` or the agent-cost panel.
Waiting until the cascade actually hits (or until it's the next priority
the operator points at) keeps the optimizations branch focused.

## Status

**Queued.** Pick this up next time a 429 cascade is observed, or when the
operator decides quota handling is the next priority.
