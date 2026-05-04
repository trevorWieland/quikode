# runbook — tanren watch points

Things specific to driving [tanren](https://github.com/trevorWieland/tanren)
that bite. For ops generally, see `runbook-operations.md`. For breakage
recovery, see `runbook-incident-response.md`.

## DAG depth & breadth

tanren's roadmap has 3+ deep stacked dep chains. Verify your config has
enough headroom:

| Knob | Default | Tanren needs |
|---|---|---|
| `stacking_max_depth` | 6 | ≥ 6 (default raised from 4 specifically for tanren chains) |
| `stacking_max_breadth_per_root` | 12 | Usually fine; check with `quikode dag-stats --by milestone` if a single root spawns more |

Note: the breadth cap is **per stack root**, not per milestone. tanren's
M-XXXX milestones can span 70+ tasks, but those split across many roots.
Hitting the breadth cap usually indicates a planning bug.

## BDD convention (tanren-specific contract)

tanren enforces a strict `.feature` file contract at `tests/bdd/features/`:

- One file per behavior at `B-XXXX-<slug>.feature`
- Closed tag allowlist: feature has `@B-XXXX`; scenarios have `@positive` or `@falsification` plus 1–2 of `@web|@api|@mcp|@cli|@tui`
- Strict-equality coverage: every interface in the behavior's `interfaces:` set needs both a positive and (when listed) falsification scenario
- `Scenario Outline` / `Examples:` are forbidden; `Background:` / `Rule:` are allowed
- Two-interface scenarios need a `# rationale:` comment line above the tag block
- Three+ interface tags is a hard error

Enforced by `xtask check-bdd-tags`, wired into `just check`. The
checker prompt explicitly references this command.

If tanren's BDD framework changes, update `prompts/planner.md` and
`prompts/subtask-doer.md` to match. Tests that grep the prompts for
`B-XXXX`, `@positive`, `@falsification`, the closed allowlist will fail
loudly if a prompt update misses something.

## `xtask check-bdd-tags` — the targeted validator

When the orchestrator's checker reports a BDD-lane fail in `just ci`,
running `just check-bdd-tags` standalone produces the actionable output
(specific scenario / tag violations). The checker prompt instructs
agents to do this on BDD failures.

`python3 scripts/roadmap_check.py` is a separate validator for
orphan-feature errors (a `.feature` file with no behavior id in the
DAG).

## First-time-of-day cold cache

`cargo build --workspace --locked` on a cold sccache is slow (~10–15
min on the tanren codebase). The shared sccache amortizes across
parallel containers within a single run, but the first container of
the day pays the full bill.

Mitigation: queue an unimportant task first (e.g. a small R-* node) so
the slow build happens once, and subsequent parallel tasks run
fast. Pre-warming via `quikode warm-cache` is on the future-work list
(see `future-work.md` — V3-010).

## Squash-merge with `--delete-branch`

tanren's repo policy is squash-merge with `--delete-branch`. This means:

- Stacked children pointing at a parent's branch get auto-closed by GitHub when the parent merges. Quikode's auto-recreation path handles this — see `runbook-incident-response.md` "PR auto-closed".
- `git rebase origin/main` on a stacked child re-applies the parent's commits (which are now folded into a single squash on main), causing duplicate-commit conflicts. Quikode uses `git rebase --onto origin/main <parent_sha>` to drop the parent's history. See `worker.py:run_rebase_to_main`.

Don't change tanren's merge policy without thinking about both effects.

## R-0001 / R-0002 history

R-0001 was the canonical first-real-task validation and merged into
tanren `main`. **Future agents must not re-attempt R-0001 from
scratch.**

R-0002 (Create an organization) is the canonical review-loop /
fixup-decomposition validation handle as of 2026-05-04. PR #143 has
been the primary handle but the PR number can shift after auto-close
+ recreation. Always check current state:

```bash
quikode show R-0002
quikode subtasks R-0002
gh pr view <pr> --json state,mergeable,statusCheckRollup
```

If R-0002 is in AWAITING_MERGE with all CI green and threads resolved,
the right action is review/merge (or rely on the settled-task ntfy
ping). If it's BLOCKED with "review_rounds_max exceeded", codex has
been finding nits forever — operator picks: merge as-is or abort with
a reason.

The DAG node count went 232 → 233 when F-0002 was added. tanren's
roadmap will continue to grow; quikode seeds-on-demand from the DAG so
new nodes appear without intervention. Removed nodes leave orphan rows
(see `future-work.md` V3-008).

## Manual probes (V3-005, still open)

tanren's `expected_evidence.kind == 'manual'` evidence type — running a
`tanren-mcp` binary in the background, hitting `curl /health`, etc. — is
**not auto-runnable** by the checker. The current checker prompt emits
`MANUAL_PROBE_REQUIRED` when it encounters one, surfaces that to the
user, and falls back to `quikode mark-merged` for human override. This
blocks any R-* nodes whose primary evidence is manual probes (rare
today; growing as tanren matures).

The fix is a "manual-probe runner" subagent — see `future-work.md`.

## Container resource accounting

tanren's `cargo build` is memory-hungry — peak ~3 GB per container.
Plus rust-analyzer-style intermediate artifacts. With
`mem_per_task_gb=12` (default; tanren workspace runs at 12), five
parallel containers reserve 60 GB + host headroom
(`host_reserved_mem_gb=16`). On a 78 GB host that's the comfortable
ceiling; SQLite contention rises non-linearly past ~7 in any case.

Don't push `--max-parallel` past 7 unless you've checked headroom:

```bash
quikode resources             # shows computed cap + host actuals
```

`quikode resources` shows what `max_parallel_auto=true` would compute
(without enabling it). If the auto-computed value is below your
explicit `--max-parallel`, you'll OOM under load.

`container_stats` table records periodic samples (`docker stats`-style)
per running container, sampled every `container_stats_sample_seconds`
(default 30s). Use it to tune `mem_per_task_gb` to actuals.

## CI lane to pay attention to

`just ci` runs (in order):

1. `xtask check-bdd-tags` — BDD tag validator. Most-frequent failure source for R-* nodes.
2. `cargo check` / `cargo test --workspace` — Rust correctness.
3. `cargo deny` / `cargo machete` / `cargo doc` — supply chain + docs.
4. `web-{install,build,lint,typecheck,format-check}` — frontend lane.

The checker prompt already surfaces `just ci` output. The BDD lane
failures are the most informative — almost always a tag or coverage
violation that the agent can fix in one round trip.

## Why glm-5.1 stays as doer despite shaky convergence

User policy: balance subscription usage across the three providers
(claude / codex / opencode). The structural mitigations are:

- Subtask breakdown (planner emits per-slice instructions instead of one big plan)
- Per-subtask commits (lost work is bounded to one slice)
- Progress-check agent (BLOCKs flatlined slices early instead of burning the budget)
- Pre-commit hooks per slice (formatting / lint violations surface immediately, not at the end)

If you find yourself wanting to swap doer models, **don't** without
checking with the user first. Edit `.quikode/config.toml` per-workspace
if you need a one-off swap for an experimental run.

## Tanren rollout phases

Each phase is a checkpoint. If a phase surfaces issues, fix them before
advancing — don't skip phases.

### Phase 1 — Single-task validation (`--max-parallel 1`)

- **Scope:** one R-* task at a time, no parallelism, stacking off.
- **Command:** `quikode daemon start --only R-0002 --max-parallel 1 --retry-failed`
- **What to watch:** full pipeline holds against real BDD/build/test
  surface (tanren scale).
- **Success:** task lands MERGED within 1-2h with no manual
  intervention beyond review + merge.
- **When to advance:** 1-2 R-* tasks land clean.

### Phase 2 — Parallel independent tasks (`--max-parallel 3`)

- **Scope:** 2-3 tasks with no `depends_on` overlap; stacking still off.
- **Command:** `quikode daemon start --max-parallel 3 --retry-failed`
  (no `--only`).
- **What to watch:** `cargo build` cold-cache time, codex/claude
  rate-limit hits, container memory pressure (~3GB peak per task),
  SQLite locking under concurrency.
- **Success:** 3 unrelated tasks land in parallel without each other's
  failures.
- **When to advance:** a milestone's worth of independent tasks lands
  clean.

### Phase 3 — Stacked diffs + parallel-5 + notifications (current)

- **Scope:** dependent R-* tasks within a milestone; `--max-parallel 5`.
- **Config:** `[stacking] strategy = "within-milestone"`,
  `notify_settled_channel = "ntfy"`,
  `preempt_at_subtask_boundary = true` (optional),
  `review_rounds_max = 15`.
- **Command:** `quikode daemon start --max-parallel 5 --retry-failed`.
- **What to watch:** `--onto` rebase semantics on real tanren diffs,
  conflict_resolver agent cost (~$0.50-1.00/call on real BDD),
  mid-flight parent-merge handling firing at 5 worker checkpoints,
  settled-task ntfy delivering to phone, fixup decomposition cost
  (planner+doer+commit per slice instead of monolithic doer).
- **Success:** 5 stacked tasks land in parallel with auto-rebases
  clean and review-response cycle ending in green CI + resolved
  threads.
- **When to advance:** a fully-stacked milestone lands without aborts
  AND CI-fail-after-AWAITING_MERGE recovery has fired at least once
  cleanly.

### Phase 4 — Auto-merge enabled (full autonomy)

- **Scope:** same as Phase 3 + auto-merge.
- **Config change:** `.quikode/config.toml` `auto_merge_when_clean = true`.
- **What to watch:** tasks merge themselves once green + threads
  resolved + age threshold met. Verify per-task cost ceilings.
  Walk-away test: leave for 4-8h, return to find tasks merged.
- **Success:** 24h unattended run with N tasks merging via daemon
  (zero human merge clicks).
- **When to advance:** 24h walk-away holds clean.

### Phase 5 — Scale up parallelism (`--max-parallel 7+`)

- **Scope:** same as Phase 4 + scaled parallelism.
- **Resource math:** each tanren task uses ~3GB RAM peak + ~2 effective
  cores. With 78GB host (current) and 24 cores, the memory ceiling is
  min((78-16 reserve) / 12, 24/2) ≈ 5 with `mem_per_task_gb=12`, or
  ~14 if dropped to `mem_per_task_gb=4`. Going past parallel-7 also
  requires the per-task SQLite connection refactor (see
  `future-work.md` "Per-task SQLite connection") — single shared
  connection becomes the bottleneck around then.
- **Config change:** `[resources] max_parallel_auto = true` OR
  `quikode daemon start --max-parallel 7 --retry-failed`.
- **What to watch:** docker storage pressure (each container is ~1GB
  layer), sccache contention (warm via `quikode warm-cache` first to
  avoid 10× cold-cargo penalty), agent CLI rate limits become real
  with 7× concurrency, SQLite `_tx_lock` p99 latency rising past
  ~50ms.
- **Success:** 7+ parallel tasks land with no host-resource
  saturation alerts and no SQLite lock starvation.
- **When to advance:** this is steady-state for tanren workflow.
