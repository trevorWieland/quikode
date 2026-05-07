# Orientation

This file is the entry point for any agent (human or LLM) joining quikode mid-stream. Read it top to bottom before touching anything.

## What quikode is

An event-driven task-DAG runner that orchestrates AI coding agents (codex, claude, opencode) through a strict per-task FSM (`quikode/fsm.py`). Each DAG node becomes a worker that plans → does subtasks → audits → opens PR → drives to merge. The current target project is **tanren** (`/home/trevor/github/tanren`), running out of workspace `/home/trevor/github/quikode-runs/tanren`.

## Reference docs

- `README.md` — install, quick start, command map.
- `docs/architecture.md` — FSM diagram, store schema, profiles, recovery semantics.
- `docs/runbook-operations.md` — daily operation.
- `docs/runbook-incident-response.md` — failure-mode handling.
- `docs/profiles/tanren.md` — tanren-specific image/CI/budget conventions.
- `docs/roadmap.md` — remaining active work in the runner itself.
- `plans/00-INDEX.md` — list of stability/capability enhancement plans (some shipped, some queued).

## Mission

> **Reliably get every tanren DAG node from initial plan to "awaiting human merge", with zero manual intervention.**
> A successful run is one where no subtask takes more than 5 retries and no task needs more than 3 pre-PR audit cycles. Any run that exceeds either is evidence quikode itself needs refinement, not the work the doer is doing.

The 5-retry / 3-audit-cycle numbers are **soft signal caps**, not hard kill caps. Tasks are *not* automatically blocked for crossing them. The system retains its existing budgets (`subtask_hard_max_attempts=50`, `pre_pr_audit_max_cycles=3`) so that real progress isn't truncated. The agent's job is to *notice* when a soft cap trips and *act* — investigate the root cause and fix it at the system level (FSM transition, prompt edit, scope-review handling, etc.), not merely retry harder.

## Standing responsibilities (every cycle)

