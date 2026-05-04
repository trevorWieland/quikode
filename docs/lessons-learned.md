# lessons learned

Empirical observations from getting quikode v0.1 working end-to-end. Sourced from ~10 hours of real driving on the FastAPI fixture and 4 sequential R-0001 attempts on tanren. Each item below was once an hour of bewilderment.

## Agent CLI quirks

### codex defaults to a read-only sandbox

`codex exec` without flags runs in `--sandbox read-only`. The agent literally cannot edit files. We fixed this by passing `--sandbox workspace-write` initially, then escalated to `--dangerously-bypass-approvals-and-sandbox` when we discovered codex's inner bwrap sandbox can't create user namespaces inside an unprivileged docker container — exec_command silently falls back to a GitHub-API file fetch which 404s on unpushed branches, producing bogus FAIL verdicts. Since the docker container is itself a sandbox, bypassing codex's inner one is fine.

### codex prints a verbose preamble + token count to stdout

Workdir, model, session id, "user", "codex", "tokens used" all interleaved with the actual response. We use `--output-last-message <file>` to write only the final answer to a tempfile, redirect codex's verbose stream to stderr in the wrapper, then `cat` the tempfile. Stdout becomes a clean response.

### codex's "tokens used" output is parseable

`tokens used\n<N>` shows up reliably on stderr. We parse it via regex (`quikode/agents/base.py::parse_tokens`) and persist to `agent_calls.tokens_used`. Claude-code and opencode in text mode don't surface tokens reliably — would need `--output-format json` parsing, deferred.

### claude-code expects `~/.claude.json` at $HOME root

Not inside `~/.claude/`. The dir alone is not enough — claude exits 137 with "Claude configuration file not found" repeated three times. The entrypoint copies both.

### opencode uses sqlite WAL — needs a writable mount

`Failed to run the query 'PRAGMA wal_checkpoint(PASSIVE)'` if `~/.local/share/opencode/` is mounted read-only. The fix isn't to mount RW (parallel containers would fight on the same db) — it's to mount the host auth at `/host-auth/opencode-data:ro` and have the entrypoint copy to `/home/dev/.local/share/opencode/` so each container has its own writable copy.

### `bash -lc` strips Dockerfile `ENV PATH`

A login shell sources `/etc/profile` and rebuilds PATH. `claude`, `codex`, `opencode` were all "command not found" inside `docker exec ... bash -lc 'claude ...'` even though they were on PATH per the Dockerfile. Fixed by writing PATH additions to `/etc/profile.d/quikode.sh`.

### opencode glm-5.1 is variable on long sessions

In our R-0001 runs, doer attempt 1 reliably produced ~1500-2600 lines of comprehensive multi-interface implementation in a single ~75 min session. But attempts 2 and 3 (after triage feedback) were either suspiciously fast (5-10 min, made wrong fix) or hit subprocess timeouts. The session terminates between attempts so it's effectively cold-start each time. The doer prompt has been strengthened to make triage authoritative; subtask breakdown (v2 Phase 0) is the structural fix.

## Container infrastructure

### `useradd` is in `/usr/sbin`, not in root's PATH during docker build

`debian:trixie-slim` has `useradd` at `/usr/sbin/useradd` but `/usr/sbin` isn't in root's PATH for `RUN`. Use the absolute path or it'll fail with "command not found".

### Worktree's `.git` file references the parent repo by absolute host path

`git worktree add ../wt/X -b foo` creates `<wt-path>/.git` containing `gitdir: <repo>/.git/worktrees/X`. That's a host path. Inside the container, git follows the redirect and looks for the same absolute path; if not mounted, you get `fatal: not a git repository: <path>` and the container exits 128. Fixed by mounting `<repo>/.git` at the same absolute path inside the container.

### git's safe-directory check trips on uid mismatches

The container runs as the host's UID via `--user $(id -u):$(id -g)`. Inside, the dev user is uid 1000, but mount ownership shows up differently. Git refuses to operate on "dubious ownership" dirs. We work around with `git config --global --add safe.directory '*'`.

### Docker exec's entrypoint runs at container start, not at exec

