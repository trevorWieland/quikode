# future work — open candidates

Open-items tracker. Most v3 items shipped during the 2026-05-04 driven
run; this file now records what's still on the table.

Status legend: 🔴 high · 🟡 medium · 🟢 nice-to-have

---

## Carried forward (still open)

### 🟢 V3-006 xtask extension prompt awareness

**Status: OPEN.** F-0002 added `xtask/src/bdd_tags/`. Future tanren nodes
will likely add more xtask subcommands. The doer should know where they
plug in. ~5 LOC in `prompts/doer.md`.

### 🟢 V3-008 DAG drift reconciliation

**Status: OPEN.** If the DAG removes a node, quikode's store keeps the
orphan row. `quikode reconcile-dag` would prune those rows with a
confirm prompt. Low priority — DAG nodes rarely get removed.

### 🟢 V3-009 TUI DAG-version banner / `/reload` slash command

**Status: OPEN.** When the DAG file changes mtime, the TUI should
surface a banner: "DAG file changed — reload?" and a `/reload` slash
command that reseeds the store without losing in-flight task state.

### 🟢 Cost rolling-average in regular TUI dashboard

`quikode briefing` shows ccusage costs. The DAG viewer's stats panel has
rolling avg per task. The regular TUI dashboard does not. Small UX win.

### 🟢 Multi-workspace daemon

`quikode daemon` is single-workspace today. Could supervise N workspaces
from one daemon process. Future; not a friction point yet.

### 🟢 Stack depth >6

Cap is currently 6 (`cfg.stacking_max_depth`). Lifted from 4 for tanren's
chains. Could go higher if a future DAG needs it; would need
breadth-aware safety to avoid pathological stacks.

---

## Surfaced during 2026-05-04 driven run

### 🟡 Per-task SQLite connection (untaps 8-10 parallelism)

The `Store` class uses a single shared connection serialized by `_tx_lock`.
At max-parallel=5 contention is invisible (~50ms p99); at 10+, the lock
becomes the bottleneck. Switching to a per-task connection (or a small
connection pool) would lift the SQLite ceiling. Required before going
past max-parallel ~7.

### 🟡 Per-thread review-response slice attribution

`fixup-review` rounds emit one mini-subtask per identified issue, but the
mapping from (review_thread_id → subtask_id) is implicit. If we recorded
the mapping, `quikode show` could surface "thread #X addressed in
F-3-1-foo (commit abc1234)" instead of just the count. Improves operator
trust during heavy review-response cycles.

### 🟡 Force-with-lease push retry on race loss

When the divergence-rebase pushes with `--force-with-lease` and the
remote was advanced again between fetch + push, the push silently fails
(non-blocking, see `_rebase_diverged_branch`). A bounded retry (refetch +
re-rebase + push, max 2 cycles) would smooth out concurrent-push races
that today require a second subtask boundary to recover.

### 🟢 Auto-merge with longer min-age

Today `cfg.auto_merge_when_clean` exists but defaults off. Pairing with
`cfg.notify_settled_after_s` (default 30min) and a longer min-age (say
2h) for tanren-style workspaces would let the daemon land settled work
without operator clicks while still giving humans first-mover review
opportunity. Low-risk extension once notification path is trusted.

### 🟢 Briefing summary of recent BLOCKED

`quikode briefing` shows the in-flight + awaiting set but not "BLOCKED in
the last 24h with reason". Operators returning to the workspace should
see the BLOCKED set up front so review/retry decisions surface.

### 🟢 Rate-limit detection for codex/anthropic

When codex hits its per-account rate limit, the agent CLI returns
non-zero with a specific stderr signature. Today the worker treats this
as a transient error (free retry, eventually capped). A dedicated
detector + cooldown would smooth the experience.

### 🟡 Retroactive ccusage outlier clamp on existing rows

The 2026-05-04 cap commit (`567af2b`) prevents *new* parser outliers
from inflating totals, but pre-fix rows persist in `agent_calls` and
keep the briefing total roughly $585 too high (two $292.89
misattributions on R-0008 and R-0019). A small migration —
`UPDATE agent_calls SET cost_usd = 0 WHERE cost_usd > 50 AND ts <
<commit_ts>` with a logged note — would make historical totals match
post-fix reporting. Low priority but recurring eyesore in `quikode
briefing`.

### 🟡 Subtask transient-classification storm under fast-fail

R-0019 S-04-api on the prior daemon run logged a checking ↔ doing
flapping pattern: `doing → checking → doing(attempt 4) → checking
(0s) → doing(0s) → ...` for ~80s before orphan recovery interrupted.
Suggests the doer was exiting fast (rc != 0) before producing a real
diff and the checker treated the verdict as transient, never
incrementing `subtasks.retries`. The transient-cap (default 10) and
fixup decomposition mitigate, but a tighter classifier (require
*some* worktree edit between consecutive transient verdicts) would
short-circuit the storm earlier and conserve agent budget.

