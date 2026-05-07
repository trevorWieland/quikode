# Plan 28 — Streamlined post-PR state machine

**Status:** queued (ready to ship in single PR).

## Why

The current post-PR flow (states `pending_ci` → `awaiting_review` → `merge_ready` plus `triaging_feedback` / `addressing_feedback` branches) **conflates two unrelated polling streams**: GitHub's CI status and inline review-thread comments. The thread polling pulls in every codex / bugbot / AI-reviewer line comment as if it were a human-driven "address this." That's churn-prone:

- Bots produce N "consider X" comments per push → N triage classifier calls → N fixup-planner rounds → CI re-runs → repeat. The pre-PR audit gauntlet (rubric / standards / behavior) is meant to be strong enough that last-minute reviewer fixes are the exception, but today every bot suggestion drives the system as if it were a human ask.
- Real GitHub `Review` objects (with `state` ∈ `{APPROVED, CHANGES_REQUESTED, COMMENTED}`) are **never queried at all** — a human's approval click cannot reach the FSM.
- The settle window exists to absorb (a) bot churn and (b) "wait a moment after push before merging." With only-real-reviews-trigger, (a) goes away and (b) collapses to "wait for an APPROVED review."

The streamlined model has **two polling phases** plus the ordinary fixup loop:

1. PR pushed → `PENDING_CI`. Poll only CI completion.
2. CI green → `AWAITING_REVIEW`. Poll only formal Reviews.
3. CI fail → bundle the CI logs as context and dive straight into `ADDRESSING_FEEDBACK` (skip the per-thread classifier).
4. Real `CHANGES_REQUESTED` review → bundle ALL nearby context (every comment, every thread, every prior review, bot suggestions) and dive into `ADDRESSING_FEEDBACK`. Resolved threads are excluded from the bundle (resolved = human dismissed it).
5. Real `APPROVED` review (and `auto_merge_when_clean=True`) → squash-merge.

`MERGE_READY` and `TRIAGING_FEEDBACK` retire entirely. Settle window retires. The per-thread classifier retires. ~250 LoC removed, ~200 LoC added.

## Decisions (resolved with the user)

1. **`COMMENTED` reviews** — context only, no transition.
2. **Inline line comments without an enclosing Review** — context only, no transition.
3. **Auto-merge on `APPROVED`** — preserve current `auto_merge_when_clean` flag, no extra timer (immediate squash on observed APPROVED + clean CI + mergeable).
4. **Settled-task notifications** — drop entirely. This is an intentional design cutover; the operator's natural rhythm is the AWAITING_REVIEW state itself.
5. **Bot threads** — never auto-resolve. **Resolved threads = human-dismissed → exclude from bundled context.** The bundle only carries unresolved thread bodies, all comments (including bot summaries), and the live Review objects. Human reviewers should still resolve incorrect AI-reviewer comments to keep the bundle clean.

## FSM after the change (post-PR slice)

```
PR_OPENING --pr_opened--> PENDING_CI

PENDING_CI:
  --ci_passed--> AWAITING_REVIEW
  --ci_failed--> ADDRESSING_FEEDBACK            (bundled CI excerpt)
  --parent_merged_or_conflict--> REBASING_TO_MAIN

AWAITING_REVIEW:
  --changes_requested_received--> ADDRESSING_FEEDBACK   (bundled review context)
  --approved_received--> MERGED                  (only when auto_merge_when_clean=True)
  --ci_failed--> ADDRESSING_FEEDBACK             (CI flaked red after pass)
  --parent_merged_or_conflict--> REBASING_TO_MAIN
  --pr_closed--> ABORTED
  --merged--> MERGED                             (human merges externally)

ADDRESSING_FEEDBACK:
  --feedback_pushed--> PENDING_CI
  --feedback_exhausted--> BLOCKED

REBASING_TO_MAIN / CONFLICT_RESOLVING: unchanged.
```

Three post-PR states (PENDING_CI, AWAITING_REVIEW, ADDRESSING_FEEDBACK) instead of six. Two polling timers (CI-only in PENDING_CI; CI + Reviews in AWAITING_REVIEW). One trigger event per direction.

### Removed events / states

