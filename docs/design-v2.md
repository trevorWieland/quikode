# quikode v2 design

> **Status: IMPLEMENTED.** This document is kept as architectural reference for the design decisions.
> Current architecture lives in [`architecture.md`](architecture.md). Operational guidance is in
> [`runbook-operations.md`](runbook-operations.md).

All five phases landed 2026-05-02 (Phase 0, Resources, Phase A, Phase B, Phase C). The 76-test count cited below was the v2-completion baseline; the current suite is 581 tests (covers v3 work landed since).

For the current shape of the system, see `architecture.md`. This doc is the design that drove v2.

## What v2 brings, in priority order

| Phase | What | Why | Depends on |
|---|---|---|---|
| **Phase 0** | **Subtask breakdown** | Doer convergence on multi-step tasks | nothing |
| **Resources** | Per-task cpu/mem caps + `quikode resources` | Predictable parallelism, OOM safety | nothing |
| **Phase A** | Smart conflict resolution | Parallel disjoint tasks land cleanly | Phase 0 reuses |
| **Phase B** | Intent-gap detection on dep merges | Catches silent intent drift when one task merges under another's plan | Phase 0 + A |
| **Phase C** | Stacked diffs (deferred) | Tight dep chains run concurrently | A + B mature |

Recommended build order is exactly that table. Each phase is independently shippable.

---

## Phase 0 — subtask breakdown

### Problem this solves

The pattern across every R-0001 attempt: doer attempt 1 produces 95% of the work in one ~75-min session. Checker finds 1-2 narrow gaps. Attempts 2 and 3 are either suspiciously fast (5-10 min, doer made the *wrong* fix) or hit subprocess timeouts. The session terminates between attempts so attempts 2/3 are essentially fresh-context invocations that have to rebuild a mental model from a long markdown plan and a brief triage note. That's hard for any model and harder for glm-5.1 specifically.

### Proposal

Replace monolithic do/check with a **per-subtask** loop driven from a structured planner output.

**Planner emits JSON** (instead of free-form markdown):

```jsonc
{
  "node_id": "R-0001",
  "subtasks": [
    {
      "id": "S-01",
      "title": "Add account/event domain types to tanren-identity-policy",
      "depends_on": [],
      "files_to_touch": [
        "crates/tanren-identity-policy/src/account.rs",
        "crates/tanren-identity-policy/src/lib.rs"
      ],
      "boundary": "Domain crate only. No persistence, no events, no service logic.",
      "acceptance": [
        "cargo check -p tanren-identity-policy passes",
        "module exports `Account`, `OrgId`, `Invitation`, `InvitationToken`",
        "IdentityError has DuplicateIdentifier, InvitationExpired, InvitationInvalid, WrongCredentials variants"
      ]
    },
    { "id": "S-02", "depends_on": ["S-01"], "...": "..." },
    { "id": "S-08-bdd-feature", "depends_on": ["S-01..S-07"],
      "acceptance": [
        "tests/bdd/features/B-0043.feature exists",
        "every scenario has exactly one of @positive/@falsification + exactly one interface tag",
        "scenarios cover all 7 witnesses listed in B-0043's expected_evidence"
      ]
    }
  ],
  "final_acceptance": [
    "just ci passes",
    "all 7 witnesses from B-0043's expected_evidence are exercised by passing BDD scenarios"
  ]
}
```

**FSM gains a subtask loop:**

```
PLANNING → DOING_SUBTASK[1] → CHECKING_SUBTASK[1] ──pass──► DOING_SUBTASK[2] → ...
                                       │
                                     fail
                                       │
                                       ▼
                                TRIAGING_SUBTASK
                                       │
                              ┌────────┴────────┐
                          retry              give-up (per-subtask budget exceeded)
                              │                    │
                              └─→ DOING_SUBTASK[N]  └─→ flag subtask as BLOCKED, continue
                                                        (final check will catch missing pieces)

                          (after all subtasks)
                                       ▼
                                FINAL_CHECKING (the existing checker, scoped to whole spec)
                                       │
                              ┌────────┴────────┐
                            pass              fail
                              │                  │
                          COMMITTING        BLOCKED (with per-subtask diagnostics)
```