### 🟢 Daemon log rotation

`daemon.log` is append-only (TODO comment in `daemon.py`). After a
multi-week run it'll grow without bound. Size-based rotation
(e.g., rotate at 50MB, keep last 5) is the simple fix.

### 🟢 Persistent quikode-managed Monitor

When operating quikode autonomously, the operator (or driving Claude
session) sets up a tail-and-grep on `daemon.log` for state
transitions. A `quikode tail-events` (or `tui --events`) command
that emits one line per BLOCKED / AWAITING_MERGE / fixup-cap /
review-cap event would let any operator wire it into their preferred
notifier (ntfy CLI, Slack webhook, ssh+jq) without re-implementing
the grep filter.

---

## Stacked-diff vision: arbitrary DAGs without merging to main

The long-term aim is a stacked-diff model robust enough that an entire
DAG can advance through review-and-rebase entirely on top of the
target branch, without anything needing to merge to main first. The
operator returns to a queue of small, semantically-bounded chained
PRs; ancestor-PR fixes percolate down the chain automatically.
Phase 1 (readiness gate) shipped 2026-05-04. Phase 2 (multi-parent
merge-base) and Phase 3 (instructional resolver) are open.

### 🟡 Phase 2 follow-up — Cascade rebase on parent advance

Phase 2 minimum-viable shipped 2026-05-04 evening: schema, store
helpers, picker side-effects, `stacking.construct_merge_base`,
worker provisioning hook, 14 tests. What remains:

- **Cascade rebase scheduler.** When a parent in MERGE_READY accepts
  a new commit (e.g. fixup pushed in the addressing-feedback path),
  every descendant relying on its merge-base needs to recompute.
  Today the scheduler reacts to *parent merge*; extend
  `_schedule_rebases_for_merged_parent` (or a sibling
  `_schedule_rebases_for_parent_advance`) to fire on push, not just
  merge, and traverse the multi-parent DAG in topo order so D
  rebases only after B/C have themselves finished.
- **`run_rebase_to_main` multi-parent variant.** Today the worker
  uses `git rebase --onto <parent_sha>` against a single parent;
  for multi-parent, rebase against the *prior* merge-base sha so
  the child's commits replay onto the new merge-base. The store
  already records `parent_merge_base_sha` for this reason.
- **Stack-walk helpers as DAG walks.** `_stack_depth`,
  `_stack_root`, `_stack_size_under_root` currently follow the
  scalar `parent_task_id` chain. Generalize to walk
  `parent_task_ids` (union of all paths upward). Multi-parent
  cycle detection already covered by the scalar version's `seen`
  set — extend to the array case.
- **End-to-end integration test** with real git: tmp repo with
  three branches, run `construct_merge_base`, assert a clean
  merge tree comes out, then introduce a conflict and assert
  the helper aborts cleanly without leaving the repo dirty.

The MVP that shipped today is correct on the schema + provisioning
fork-point; the cascade-rebase logic is the load-bearing follow-up
that lets a multi-parent chain advance through review without
operator intervention.

### 🔴 Phase 3 — Instructional conflict resolver

**NOT** a "skip-checker on conflict-free rebase" fast path — that
risks shipping a semantically broken foundation (the canonical
"add-a-foo / add-bar-to-every-foo" failure mode where each commit
applies cleanly but the chain is incoherent).

Instead: when the conflict resolver produces a successful resolution,
it persists a structured **merge instruction** alongside the resolved
diff: which siblings are likely to need the same fix, the *reason*
the conflict appeared (e.g. "field rename, propagate to all callers
that touched the renamed call"), and the recipe (sed-pattern,
import-rewrite, manual-checked snippet, etc).

Subsequent resolver invocations on sibling rebases consume the prior
instructions before re-investigating from scratch. This is
fundamentally about **propagating intent**, not skipping checks:
- The instruction is human-readable and surfaces in `quikode show`,
  so reviewers see *what is being applied automatically* across the
  chain, not just that the chain is conflict-free.
- The follow-up agent applies the instructions and re-runs the
  acceptance gate; semantic validation always fires.
- Repo standards (lints, type checks, BDD tags, etc.) are part of the
  acceptance gate, so any "intent drift" is still caught locally.

Operator vision: "I go on vacation for a week, come back to a queue
of small chained PRs to review. I leave a note on PR-A — that note
auto-applies to B/C/D as the chain rebases on the resolved A. The
system stays stable because every layer of the stack still passes
its own intent + acceptance checks."

