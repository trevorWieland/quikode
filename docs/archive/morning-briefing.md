# morning briefing — 2026-05-02

## TL;DR

The quikode loop **works correctly end-to-end**: planner → doer → checker (with real verification) → triage → retry → push → PR. The fixture (FastAPI hello endpoint) ran cleanly 3 times in a row early in the night. Three full R-0001 runs against tanren followed; each surfaced something to fix in quikode and exposed real model-capability limits in the doer.

R-0001 has **not yet** reached `AWAITING_HUMAN`. A 4th run is in flight (started 08:11). The doer agent (opencode glm-5.1) wrote substantial real implementation across all 5 interfaces in each run, but failed to converge on the last 1-2 narrow fixes within the 3-attempt do/check budget.

## What worked

- **Loop end-to-end on the fixture**: 3 consecutive clean cycles, each reaching `AWAITING_HUMAN` with a real PR open against `trevorWieland/quikode-fixture`.
- **Codex checker is now genuinely verifying**: after fixing the bwrap-sandbox issue, the checker runs `just ci`, makes real HTTP calls (`POST /v1/accounts` → 200, duplicate → 409, etc.), runs the CLI via shell, invokes MCP tools, reads files. It catches real misalignment.
- **Triage is producing correct, actionable feedback** (claude-opus). Examples from R-0001 runs:
  - "Add `@mcp/@cli/@tui` scenarios to feature file"
  - "Rename `/v1/sessions/current` → `/v1/accounts/me`"
  - "Replace plaintext credentials with opaque fixture handles"
  - "MCP binary is contaminating stdio with tracing logs; switch to stderr"
- **Doer attempt 1 of every run produced real, comprehensive implementation**: ~1500-2600 lines of Rust + TypeScript across all 5 interfaces (API, CLI, MCP, TUI, web), full BDD scenario file with 40+ scenarios passing.
- **Quikode's own resilience improved** while running: 13 issues found and fixed without breaking the running orchestrator.

## What didn't converge

R-0001 is the test case. Across all attempts the **first** doer pass produced a near-complete implementation each time. But:

- **Run #2** (`quikode/r-0001-543588`): all 3 attempts ran. Final fail = 2 narrow issues (incomplete per-interface tags + simulated rather than PTY-driven TUI test driver). Attempt 3 hit the 7200s subprocess timeout at 05:46 → **FAILED**.
- **Run #3** (`quikode/r-0001-a4b79d`): all 3 attempts ran in 2 hours. Final fail = MCP stdio contamination by `tracing` logs. Triage gave a clean specific fix; **doer attempt 3 ignored the triage and fixed an unrelated TUI error-code issue instead**. → **BLOCKED**.
- **Run #4** (`quikode/r-0001-919730`): in flight as of this briefing (planner phase, 08:11).

The pattern: **opencode glm-5.1 is good at the bulk implementation but doesn't reliably internalize triage corrections** for narrow follow-up fixes, especially across short-context attempts 2 and 3 where it has to remember what it already did.

## Recommendations (for your review)

1. **Treat R-0001 as effectively reviewable today**, modulo the MCP stdio fix. The work in run #2 and run #3's attempt 1 worktrees was substantial. (Note: run #2's worktree was lost when attempt 3 hit the 7200s timeout and quikode auto-cleaned BLOCKED worktrees. I've fixed this — BLOCKED/FAILED tasks now keep their worktree on disk for inspection. Run #3 and #4 will retain theirs.)

2. **Consider a different doer for tasks of R-0001's complexity**. opencode glm-5.1 nailed the bulk work but couldn't follow narrow triage corrections. Candidates: claude-opus for both planner and doer; or codex for the doer (its `--dangerously-bypass-approvals-and-sandbox` workspace is now wired correctly).

3. **The doer prompt has been strengthened** to make triage feedback authoritative. Run #4's doer is the first that will see the new wording, so we'll find out shortly whether that helps.

4. **The v0.1 loop is solid enough to point at the broader DAG.** The pieces all work; the variance is in model capability per task. A reasonable next step is to run a small *easier* R-* node (something with fewer interfaces) to validate the end-to-end success path, before R-0001 specifically converges.

## What you'll find in the workspace

```
/home/trevor/github/quikode-runs/tanren/
├── R-0001-attempt1to3.review.md    ← Run #2 export (the most thorough run, killed on timeout)
├── R-0001-run3-blocked.review.md   ← Run #3 export (BLOCKED on MCP fix)
└── .quikode/
    ├── quikode.db                  ← SQLite state (run history, agent calls, artifacts)
    ├── logs/R-0001.log             ← per-task log of every prompt/response
    ├── sccache/                    ← shared rust build cache (~few GB)
    └── worktrees/                  ← preserved for non-MERGED states (run #4 onwards)
```

Useful commands:
```bash
quikode briefing                        # one-shot snapshot (state, transitions, cost, disk, warnings)
quikode show R-0001                     # state timeline + agent costs + artifacts
quikode export R-0001 -o file.md        # full bundle (plan + verdicts + diff)
quikode dag-stats                       # per-milestone DAG progress
quikode watch                           # live-updating table
```

## Quikode itself: what's new since you slept

Critical fixes (blocked tanren, all caught and fixed live):
1. `--dangerously-bypass-approvals-and-sandbox` for codex (bwrap fails inside docker)
2. Unique branch suffixes per run (avoids remote collisions we can't delete)
3. Doer timeout bumped 2h → 4h
4. BLOCKED/FAILED worktrees preserved for inspection (was: deleted)

Improvements / new commands:
- `briefing` — wake-up snapshot
- `dag-stats [--by milestone|layer]` — per-group breakdown
- `export <id> -o file.md` — full review bundle
- `watch` shows in-state elapsed + worktree heartbeat with color thresholds
- `show <id>` shows state timeline + per-call agent cost
- `prune` + `disk-usage` — disk hygiene
- `dev-test` — one-shot fixture validation
- `mark-merged <id ...>` — bootstrap from already-complete tasks
- `run --retry-failed` — auto-reset BLOCKED/FAILED tasks on startup
- `retry <id>` now also cleans the prior worktree

Internal:
- `agent_calls` SQLite table with phase / cli / model / rc / duration / token count per call
- Stalled-task heartbeat in orchestrator
- 32-test pytest suite covers DAG, state, prompts, agent invocations, token parsing

## Decisions waiting for you

1. **Try a different doer for R-0001?** (see Recommendation #2 above)
2. **Push run #2 or run #3 attempt-1 to a PR for manual review** despite the unfixed-MCP bug? It's substantive work that mostly passes.
3. **Smaller R-* node first** to prove the loop without R-0001's complexity?
4. **Begin Phase A from `docs/design-v2.md`** (smart conflict resolution) before opening parallel R-* runs?

Run #4 progress will be visible via `quikode briefing` whenever you check.