**Wins:**
1. **Doer context per call is small.** ~1 subtask + ~3 file paths + 3-5 acceptance bullets. Way under model budget.
2. **Earlier failure isolation.** If subtask 3/8 fails, you know which area; triage is scoped to that subtask, not the whole spec.
3. **Per-subtask retry budget.** Generous (e.g. 2 × 8 subtasks = 16 attempts at the slice level) without burning hours on each.
4. **Lightweight per-subtask checker.** Most subtasks are "compiles + has function X + grep for Z returns ≥1" — codex with the sandbox flag handles this in under a minute, not 10.
5. **Parallelizable within a node.** Subtasks with disjoint `files_to_touch` and no `depends_on` between them can run concurrently. Significant win on big nodes.

### Mechanics

1. **New planner prompt** (`prompts/planner-v2.md` or replace `planner.md`) — instructs the planner to emit strict JSON. Specify a JSON schema, validate on receipt, on validation failure run the planner once more with the validation error as feedback.

2. **New subtask prompts:**
   - `prompts/subtask-doer.md` — implements ONE subtask. Reads task title + boundary + files + acceptance. Stops when acceptance is satisfied.
   - `prompts/subtask-checker.md` — verifies a single subtask's acceptance criteria. Output: `VERDICT: PASS|FAIL` + per-criterion verdict + ROOT_CAUSE on fail.
   - `prompts/subtask-triage.md` — same shape as v0.1 triage but scoped to one subtask.

3. **Database schema**:
   ```sql
   CREATE TABLE subtasks (
     id            INTEGER PRIMARY KEY AUTOINCREMENT,
     task_id       TEXT NOT NULL,
     subtask_id    TEXT NOT NULL,            -- e.g. "S-01"
     title         TEXT,
     depends_on    TEXT,                     -- JSON array of subtask_ids
     files_to_touch TEXT,                    -- JSON array
     boundary      TEXT,
     acceptance    TEXT,                     -- JSON array
     state         TEXT NOT NULL,            -- pending|doing|checking|triaging|done|skipped|blocked
     retries       INTEGER DEFAULT 0,
     created_at    REAL,
     updated_at    REAL,
     UNIQUE(task_id, subtask_id)
   );
   ```

4. **Worker FSM additions** — in `worker.py`:
   - `_plan()` produces JSON, validates, persists subtasks rows.
   - `_do_check_loop()` becomes `_subtask_loop()` — iterates subtasks in topological order, calls `_do_subtask()` and `_check_subtask()`. Handles per-subtask triage + retry within budget.
   - `_final_check()` is the existing checker, run once after all subtasks complete (or are skipped).

5. **Config**:
   ```toml
   [subtasks]
   enabled = true
   max_retries_per_subtask = 2
   parallel_within_node = false   # phase 0.5 — opt-in
   subtask_doer_timeout_s = 1800  # 30 min
   subtask_checker_timeout_s = 600
   ```

6. **CLI**:
   - `quikode show <task-id>` shows per-subtask state in addition to current artifacts.
   - `quikode subtasks <task-id>` (new) — table of subtasks with state, retries, file list.

### Edge cases

- **Planner returns malformed JSON** — re-prompt once with the validation error. If still bad, fall back to legacy markdown planner (with a warning surfaced in the briefing).
- **Subtask dependencies form a cycle** — fail fast at planning; treat as planner error.
- **A subtask edits files outside `files_to_touch`** — log a warning. Don't reject (the planner's file estimate is approximate). The boundary discipline is on the doer prompt, not enforced.
- **Final checker fails after all subtasks pass** — go to `TRIAGING` (existing whole-spec triage), then `DOING_SUBTASK[N+1]` for any newly-spawned cleanup subtask, OR back to whichever existing subtask the failure points to. The triage agent should output which subtask to re-do.

### Validating Phase 0 against the fixture

The current FastAPI fixture is too trivial — one endpoint, no real subtask structure. **Expand it** for Phase 0 testing:

```
quikode-fixture/
  app/
    main.py        — register routes
    health.py      — existing /health
    hello.py       — /hello (subtask: greeting endpoint)
    goodbye.py     — /goodbye (subtask: farewell endpoint)
    util.py        — shared formatter (subtask: util module, depended on by both endpoints)
  tests/
    test_health.py
    test_hello.py
    test_goodbye.py
    test_util.py
```

DAG:
```
T-001-util       (no deps)
T-001-hello      (depends_on: [T-001-util])
T-001-goodbye    (depends_on: [T-001-util])
T-001-register   (depends_on: [T-001-hello, T-001-goodbye])
```

That's a four-subtask flow with one fan-out point and one fan-in. Tests Phase 0 properly without needing a tanren-scale task to converge.

---

## Resource controls

### Problem

Containers are unconstrained. A runaway rust build (or memory-hungry agent) can OOM the host. Even healthy concurrent containers can starve each other. WSL is currently capped at 80GB total; we have 24 cores. Need to set per-task ceilings and choose `max_parallel` based on host headroom.

### Proposal

**Per-task docker run flags:**
```
docker run --cpus=$cpu_per_task \
           --memory=${mem_per_task_gb}g \
           --memory-swap=${mem_per_task_gb}g  # no swap; OOM cleanly
           ...
```

**Config:**
```toml
[resources]
cpu_per_task = 4
mem_per_task_gb = 12
host_reserved_cpu = 4         # leave for host + sccache + agent CLIs
host_reserved_mem_gb = 16
max_parallel_auto = true      # if true, compute from above
```

When `max_parallel_auto = true`, the orchestrator on startup:
- reads `nproc` and `/proc/meminfo` (or `docker info` for WSL effective limits)
- subtracts `host_reserved_*`
- computes `max_parallel = min((cpu - res) // cpu_per_task, (mem - res) // mem_per_task_gb)`
- logs the calculation; uses the result unless overridden by `--max-parallel` CLI flag.

**`quikode resources` (new command):**
```
host: 24 cores, 80 GB RAM (WSL cap)
reserved: 4 cores, 16 GB
budget: 20 cores, 64 GB
per-task: 4 cores, 12 GB
max parallel (auto): min(20/4, 64/12) = 5

current: 1 task running
  R-0001  4 cores cap, 8.2 GB used / 12 GB cap, 0.6 cores 1m avg
free budget: 16 cores, 51 GB
```

**Briefing additions:**
- New "max RSS" column in the in-flight table (sampled every 30s via `docker stats --no-stream`).
- Warning row if any container's max RSS in last 5 min is >80% of `mem_per_task_gb`.

**Persistence:**
```sql
CREATE TABLE container_stats (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id       TEXT NOT NULL,
  container_id  TEXT,
  cpu_pct       REAL,
  mem_bytes     INTEGER,
  ts            REAL NOT NULL
);
```

This lets you accumulate empirical data: "R-0001's 99th-percentile RSS was X GB" — and tune `mem_per_task_gb` to actuals, not guesses.

---

## Phase A — smart conflict resolution

(Carried forward from v1 of this design doc, mostly unchanged.)

### Problem

Two parallel tasks A and B both touch the same file. Both pass their own checks. Both open PRs. The second one to merge will hit a merge conflict.

### Proposal

New states `REBASING`, `CONFLICT_RESOLVING`. After PR opens, the polling loop also checks `mergeable`. On `CONFLICTING`:
1. Try `git rebase origin/main`.
2. Clean rebase → force-push-with-lease → back to polling.
3. Conflict during rebase → spawn conflict-resolver agent with: original plan, task diff, conflicting commits, conflicted files (with markers).
4. Resolver edits markers; checker re-runs; if pass → commit + force-push; if fail → triage cycle.
5. Resolver budget = 2; on exhaustion → `BLOCKED` for human review.

**Reuses:** the same checker pipeline as do/check loop. The triage prompt is the same. Only the resolver is new.

**Config:**
```toml
[conflicts]
auto_resolve = true
conflict_resolver_cli = "claude"
conflict_resolver_model = "claude-opus-4-7"
max_resolve_attempts = 2
```