| Construct | Action | Reason |
|---|---|---|
| State `MERGE_READY` | retire | Settle window dies; AWAITING_REVIEW + APPROVED is the auto-merge trigger |
| State `TRIAGING_FEEDBACK` | retire | Real Review.state IS the verdict; no per-thread classifier |
| Event `SETTLE_WINDOW_ELAPSED` | retire | Drops with MERGE_READY |
| Event `THREADS_FOUND` | retire | Replaced by CHANGES_REQUESTED_RECEIVED |
| Event `CI_FAILED_OR_THREADS_FOUND` | split | → CI_FAILED (PENDING_CI→ADDRESSING) + CHANGES_REQUESTED_RECEIVED (AWAITING→ADDRESSING) |
| Event `CI_GREEN_THREADS_CLEAN` | rename | → CI_PASSED (PENDING_CI→AWAITING_REVIEW) |
| Event `ACTIONABLE_FEEDBACK` / `NO_ACTIONABLE_FEEDBACK` | retire | Replaced by direct AWAITING→ADDRESSING via CHANGES_REQUESTED |

### New events

- `CI_PASSED` — PENDING_CI → AWAITING_REVIEW
- `CI_FAILED` — PENDING_CI/AWAITING_REVIEW → ADDRESSING_FEEDBACK
- `CHANGES_REQUESTED_RECEIVED` — AWAITING_REVIEW → ADDRESSING_FEEDBACK
- `APPROVED_RECEIVED` — AWAITING_REVIEW → MERGED (auto-merge gated)

`STACK_READY_STATES` becomes `{PENDING_CI, AWAITING_REVIEW}` (two instead of three).

## What gets deleted (the leverage)

- `quikode/triage.py`: `classify_review_thread`, `triage_review_threads`, `_invoke_classifier_host`, `_parse_classifier_envelope`, `ReviewVerdict`, `TriageOutcome` (~150 LoC). `parse_ci_failure` survives — still used by CI-fix path.
- `quikode/orchestration/review_watch.py`: `_classify_threads`, `_triage_review_threads`, `_resolve_auto_classified_threads`, `_reply_to_auto_resolved_thread`, `_block_if_review_rounds_exhausted` (in current shape) — replaced by `_fetch_latest_reviews` + `_handle_changes_requested` + `_handle_approval`.
- `quikode/orchestration/merge_watch.py`: `_classify_post_pr_target_state` 3-way truth table, `_maybe_notify_settled`, `_notify_settled_preconditions`, `_merge_ready_entry_ts`, `_last_clean_post_pr_entry_ts`. Replaced by 2-way classifier and trigger-driven auto-merge.
- `quikode/store_review.py`: per-thread tracking helpers (`mark_thread_addressed`, `addressed_in_commit_sha` column). `review_threads` row stays as a context cache only.
- `prompts/review-classifier.md`: deleted.
- `prompts/intent-reviewer.md`: kept (used by intent-review path, separate concern).
- Config flags retired: `respond_to_bot_reviews`, `notify_settled_channel`, `notify_settled_after_s`, `notify_ntfy_url`, `notify_ntfy_topic`, `notify_slack_webhook_url`, `auto_merge_min_age_s`, `stack_settle_quiet_s` (the last only if no other caller — verify in implementation).

## What gets added

- `quikode/github_graphql.py`:
  - `Review` pydantic model (id, state, author, body, submitted_at, is_bot)
  - `get_latest_reviews(repo, pr_number)` — GraphQL `pullRequest { reviews(last: 50) }`
  - `get_pr_comments(repo, pr_number)` — REST `gh pr view --json comments`
  - `bundle_pr_context(repo, pr_number)` → string with **unresolved threads + all PR-level comments + recent reviews + their bodies**, suitable as fixup-planner context.
- `quikode/state_schema.py` migration: add column `last_processed_review_id TEXT` on `tasks`. Drop `last_notified_settled_ts`. Schema version bump + one-shot UPDATE for in-flight rows (MERGE_READY → AWAITING_REVIEW; TRIAGING_FEEDBACK → PENDING_CI; the latter relies on the `recover_after_crash` path).
- `quikode/store_review.py`: `mark_review_processed(task_id, review_id)`, `last_processed_review_id(task_id)`.
- `quikode/orchestration/review_watch.py`:
  - `_handle_changes_requested(task_row, review, pool, futures)` — bundle context, transition AWAITING→ADDRESSING, dispatch worker
  - `_handle_approval(task_row, review, pr_status)` — gated on `cfg.auto_merge_when_clean`, fires squash-merge
- `quikode/workers/feedback.py`: `run_changes_requested_response(bundled_context: str)` (replaces `run_review_response(threads)`).
- `prompts/fixup-planner.md`: small edit — the existing `review_threads_block` channel is renamed/repurposed as a generic `bundled_review_context` blob. The planner consumes it identically.

