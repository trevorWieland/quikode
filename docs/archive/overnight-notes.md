# overnight notes — 2026-05-02

**TL;DR:** quikode v0.1 is functional end-to-end. R-0001 is the first real tanren-DAG task; loop is in flight. Several non-trivial fixes were needed to get codex's checker working inside docker. Lots of new tooling went in. v2 design doc drafted at `docs/design-v2.md`.

## R-0001 status

The first attempted run had a working doer but a **broken checker** — codex's bwrap sandbox can't create user namespaces inside an unprivileged docker container, and codex was silently falling back to GitHub-API file fetches that 404'd against the unpushed branch. This produced bogus FAIL verdicts and a "task didn't progress" appearance even though the doer had written 1200+ lines spanning all 5 interfaces and a BDD feature file.

After the fix (`--dangerously-bypass-approvals-and-sandbox`), the checker now:
- runs `just ci` and reads its output
- makes real HTTP calls (`POST /v1/accounts` returned 200, duplicate returned 409 with the right error code, etc.)
- runs the CLI via `assert_cmd`-style invocation, checks session-file mode 0600
- invokes MCP tools and verifies structured content
- catches real misalignment (e.g., the doer's first attempt named a route `/v1/sessions/current` instead of `/v1/accounts/me`, missed `@cli`/`@mcp`/`@tui` BDD tags, didn't auto-navigate web after success, leaked plaintext credentials into scenario text)

The triage agent (claude-opus) digested the checker's failures and produced precise, actionable fixes. Doer attempt 2 is in flight as of 01:42, applying those fixes.

If R-0001 reaches AWAITING_HUMAN, the PR will be ready for review at `https://github.com/trevorWieland/tanren/pull/<n>`.

## What changed in quikode tonight

### Critical (fixed live, would have blocked tanren)

1. **codex `--dangerously-bypass-approvals-and-sandbox`** — bwrap inside docker is a no-go. Without this flag the checker is blind. ([`quikode/agents/codex.py`](quikode/agents/codex.py))
2. **Unique branch suffixes per run** (`quikode/<task-id>-<6hex>`). Eliminates remote-branch collisions when a prior failed/aborted push left a branch we can't delete. ([`quikode/worktree.py`](quikode/worktree.py))
3. **`tuple | set` TypeError fix** in orchestrator's `_all_done` — was crashing on Python 3.12.
4. **`AWAITING_HUMAN` short-circuit** when doer makes no diff (handles tasks that are already complete in main).
5. **Doer timeout bumped 7200s → 14400s** — opencode glm-5.1 routinely takes >2h on multi-interface tasks.

### New CLI commands

| Command | Purpose |
|---|---|
| `briefing` | One-shot snapshot: in-flight, awaiting, blocked, recent transitions, agent cost, dag progress, disk usage, warnings. **Run this first when you wake up.** |
| `dag-stats [--by milestone\|layer]` | Per-group breakdown of merged/awaiting/active/blocked/pending |
| `export <id> [-o file.md]` | Bundle planner output, doer summary, checker verdict, triage notes, full git diff into one markdown for human review |
| `prune [--sccache-max-gb N]` | Trim sccache + remove worktrees of terminal-state tasks |
| `disk-usage` | What quikode is using on disk |
| `dev-test` | One-shot fixture validation (resets + runs + asserts AWAITING_HUMAN within timeout) |
| `mark-merged <id ...>` | Manually mark already-complete tasks as MERGED (used to bootstrap R-0001 from F-0001) |

Existing `watch` and `show` got beefed up: watch now shows in-state-elapsed and worktree-mtime with color thresholds; show now prints the state timeline and per-call agent cost.

### Internal

- Token tracking + duration captured into a new `agent_calls` SQLite table. Codex's `tokens used\nN` is parseable; claude/opencode in text mode don't expose tokens reliably (would need `--output-format json` parsing — deferred).
- Stalled-task heartbeat in the orchestrator (warns when DOING task's worktree mtime falls behind threshold).
- 32-test pytest suite covering DAG, state, prompts, agents, token parsing.

## What's in `docs/design-v2.md`

Design for the next major version, addressing three parallel-execution hazards:

1. **Phase A — smart conflict resolution.** Today: parallel disjoint tasks fail to merge cleanly when they touch the same files. Proposal: detect `mergeable=CONFLICTING`, attempt rebase, on conflict spawn a "conflict resolver" agent with the task plan, the conflicting commits, and the conflicted file markers. Verify with the existing checker. New states `REBASING`, `CONFLICT_RESOLVING`.

2. **Phase B — intent-gap detection on dep merges.** Today: B's plan is written against an older main; A merges; B has no merge conflict but its work is silently misaligned with the new world (e.g., A added a new instance of a pattern B was supposed to apply universally). Proposal: after every `MERGED` transition, queue intent reviews for in-flight tasks. New "intent reviewer" agent diffs main since B's `base_ref_sha` and emits NO_DRIFT / MINOR_DRIFT / INTENT_CONFLICT. Per-verdict actions: continue / rebase+recheck / replan.

3. **Phase C — stacked diffs.** Today: B blocks until A merges. Proposal: B branches off A's branch optimistically; rebases on parent merge or force-push. New `parent_branch` field on tasks. Cascade rebases happen topologically.

Recommended rollout order: A → B → C. A and B together capture most of the parallelism win without the complexity of stacking. Phase C deferred until the simpler model is proven.

## Files added/changed

- `quikode/agents/codex.py` — added `--dangerously-bypass-approvals-and-sandbox`
- `quikode/agents/base.py` — `parse_tokens`, `AgentResult.tokens_used`/`duration_s`
- `quikode/worktree.py` — `branch_for(unique_suffix=True)`
- `quikode/state.py` — `agent_calls` table, `record_agent_call`
- `quikode/orchestrator.py` — stall heartbeat, `_check_stalls`
- `quikode/worker.py` — agent_call recording, doer timeout 14400s, no-diff short-circuit
- `quikode/cli.py` — `briefing`, `dag-stats`, `export`, `prune`, `disk-usage`, `dev-test`, `mark-merged`, `reset`, beefed-up `watch`/`show`
- `quikode/config.py` — `claude_json_path`, `stall_warn_seconds`
- `tests/` — full pytest suite (32 tests)
- `docs/design-v2.md` — v2 design
- `docs/overnight-notes.md` — this file