Implementation sketch:
- New table `merge_instructions` (resolver-emitted, JSON body).
- `prompts/conflict-resolver.md` updated to emit instructions in
  addition to the diff.
- `_spawn_conflict_resolver` reads sibling instructions for the same
  cascade (same root parent rebase) before invoking the agent — the
  prompt frontloads "here is what the prior resolver in this cascade
  found and chose to apply."
- `quikode show` surfaces the instruction history under each
  rebase event.
- An **operator-supplied** instruction channel: `quikode annotate
  R-001 "<note>"` writes to the same table with a special source so
  the resolver consumes it on every cascading rebase.

Phase 3 unblocks Phase 2 in practice — without instructional
resolver, a single A-change creating sibling conflicts in B/C/D
re-runs the resolver agent N times on each cascade, and each
invocation is blind to its peers. That cost (and the inconsistency
risk) is the load-bearing reason Phase 2 alone isn't sufficient.

---

### 🔴 Phase B remainder — sonnet review-thread classifier + CI log parser

The state-machine refactor (TRIAGING_FEEDBACK / ADDRESSING_FEEDBACK
+ PENDING_CI / AWAITING_REVIEW / MERGE_READY) shipped 2026-05-04
evening, but the *content* of the triage step still defers to the
fixup planner directly (passing all unresolved threads). The next
step makes triage Python-deterministic:

- `parse_ci_failure(logs) → list[CIFailure]` — pure-python pattern
  matcher for cargo / pytest / clippy / lint output. Extract
  (file, line, error_type, message). Failures we can't classify
  fall through to current behavior. Replaces handing raw 80-line
  log excerpts to the fixup planner.
- `classify_review_thread(thread, plan, recent_diff) → ReviewVerdict`
  — lightweight sonnet call per thread. Outputs verdict ∈
  {correct, incorrect, needs_discussion} + a polite reply for
  INCORRECT. Wired into `_poll_review_threads` as the entry into
  TRIAGING_FEEDBACK: the orchestrator runs the classifier in-process
  (no container, no future), posts auto-replies + resolves
  INCORRECT threads via the existing GraphQL paths, and only then
  dispatches ADDRESSING_FEEDBACK with the CORRECT subset.
- New `quikode/triage.py` module so the logic lives outside the
  orchestrator's poll loop and can be tested independently.

Why deferred to a follow-up: the state machine + driver landing
first means we get the operator visibility win (no more 30-min
opaque "responding_to_review") immediately, while the classifier
work — which involves a new sonnet prompt, host-side claude CLI
invocation, and significant test coverage — gets its own focused
diff. Should land within the next session.

### 🟡 Triage hooks for retry-cause classification

Pairs with the retry-reason taxonomy (above). The
TRIAGING_FEEDBACK step is the natural place to record *why* a
retry is happening — when the CI parser identifies "test
failure: TypeError on x.py:42" that's a structurally different
retry than "rate-limit timeout from codex CLI". Surfacing both
flavors via the classifier makes the resulting subtask plan
better-informed.

## Recently shipped (2026-05-04 evening additions)

- **Stacking-readiness gate (Phase 1 of the stacked-diff vision)** —
  `cfg.stacking_readiness ∈ {"speculative","settled"}` and
  `cfg.stack_settle_quiet_s` (default 600s). In `settled` mode a
  parent qualifies as a stack base only when it's reached MERGE_READY.
  Closes the codex-fixup-storm rebase loop where every review
  round re-rebased every child.
- **Resume-boost in `score_candidate`** — orphan-recovered tasks
  with subtasks already DONE or a PR open now outrank cold roots:
  +25 max from subtask completion fraction, +15 if PR open. Caps at
  +40 so a high-fan-out fresh root with 9+ dependents still wins.
  Closes the "R-0015 was nearly done but a restart picked something
  fresh instead" footgun.
- **Post-PR state machine refactor (Phases A + C + D)** — the
  legacy AWAITING_MERGE / RESPONDING_TO_REVIEW pair was replaced
  with a clean five-state model: PENDING_CI / AWAITING_REVIEW /
  MERGE_READY (the post-PR resting states), TRIAGING_FEEDBACK /
  ADDRESSING_FEEDBACK (the fixup pipeline). State transitions are
  driven by `_classify_post_pr_target_state` on every poll based
  on live CI + thread + settle-window signals. Auto-merge gate
  and settled-task notification now require MERGE_READY, not the
  overloaded AWAITING_MERGE catch-all. TUI labels, briefing
  groupings, and detail-panel phase strings updated to the new
  vocabulary. Migration in `Store._migrate` rewrites legacy values
  idempotently. Closes the operator-visibility complaint about
  "responding_to_review for 30 min with nothing observable."