The entrypoint copies auth files. If you `docker exec` immediately after `docker run -d`, you race the copy. Symptom: claude exits 137 with "config not found" because the file isn't written yet. Fixed by having the entrypoint touch `/tmp/qk-ready` as its last step and having the orchestrator poll for that sentinel before any agent invocation.

### `gh auth setup-git` + fallback `/tmp/.git-credentials`

`GITHUB_TOKEN` in env is not enough for `git push` — git needs a credential helper. Run `gh auth setup-git --hostname github.com` in the entrypoint (gh becomes the credential helper using the env token), and write a backup `https://x-access-token:$TOKEN@github.com` to `/tmp/.git-credentials` as belt-and-suspenders.

### Branch collisions are real and you can't always force-push

If a prior run pushed `quikode/r-0001` to the remote and you can't delete the remote branch (permission denied, hooks, etc.), `git push -u origin quikode/r-0001` from a fresh divergent branch fails non-fast-forward. Solution in v0.1: every run gets a unique 6-hex suffix on the branch name (`quikode/r-0001-<hex>`), so collisions never happen.

### sccache safely shares state across parallel containers

Tested with 3 parallel rust builds against a single shared `/sccache` mount. Sccache uses file locks correctly. Each task still gets its own ephemeral `/home/dev/cargo-target/` so concurrent builds can't corrupt each other.

## Tanren-specific things

### F-0001 scaffolded an stdio MCP, but the architecture mandates HTTP Streamable

`docs/architecture/technology.md:122` and `delivery.md:582` are explicit: stdio MCP is rejected as a product transport. F-0001's `bin/tanren-mcp/src/main.rs` uses `rmcp::transport::io::stdio` which is wrong. R-* tasks inherit this bug — they extend the empty stdio MCP because boundary discipline says they can't change the transport. The fix is **F-0002**, a foundation patch outside any R-* task's scope.

### `just ci` is the gate; ~5 min per pass on tanren

Runs check + tests + deny + doc + machete + web-{install,build,lint,typecheck,format-check}. No parallelism between stages. The web stages alone are ~30-40s. Rust portion dominates first run; sccache makes subsequent runs much faster.

### The 500-line-per-file budget bites in BDD

`crates/tanren-bdd/src/steps.rs` grew past 500 lines fast in R-0001 runs. The fix is to split into per-interface modules (`steps/api.rs`, `steps/cli.rs`, etc.) — which was also the right architectural answer for real per-interface drivers, not just a line-budget escape.

### Cucumber scenario counting is non-obvious

A `Scenario Outline` with N example rows × 5 interface tags counts as 5N scenarios at runtime. R-0001 has ~7 witnesses; the planner correctly enumerated them; the doer wrote 9 source `Scenario` blocks; runtime expanded to 40-46. **Don't conflate "BDD scenarios reported by the runner" with "distinct witnesses in the behavior catalog"** — they differ by an interface multiplier.

## Loop-level observations

### Doer attempt 1 is the highest-leverage attempt

Across all R-0001 runs, attempt 1 produced 95% of the work. Attempts 2 and 3 are correction passes — but opencode's session boundaries make corrections unreliable. v2's subtask-breakdown approach replaces "monolithic do + iterative triage" with "many small dos, each verified individually" specifically to address this.

### Triage from claude-opus is consistently sharp

Every triage we've inspected was correct, specific, and actionable: cited exact files+lines, named the right binaries, gave concrete fix suggestions. The doer ignoring triage is the failure mode, not triage being wrong. The doer prompt now explicitly says "triage is authoritative; do not deviate."

### Checker (codex with sandbox-bypass) does real verification

Not just file inspection — runs `just ci`, makes real `curl` calls against a booted API, runs the CLI binary with `assert_cmd`, invokes MCP tools. False positives are rare; the checker often catches things that look fine in the code. Worth the agent cost.

### subprocess.run timeout fires only on the parent process

If the doer subprocess goes 7200s, subprocess.run raises TimeoutExpired but the kernel actually keeps the subprocess alive briefly. We saw a ~3-min "ghost still doing" period before the actual transition to FAILED. Doer timeout has been bumped to 14400s (4h) to give multi-interface tasks like R-0001 enough headroom.

### "Empty doer output" is a real failure mode

