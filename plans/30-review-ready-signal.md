# Plan 30 — Unified review-ready signal: ntfy + stacked-diff gate

**Status:** queued (single PR).

## Why

Plan 28 dropped the entire settled-task notification surface in favor of "AWAITING_REVIEW *is* the needs-human state." That works when the operator is watching `qk briefing`. It does not work for **away-from-machine workflows** where the operator only learns a PR is reviewable when their phone buzzes.

Reintroduce ntfy.sh delivery — but this time **unify the signal with stacked-diff readiness**, so one threshold serves both purposes.

## The unified signal

A task is **review-ready-settled** when:

> It has been in `AWAITING_REVIEW` continuously for at least `cfg.review_ready_settle_s` (default **900s = 15 min**).

The threshold absorbs the noise that would otherwise produce false alarms or premature dependent kickoff:

- **Late post-CI rebase events** that briefly drop the row into `REBASING_TO_MAIN` and back (now without churn).
- **Last-minute additional context** from human teammates / bot reviewers landing as PR comments — the bundled-context machinery (plan 28) collects them on the next CHANGES_REQUESTED, but if the operator wants to read them before kicking the next round, 15 min gives the conversation time to land.

When the threshold is crossed for the first time on a settled period, two things fire:

1. **ntfy.sh notification** to the operator: title + PR URL + brief summary. Click goes to the PR.
2. **Stacked-diff dependents** become eligible to pick up. From this moment, children whose only unmet deps are this task can start work on a guaranteed-green base.

The unification matters: dependents kick off **after** the operator-attention signal fires, so by construction every stacked child starts from a CI-green parent that the operator could have reviewed. The 15-minute buffer also gives the operator a window to review *first* if they're at the keyboard, before downstream work commits to the parent's diff.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `cfg.review_ready_settle_s` | `900` | Seconds in AWAITING_REVIEW before review-ready-settled fires. |
| `cfg.notify_ntfy_url` | `"https://ntfy.sh"` | ntfy server. |
| `cfg.notify_ntfy_topic` | `""` (disabled) | ntfy topic. Empty = no notification. |
| `cfg.stacking_strategy` | `"off"` | `"off"` / `"within-milestone"` / `"aggressive"`. Tanren ships set to `"aggressive"`. |
| `cfg.stacking_readiness` | `"speculative"` | `"speculative"` (any STACK_READY state) / `"settled"` (review-ready-settled gate). Tanren ships set to `"settled"`. |

The `notify_ntfy_*` keys were retained in user `.quikode/config.toml` files even after plan 28 retired them; reusing them keeps existing configs working unchanged.

## Stacked-diff: priority-tier the scheduler

Today's `score_candidate` adds **+50 boost for stacked children** to push them through fast. Plan 30 reverses the priority direction:

> **Primary tasks (no unmet deps) take precedence.** Stacked children are only picked when no primary candidate is pickable.

Implementation: in `collect_pick_candidates` consumers (`_pick_next`, `best_queued_priority`), partition the candidate list into `primary` and `stacked` buckets. If `primary` is non-empty, score-pick from that subset; only fall through to `stacked` when no primary is queued.

Reasoning (from the user): "stacked children should only see slots in scenarios where all available primary nodes are done or awaiting review." Primaries unblock more downstream work per slot than stacked children do, and stacked children that start *after* the parent is review-ready-settled have the strongest possible foundation — minor edits + rebase only.

`stacked_boost = +50` is removed (no longer relevant with hard tier filter). `unblock_boost`, `pr_boost`, `progress_boost` retained.

## State tracking

- `tasks.last_notified_settled_ts` (column already exists from pre-plan-28; left in schema by plan 28 because SQLite column-drop is expensive). Repurposed: stores the ts at which we last fired the review-ready ntfy for this task. Used for ntfy idempotency — re-fire only if a new entry into AWAITING_REVIEW occurred after this ts.
- The "entered AWAITING_REVIEW at ts X" signal is computed from `state_log` (most recent `to_state='awaiting_review'` row), not stored separately. Cheap indexed query.

## Files to modify

1. `quikode/notify.py` — **new** (ntfy-only, ~60 LoC). No slack codepath; that retired in plan 28 by user decision.
2. `quikode/config.py` — add `review_ready_settle_s`, restore `notify_ntfy_url` + `notify_ntfy_topic` (with new docs). No slack / channel-enum complexity.
3. `quikode/config_loader.py` — read the three keys.
4. `quikode/orchestration/review_watch.py` — `_maybe_notify_review_ready` called from the AWAITING_REVIEW poll branch when the settle threshold is crossed and ntfy hasn't fired for this period.
5. `quikode/orchestration/scheduler.py` — `is_parent_stack_ready` in `"settled"` mode now consults the most recent state_log entry into AWAITING_REVIEW + `review_ready_settle_s`. `collect_pick_candidates` consumers tier primaries vs stacked.
6. `quikode/store_review.py` — `last_review_ready_notified_ts` getter/setter; helper `most_recent_awaiting_review_entry_ts(task_id)`.
7. `tests/test_notify.py` — **new**, covers `notify_review_ready` happy-path, missing-topic-skip, http-failure-tolerance.
8. `tests/test_priority_pick.py` — extend with primary-first tier semantics.
9. `tests/test_review_watch_*` — add settle-threshold gating.
10. `/home/trevor/github/quikode-runs/tanren/.quikode/config.toml` — bump `[stacking] strategy = "aggressive"`, add `stacking_readiness = "settled"`, add `review_ready_settle_s = 900`.

## Tests

- `notify_review_ready` returns False when `notify_ntfy_topic` is empty.
- `notify_review_ready` posts to `https://ntfy.sh/<topic>` with title + body + click headers; returns True on 2xx.
- HTTP failure (timeout, non-2xx) returns False without raising.
- Scheduler: when both primary and stacked candidates are pickable, pick from primaries.
- Scheduler: when only stacked candidates are pickable, pick the highest-scoring stacked.
- `is_parent_stack_ready` in `"settled"` mode returns False when parent in AWAITING_REVIEW for < threshold; True at / past threshold.

## Rollout

Single PR. Daemon restart required (Python code changes). Tanren workspace config bump enables the aggressive stacking + settled readiness immediately.