---

## Phase B — intent-gap detection

(Carried forward.)

### Problem

> Imagine A is to add a new instance of foo, while B is to add bar to every foo. If these go in parallel, when A merges in, even if B doesn't have a merge conflict, there might be an intent gap.

The dangerous failure mode: B's PR has no merge conflict, B's CI is green, but a human reviewer would catch that the world has shifted under B's plan.

### Proposal

After **any** quikode-managed task transitions to `MERGED`, every other in-flight task gets an intent re-check.

```
MERGED transition → for each task X in (POLLING_CI, AWAITING_HUMAN, DOING, CHECKING):
                          ↓
                     INTENT_REVIEWING  ← debounce: at most one per task per 120s
                          ↓
                  intent-reviewer agent
                          ↓
        ┌─────────────────┼─────────────────┐
    NO_DRIFT          MINOR_DRIFT       INTENT_CONFLICT
        │                  │                  │
    continue          rebase + recheck    REPLANNING (existing planner with augmented prompt)
                                                 │
                                              DOING (subtask loop resumes)
```

**Reviewer prompt input:** original plan, task's working diff, diff of main since `base_ref_sha`. Strict output: `NO_DRIFT | MINOR_DRIFT | INTENT_CONFLICT` with affected areas + explanation.

**Why a separate "intent-reviewer" role:** narrow context, much shorter responses than the planner. Cheap to run aggressively after every MERGED. Haiku-class is enough.

**Config:**
```toml
[intent]
check_on_dep_merge = true
debounce_seconds = 120
max_intent_reviews_per_task = 5
max_replans_per_task = 2
intent_reviewer_cli = "claude"
intent_reviewer_model = "claude-haiku-4-5-20251001"
```

---

## Phase C — stacked diffs (deferred)

For tight dep chains where blocking-on-merge starves parallelism. Defer until Phases 0/A/B prove out — they capture most of the parallelism win without stacking's complexity (force-push-on-parent, cascade rebases, GitHub branch retargeting).

---

## Suggested rollout

1. **Phase 0** — biggest payoff for R-0001-class convergence. Validate against the expanded fixture before pointing at tanren.
2. **Resources** — quick, cheap to ship, removes a sharp edge before going parallel.
3. **Phase A** — needed before opening any parallel R-* runs.
4. **Phase B** — needed before opening any parallel R-* runs that share territory.
5. **Phase C** — optional optimization, defer.

Phases 0 + Resources + A + B is the minimum kit to drive the tanren DAG end-to-end without supervision.

---

## Decisions still open

1. **JSON schema validation strictness for planner output.** Hard reject + re-prompt once, then fall back to legacy? Or always require valid JSON? Recommend strict-with-one-retry-then-fallback initially.
2. **Subtask-level parallelism.** Phase 0.5 — within a single node, run independent subtasks concurrently. Cheap to add once Phase 0 lands but adds container-count pressure. Defer until needed.
3. **Resource limit enforcement vs. soft.** Today: `--memory=Ng` is a hard limit; OOM kills the container. Alternative: track usage but don't enforce. Recommend hard limits with a generous default.
4. **Force-push policy for Phase A.** Required to land rebased branches. GitHub flags review staleness on force-push. Acceptable cost.
5. **Intent reviewer model size.** Haiku is cheap but might miss subtle drift. Benchmark on a known intent-gap test case before committing.

---

## Why this matters more than it looks

tanren has **232 nodes** with depth 15 and a layer-5 width of 70. Today's "block on merge + monolithic doer" model means:
- **v0.1 worst-case throughput:** ~15 cycle-layers × 2-3 hours = ~40-50 hours wall clock for the full DAG, with poor convergence on complex nodes.
- **v2 (Phase 0 + Resources + A + B):** layers overlap, conflicts auto-resolve, intent gaps are caught, and per-subtask retries make individual nodes more reliable. Estimated **10-15 hours wall clock**, plus dramatically better convergence per node.

Phase 0 alone is worth 3-5× wall-clock improvement on multi-interface nodes because the doer doesn't have to context-rebuild between attempts.