Run #3 of R-0001: doer attempt 3 returned in 1.5 min with `"Fixed bin/tanren-tui/src/main.rs ... [unrelated TUI tweak]"`. Triage had asked for an MCP fix. The doer hallucinated completion. We don't yet detect this; v2 should treat "doer made too few changes for the triage scope" as a soft retry signal.

## Run timings observed

| Run | Doer attempts | Total wall | Result |
|---|---|---|---|
| Fixture (FastAPI) | 1 | ~3:45 | AWAITING_HUMAN ×3 consecutive |
| R-0001 #1 (broken codex) | 2 | aborted | manual abort |
| R-0001 #2 | 3 (74m, 7m, 1.5m) | ~6:10 | FAILED on attempt 3 timeout |
| R-0001 #3 | 3 (74m, 7m, 1.5m) | ~2:01 | BLOCKED (retry budget) |
| R-0001 #4 | in flight as of doc time | tbd | tbd |

Mode wall-clock is ~2-3 hours per multi-interface R-* node. Fixture cycles are ~4 minutes. The doer is the dominant cost. `just ci` is ~5 min per checker pass.

## Decisions and tradeoffs

- **block-on-merge over stacking** for v0.1. Stacking (Phase C in v2) is the biggest parallelism unlock but the most complex. Block-on-merge is conservative but unbreakable.
- **single-orchestrator-per-workspace.** No clustering, no resume from POLLING_CI on restart. SQLite is the source of truth; on `quikode run` we kill all `qk-*` containers and either restart from the last state or `--retry-failed` resets BLOCKED tasks.
- **opencode glm-5.1 as doer** even when convergence is shaky. User explicitly wants to balance subscription usage across the three providers. The mitigation is structural (subtask breakdown), not model substitution.
- **Bundled prompts + per-workspace overrides.** `prompts/*.md` in the package is the default; if `<workspace>/prompts/` exists it takes priority via `ChoiceLoader`. Lets you tune per-DAG without forking quikode.
- **Hard-coded `triage_budget_per_phase = 3` was wrong** for tasks where the doer needs more context-rebuild attempts. v2 splits this into per-subtask budgets at finer granularity.

## Open issues / known sharp edges

- **No resume from POLLING_CI on quikode restart.** A long PR-poll task is currently abandoned if the orchestrator restarts. Worker re-starts from PENDING which loses the existing PR. Acceptable for now since polling tasks are short-lived; will be addressed in v2.
- **Disk usage grows.** sccache caps at 20GB by config but doesn't auto-trim. Worktrees from non-MERGED tasks pile up. `quikode prune` is manual. v2 should make this an automatic part of orchestrator idle.
- **No web frontend BDD driver in stable form.** Run #2 used a substituted handler-direct call; run #3+ used real headless chrome attempts but the harness is brittle. F-0002 is the right place to set the canonical pattern.
- **Stale state_log entries don't get cleaned up.** Per-task state log is unbounded. After many runs of the same task, the log can have hundreds of entries. Not a problem yet; would need a retention policy if quikode runs the full DAG.

## v3 lessons (2026-05-03)

The 3-run E2E that drove the stacked-diffs comprehensive fix
surfaced bugs that became long-term automatic fixes. See
`design-stacked-diffs-fix.md` for the full design; the items below are
the load-bearing learnings.

### `git rebase --continue` needs `core.editor=true` in containers

git wants an editor for the resolved commit's message; containers have
no TTY/EDITOR. Without override, `--continue` fails with `Terminal is
dumb, but EDITOR unset`. Fix: `git -c core.editor=true rebase
--continue` — `worker.py:_resolve_one_conflict_step`. Used everywhere
the worker continues a rebase, not just the resolver path.

### Multi-conflict rebases need an iteration loop

A 4-commit rebase can hit conflict on commit 1, then on commit 2 after
`--continue`. Original code returned after the first `--continue`. The
resolver now loops on `_rebase_in_progress()` until the rebase
completes or a per-task iteration cap fires —
`worker.py:_spawn_conflict_resolver`. Test:
`tests/test_resolver_loop.py`.

### `git rebase --onto <parent_sha>` for stacked children