## Driveby — doer prompt gate-ownership tightening

R-0010 / S-07-testkit blocked on 2026-05-07 with the same-signature stop-loss (plan 23) firing on 5 consecutive `(category=doer_output_invalid, signature=rc=0)` retries. Investigation showed the doer was **disclaiming `just tests` failures as "pre-existing from S-04/05/06"** — a direct violation of plan 12's "no CI failure leaks" invariant. S-07 wires the BDD harness through the earlier subtasks' interfaces; the harness exposes real bugs in those interfaces that the doer must fix in-place via the plan-13 cross-file scope-review carve-out.

Plan 12 hardened triage / checker / planner / progress on the no-leak rule. The **doer's own prompt** is what's leaking now.

**Edit (single file: `prompts/subtask-doer.md`):** add a "Gate ownership — no disclaim" block stating that any failing gate is this attempt's responsibility regardless of which slice introduced the underlying bug; cross-file fixes outside `files_to_touch` are explicitly authorized when removing them would cause a gate failure (plan 13); language like "pre-existing", "out-of-scope", or "from prior subtask" must not appear in the summary as a justification for leaving a gate red.

After deploy + daemon restart → `qk rewind R-0010 S-07-testkit` to retry under the strengthened prompt.

## Migration

In-flight rows during rollout map cleanly:

| Pre-rollout state | Post-rollout state |
|---|---|
| `pending_ci` | `pending_ci` |
| `awaiting_review` | `awaiting_review` |
| `merge_ready` | `awaiting_review` (re-evaluated next poll) |
| `triaging_feedback` | `pending_ci` (re-poll dispatches if CHANGES_REQUESTED still standing) |
| `addressing_feedback` | `addressing_feedback` |

Migration runs as a one-shot SQL block in `state_schema.py`. `recover_after_crash` already maps both `merge_ready` and `triaging_feedback` to `pending_ci` if the rollout interrupts mid-task — defensive overlap is fine.

## Files touched

1. `quikode/fsm.py` — state/event removal, transitions, recover_after_crash
2. `quikode/fsm_runtime.py` — drop enter_triaging_feedback / enter_merge_ready; new helpers for CI fail and CHANGES_REQUESTED
3. `quikode/state.py` (`POST_PR_STATES`, `STACK_READY_STATES`)
4. `quikode/state_schema.py` — schema version bump + migration
5. `quikode/orchestration/review_watch.py` — gut + rebuild
6. `quikode/orchestration/merge_watch.py` — simplify
7. `quikode/github_graphql.py` — `get_latest_reviews`, `bundle_pr_context`
8. `quikode/triage.py` — delete classifier code
9. `quikode/workers/feedback.py` — rename + simplify worker entry
10. `quikode/store_review.py` — drop thread bookkeeping; add `mark_review_processed`
11. `quikode/config.py` — drop retired flags
12. `quikode/cli_briefing_dev.py`, `quikode/tui/widgets/detail_panel.py`, `quikode/tui/widgets/tasks_table.py`, `quikode/tui/dag_view/render.py`, `quikode/tui/controllers/store_polls.py` — drop merge_ready / triaging_feedback rendering branches
13. `prompts/review-classifier.md` — delete
14. `prompts/fixup-planner.md` — minor edit (bundled-context channel)
15. `prompts/subtask-doer.md` — driveby (gate ownership)
16. `tests/` — extensive updates (test_fsm, test_review_watch, new test_github_graphql_reviews, etc.)

## Tests

- `tests/test_fsm.py` — new transitions, removed transitions, recover_after_crash for retired states
- `tests/test_review_watch.py` — formal review polling, CHANGES_REQUESTED dispatch, APPROVED merge, bot reviews ignored, last_processed_review_id de-dup, CI-flake from AWAITING_REVIEW
- `tests/test_github_graphql.py` — get_latest_reviews parser, bundle_pr_context renderer, bot filtering
- `tests/workers/test_feedback.py` — run_changes_requested_response happy path, blocked path, crashed path
- Update or delete: `tests/test_triage.py` (classifier sections), classifier-related test fixtures

## Rollout

Single PR. All-or-nothing — no flag. Migration script handles in-flight states. Compare to plan 17 / 24 in scope.

## Sequencing notes

- Ship before any tasks reach AWAITING_REVIEW for the first time on a new long-haul run (cleanest cutover window).
- Daemon restart required (Python code changes); prompt change for doer takes effect at next subtask attempt without restart, but combined here for one cycle.
- After deploy: rewind R-0010 / S-07-testkit (driveby benefit).
