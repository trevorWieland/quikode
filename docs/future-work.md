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

---

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