1. **Status sweep.** Run `qk briefing` and `qk status` from the workspace dir. Watch for state distribution, retry counts, BLOCKED tasks, stale heartbeat.
2. **Soft-cap audit.** For every in-flight task, scan its subtasks. If any non-DONE subtask has `retries > 5`, that subtask is automatically suspect. Open `qk show <task>` and read the latest triage. Categorize the cause:
   - **Environment / infra** (container OOM, network, disk) → fix the environment, restart agent attempt.
   - **Prompt / instruction conflict** (e.g. doer told one thing, scope reviewer rejecting it) → strengthen the relevant prompt; ship via reinstall.
   - **Doer model failure** (model can't follow the triage) → investigate prompt clarity; consider swapping the model role.
   - **Genuine architectural complexity** (legitimately multi-step) → the planner should have decomposed it; consider planner prompt edits.
   Never just say "let it retry harder." The retry budget exists to absorb noise, not to mask system bugs.
3. **PR-audit cap.** Same principle: if any task is on its 4th `pre_pr cycle`, the audit gauntlet's signal is too noisy or the fixup planner is over-decomposing. Investigate, ship a fix.
4. **Plans documentation.** When you find a system-level fix, write it to `plans/NN-name.md` and add the row to `plans/00-INDEX.md`. Plans land in batches; the index is the source of truth for what's shipped vs queued.
5. **Downtime ≠ idle time.** While the daemon is running and you have nothing urgent, polish quikode itself — write plans, refactor for line budgets, tighten prompts, add tests. Capability enhancement compounds across runs.

## Branch / git policy

- All quikode code changes go to the **`optimizations`** branch. Never commit directly to `main`.
- Commits are small and topic-focused with a clear "why" in the body. End with the standard `Co-Authored-By` trailer.
- Push frequently. The user merges `optimizations` → `main` when batches are ready, often pulling learnings from runs on multiple machines.
- Plan files are committed alongside the code changes that ship them.

## Validation ladder (before any reinstall)

```bash
uv run ruff check quikode tests
uv run ruff format --check quikode tests
uv run ty check quikode tests
uv run pytest tests/ -q
```

All four must pass. Architecture guards include line-length budgets and "no inline lint suppressions" — refactor rather than annotate.

## Reinstall + daemon restart workflow

```bash
# from /home/trevor/github/quikode (the source repo, optimizations branch)
bash scripts/reinstall.sh --skip-tests   # if you've already validated
# from /home/trevor/github/quikode-runs/tanren (the workspace)
qk daemon stop
qk daemon start --detach --max-parallel <N>
```

**Restart cost model.** Restarting the daemon kills every in-flight agent subprocess inside its container — the orphan recovery cleanly resets each affected task to PENDING + a resume marker, and the next worker run picks up from the nearest non-DONE subtask. The cost is **per-task**, bounded by the duration of one in-flight agent call (10–30 minutes), NOT cumulative task runtime. Subtasks commit on completion, so all *prior* progress is preserved on the branch. Don't hesitate to restart for a meaningful fix; do consider clustering several pending fixes into one restart.

**Note.** Prompts (`prompts/*.md`) are loaded fresh per render via Jinja's `FileSystemLoader`, so a reinstall (which updates the bundled prompts at the wheel install path) is enough — no daemon restart needed for prompt-only changes. Python code changes do require a restart.

## Resource sizing

Computed live by `qk resources`. The orchestrator math:

```
budget_cpus  = host_cpus  - cfg.host_reserved_cpus
budget_mem   = host_mem   - cfg.host_reserved_mem_gb
max_parallel = min(budget_cpus / cpu_per_task, budget_mem / mem_per_task_gb)
```

Tunable in `.quikode/config.toml`:
- `max_parallel` — explicit slot count (overrides auto). Set this when you want predictable behavior.
- `cpu_per_task` (default 2), `mem_per_task_gb` — Docker `--cpus` / `--memory` hard caps per dev container.
- `max_parallel_auto = false` — don't recompute at startup; trust the explicit `max_parallel`.

**Sizing for the current host** (WSL on a 24c/256GB Threadripper with `.wslconfig` set to 200GB / 36 processors): `max_parallel=16`, `cpu_per_task=2`, `mem_per_task_gb=10`. CPU and memory rails hit 16 simultaneously — clean fit, no oversubscription, leaves the Windows host 12 logical cores + 56 GB for desktop / light gaming / browsing.

For dedicated cloud (e.g. Hetzner CCX63, 48c/192GB): same `max_parallel=16` is safe; could push to 20 with `mem_per_task_gb=8` if no OOM kills observed.

## Troubleshooting decision tree

| Symptom | First check | Common fix |
|---|---|---|
| `qk daemon status` says STALE for >5 min | `tail` daemon.log for crashes | Stop + restart daemon |
| Task FAILED with `InvalidTransition` | daemon log Traceback | FSM bug — find the helper that fired the wrong event |
| Task BLOCKED at subtask | `qk unblock <id>` for forensics | Read latest triage; if root cause is structural, fix prompt/FSM, then `qk resume <id>` |
| Subtask retries > 5 | `qk show <id>` | See "Soft-cap audit" above — categorize, then fix |
| Audit cycle 4 reached | `qk show <id>` | Audit is over-flagging or fixup planner is too aggressive — strengthen audit acceptance or tighten fixup boundaries |
| `git push` rejected non-fast-forward | `git reflog` in worktree | Doer rewrote history (forbidden) — `git_push_recovery.py` auto-rebases on next attempt; if not, manual rebase + resume |
| Container OOM (rc=137) | `docker stats` for the container | Bump `mem_per_task_gb` and reinstall; transient retries already free-retry |
| Rate limit (429) on gh polls | Daemon log "transient subprocess failure" | `quikode/net_retry.py` already handles with exponential backoff; if it persists, check API tier |
| 7+ tasks all failing on same root cause within minutes | `qk briefing`'s "Recent transitions" | Cluster bug — fix the shared cause (e.g. missing prompts, broken bundled file) and reinstall once for all |
| Multiple tasks BLOCK at 50/50 retries simultaneously, with `qk show` retry histogram dominated by `container_vanished=N` | per-attempt duration <2s across last 30 attempts; daemon log spam: `objective check FAILED (rc=1, 119 bytes)` body = `No such container: …` | Container-vanished cascade. Plan 20's `ensure_dev_container_running` + transient-stderr classification on the gate path is now active; for already-blocked tasks: stop daemon, `qk reset-retries <id>` + `qk resume <id>` per task, restart. See `docs/runbook-incident-response.md` and `docs/incident-2026-05-07-recovery.md`. |
| `InvalidTransition` cascade (multiple tasks FAILED with `event 'crash' is not valid from state 'failed'`) | daemon log tracebacks | Two `qk run` invocations raced on the same workspace. Plan 20's flock on `<state_dir>/orchestrator.lock` prevents recurrence. Recover affected tasks via `qk reset-retries` + `qk resume`. |

When the diagnosis is unclear, prefer wiping a worktree and starting that subtask over (`qk retry <id>`) rather than carrying poisoned state forward. Per the user's standing direction: "It is truly better to wipe a worktree and start over, rather than carry forward poisoned work sometimes."

## Mode of influence

The agent's mode of influence on tanren is the **state machine** and the **prompts** — and quikode's worker / orchestrator code that drives both. Never edit tanren application code directly; if a tanren symptom suggests a fix, the fix lives in:

- `quikode/fsm.py` / `fsm_runtime.py` — FSM events, transitions, recovery semantics.
- `quikode/workers/*.py` / `quikode/orchestration/*.py` — worker phases, scheduler, supervisor.
- `prompts/*.md` — planner / doer / checker / triage / scope-review / progress / fixup-planner / audit prompts.
- `quikode/scope_review.py`, `quikode/net_retry.py`, `quikode/git_push_recovery.py`, etc.

The exception is the worktree itself: when a doer's commit has poisoned a fixable file (e.g. a broken migration), the operator-mediated path is to fix the file directly in the worktree and `qk resume <id>`. This is rare and explicitly authorized — it does NOT generalize to feature work.

## Key invariants the prompts encode

These live in the bundled prompts and are the contract every agent role honors. If a prompt change weakens any of them, you're regressing the system.

- **No CI failure leaks to main.** Every panic, test failure, type error, lint error, or migration error encountered on a quikode branch is the task's responsibility to fix in this attempt — there is no "upstream owner," no "out-of-scope," no "pre-existing." (Encoded in `subtask-doer.md`, `subtask-triage.md`, `subtask-checker.md`, `planner.md`, `progress.md`.)
- **Gate-keeping cross-file fixes are always legitimate.** The scope reviewer accepts edits outside `files_to_touch` when removing them would cause a gate failure. Triage notes from the prior attempt are passed to the scope reviewer as authoritative evidence. (`scope-review.md`.)
- **Doer never rewrites git history.** No `git reset`, `git rebase`, `git commit --amend`, `git checkout <ref>`, `git cherry-pick`. The orchestrator owns commits. (`subtask-doer.md`.)
- **Format violations get the formatter, not hand-edits.** `cargo fmt --all`, `taplo fmt`, `just markdown-fmt-fix`, `prettier --write`. (`subtask-doer.md`.)
- **Checker fails on observed failures, never fabricated ones.** No synthetic acceptance criteria the planner didn't write. The audit gauntlet is the right place for thorough invariants. (`subtask-checker.md`.)
- **One orchestrator per workspace.** `cli_core` acquires an exclusive `fcntl.flock` on `<state_dir>/orchestrator.lock` before container cleanup or orphan recovery; a second `qk run` against the same workspace exits 2 rather than racing. The lock auto-releases on FD close (incl. SIGKILL) so a hard-killed daemon never leaks it. (Plan 20.)
- **Vanished-container failures are free retries, not attempt-counter increments.** Both the agent path and the objective gate path classify rc=137 / "No such container" / "container is not running" / "Error response from daemon" as transient. Each subtask attempt's pre-flight calls `docker_env.ensure_dev_container_running` so a dead container is recreated before the next agent invocation. (Plan 20.)

## Memory system hooks

Persistent memory at `/home/trevor/.claude/projects/-home-trevor-github-quikode/memory/`. The index lives in `MEMORY.md`. Key entries:

- `feedback_long_lived_flow.md` — overnight-run conventions: wipe vs rescue, plans/*.md, daemon-restart authority.
- `feedback_agent_cli_gotchas.md` — non-obvious CLI flags + auth quirks for codex / claude / opencode.
- `project_quikode.md` — project context.

Update memory as you learn things that should outlive the current conversation (user preferences, project decisions, surprising behaviors). Do NOT memorize code patterns or file paths — those derive from the current source.

## Tooling notes

- `qk briefing` — primary status snapshot. Use this every cycle.
- `qk show <task>` — full timeline, agent calls, subtask states, latest triage/checker output.
- `qk subtasks <task> --json` — machine-readable subtask listing; pipe into Python for filtering by retry count.
- `qk tail <task>` — task log tail. Currently captures everything in memory until the agent exits (plan 11 will fix the streaming gap).
- `qk resume <id>` — drop a BLOCKED/FAILED task back to PENDING with a resume marker. Worker picks up from the nearest non-DONE subtask, including leftover audit-driven fixups.
- `qk retry <id>` — fresh restart: clears worktree + branch + subtask rows. Use only when the slate genuinely needs to be clean.
- `qk reset-retries <id> [<subtask>]` — zero retry counters on BLOCKED subtasks of a BLOCKED/FAILED task without discarding committed work. Pair with `qk resume <id>` afterwards. Refuses on actively-running tasks. Designed for the container-vanished cascade scenario where 50 attempts were burned on infrastructure noise rather than real doer work — see plan 20.

## Quick state-of-the-world (as of the most recent commit on `optimizations`)

- 13+ commits on `optimizations` since the rebuild baseline, covering FSM bug fixes, prompt invariants, scope-review with triage context, network backoff, branch-divergence auto-rebase, leftover-fixup pickup on resume.
- Plans 02 (network-backoff), 12 (no-CI-leak), 13 (scope-review gate-fix), 14 (no-fabrication checker) are shipped. Plans 03–11 are queued, in priority order in `plans/00-INDEX.md`.
- Tanren workspace currently configured for `max_parallel=16, mem_per_task_gb=10` — designed for the 200 GB / 36 processor WSL after `wsl --shutdown`.