- **haiku → sonnet sweep** — intent-reviewer and progress-check
  agents default to `claude-sonnet-4-6` for cleaner reasoning on
  the spec-compatibility and trajectory judgments. User has
  subscription capacity for the small bump in cost.
- **Phase B classifier (sonnet review-thread + CI log parser)** —
  the in-process triage step the user's "responding_to_review is
  broken" complaint asked for. CI logs get pattern-matched into
  structured failures (cargo / clippy / ruff / pytest); review
  threads get classified per-thread → CORRECT / INCORRECT /
  NEEDS_DISCUSSION. INCORRECT threads auto-reply via REST
  `/comments/{id}/replies` then resolve via the GraphQL mutation.
  Only CORRECT threads reach the planner.
- **Retry-cause classification** — new `retry_reasons` JSON column
  on subtasks plus a 9-category classifier (doer_output_invalid,
  checker_fail, checker_timeout, container_oom (rc=137),
  container_vanished, agent_cli_rate_limit, pre_commit_hook_fail,
  network_timeout, other). `quikode show <id>` renders the
  histogram per subtask + the most-recent example. Answers "why
  did this retry 17 times" without grepping logs.
- **Phase 2 multi-parent stacking MVP** — schema additions
  (`parent_task_ids`, `parent_branches`, `parent_pr_branches`,
  `parent_merge_base_sha`, `parent_merge_base_branch`) + Store
  helpers + picker-side stamping + worker `_construct_merge_base`
  hook in `_provision_worktree`. When a child has > 1 stack-ready
  parent, the worker creates a synthetic merge-base branch
  (`quikode/<id>-base-<6hex>`) via octopus / sequential `git merge`
  and forks the worktree off that. Single-parent path unchanged
  for backwards compat. Cascade-rebase follow-up filed above.

- **`quikode daemon start --detach` / `-d`** — fork + `os.setsid` +
  stdio redirect to daemon.log, so an interactive shell hangup
  (SIGHUP) can no longer silently kill the supervisor. Parent prints
  child pid + log path and exits 0.
- **Heartbeat-stale watchdog** — every 5s the supervisor reads the
  orchestrator heartbeat; two consecutive stale reads beyond
  `cfg.daemon_heartbeat_stale_kill_s` (default 600s) trigger a
  SIGTERM that the crash path then restarts. Closes the
  hung-not-crashed gap where `child.wait()` blocked forever and
  containers stayed alive doing work nobody could read.

## Recently shipped (2026-05-04 session)

All landed in this session:

- **Settled-task notifications (ntfy + slack)** — operator pings when
  AWAITING_MERGE has been quiet for `cfg.notify_settled_after_s`.
- **Fixup decomposition** for final-check, CI, and review-response —
  monolithic doer replaced with per-slice planner + commit gate.
- **Priority pick at slot-free** — orchestrator picks high-fan-out /
  stacked candidates over leaf roots.
- **Subtask-boundary yield** — workers can surrender slot mid-task to
  higher-priority queued candidates (opt-in via
  `cfg.preempt_at_subtask_boundary`).
- **CI-failure-after-AWAITING_MERGE handler** — daemon dispatches a
  CI-fix cycle when GitHub CI flips post-merge.
- **Branch divergence handling** — pure-FF + force-push BLOCK + diverged
  auto-rebase via `_rebase_diverged_branch`.
- **Stalled-future auto-recovery** — daemon force-recovers
  `responding_to_review` futures with no agent activity for >30min.
- **Per-task abort** — `quikode abort` no longer kills unrelated
  containers in the same workspace.
- **review_rounds_max cap** — codex find-everything-forever defense.
- **review_response_extra_slots** — reviews don't starve at saturation.
- **Idempotent `_open_pr`** — re-entry on a task that already has a PR
  reuses it instead of `gh pr create` failing.
- **ccusage cost sanity cap** — discards parser-misattribution outliers
  > $50/call.
- **Lefthook v2 + python3 baked into dev image.**
- **Per-subtask commit re-attribution after rebase** — the cumulative
  `retries` column seeds the local attempt counter on resume so
  progress-check cadence keeps firing across daemon restarts.
- **TUI**: pending hidden from primary table, viewport auto-scrolls to
  active fixup subtask, detail panel given 2/3 height.
- **Per-task cost rollup**, **progress-check verdict surfacing**,
  **review-thread categorization** in `quikode show`.
- **`--reason` flag** on retry/resume/abort.
- **Cascade-skipped subtasks** re-pended on `quikode resume`.

See `lessons-learned.md` for the load-bearing observations from the live
2026-05-04 driven session.