When a parent's branch is squash-merged to main, the parent's
individual commits are folded into one squash. A child stacked off the
parent's branch has those individual commits in its history. Plain
`git rebase origin/main` re-applies them onto main → duplicate-commit
conflicts on every line both touched. Fix: capture
`parent_sha = git rev-parse <parent_branch>` (local ref persists
post-deletion), then `git rebase --onto origin/main <parent_sha>` —
`worker.py:run_rebase_to_main` and `_rebase_to_base_branch`. Test:
`tests/test_rebase_onto.py`.

### GitHub auto-closes child PR on parent's `--delete-branch` merge

When the parent merges with `--delete-branch` (tanren default), GitHub
auto-closes the child PR pointing at that branch. `gh pr edit --base
main` is not enough — the PR is already closed. Fix: the rebase worker
detects the closed PR and **creates a fresh PR** pointing at main,
updating `tasks.pr_number` / `tasks.pr_url`. See
`worker.py:run_rebase_to_main`. Test: `tests/test_pr_recreation.py`.

### Worker checkpoints for mid-flight parent-merge

The orchestrator's `_schedule_rebases_for_merged_parent` couldn't
rebase children that were already mid-doer/checker — those workers
held the worktree. Fix: split into two paths. Non-active children get
a separate `_run_rebase_to_main_one` future. Active children get a
flag `tasks.needs_parent_rebase=1`; the worker checks it at safe
checkpoints (5 sites: per-subtask in `_subtask_loop`, entry to
`_final_check_loop`, `_commit_push`, `_open_pr`, each iteration of
`_poll_pr_loop`) and runs the rebase + retarget inline before
continuing. Mirrors the `needs_intent_review` plumbing. Tests:
`tests/test_worker_checkpoint_parent_merge.py`.

### Orphan task recovery on daemon restart

Daemon SIGTERM (or supervisor restart) stops the worker mid-step. Task
remains in DOING_SUBTASK / CHECKING / COMMITTING / etc. without an
active worker. `_pick_next` only picks PENDING — so orphans sit
forever. Fix: `Store.recover_orphan_tasks()` runs on every `quikode
run` startup, before the orchestrator constructor. State-specific
recovery table maps each active state to PENDING (with
`resume_from_existing_subtasks=1` for partial-progress states) or
AWAITING_MERGE (for PR-already-open states). Test:
`tests/test_orphan_recovery.py`.

### Smart rebase scheduling avoids storms

`_schedule_rebases_for_merged_parent` only triggers a child rebase
when the child's PR is `CONFLICTING` or its base branch is deleted —
not on every parent state change. Without this gate, every parent
status update would trigger redundant rebases on every child. See
`orchestrator.py:_schedule_rebases_for_merged_parent`. Test:
`tests/test_rebase_scheduling.py`.

### Per-subtask commit + push surfaces hooks early

Pre-commit hooks (`lefthook.yml` for tanren) fire per slice instead of
accumulating to end-of-task. A formatting violation in S-02 surfaces
in S-02's triage cycle, not 2+ hours later when 8 subtasks worth of
diff hits one final commit. Tracked via `subtasks.pre_commit_failures`
distinctly from real verdict FAILs. See
`design-per-subtask-commit.md` and `worker.py:_subtask_loop`.

### Progress-check agent prevents budget burn on stuck subtasks

A struggling subtask can burn the full
`subtask_hard_max_attempts=30` budget on the same root cause. The
progress-check agent (claude-haiku) runs every
`subtask_progress_check_every` attempts after
`subtask_progress_check_after`, judges
PROGRESSING/FLATLINED/TOO_EARLY, and BLOCKs the subtask after
`subtask_flatline_block_count` (default 2) consecutive flatlines.
Audit log in `progress_checks` table. See `quikode/prompts/progress.md`
and `worker.py` progress-check call sites.

### Daemon supervisor with backoff vs. tight crash loops

If `quikode run` crashes immediately on every spawn (e.g. config
parse error), the supervisor would tight-loop without a backoff.
Schedule is `[60, 300, 1800]` (cap), and resets to entry 0 only if the
child ran ≥ `daemon_min_run_for_backoff_reset_s=300s` before crashing.
Crashes within 5 min of spawn keep climbing the schedule. Test:
`tests/test_daemon_supervisor.py`.
