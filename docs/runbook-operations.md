# Operations Runbook

## Start

```bash
quikode doctor
quikode seed-from-base
quikode daemon start --detach --max-parallel <N>
quikode daemon status
```

Use `quikode run` for foreground debugging.

## Monitor

```bash
quikode status
quikode briefing
quikode show <task-id>
quikode subtasks <task-id>
quikode tail <task-id>
quikode monitor
```

For long-running watch loops, use `quikode monitor` — it polls the SQLite `state_log` directly (read-only) and emits one stdout line per high-signal transition. Avoid `tail -F daemon.log | grep` (flaky on log rotation, ANSI escapes, pipe buffering — the WSL filesystem in particular drops the file handle on daemon restart). See `orientation.md` §6.

Useful overnight tmux hooks:

```bash
tmux new-session -d -s qk-state-monitor \
  'cd /path/to/workspace && quikode monitor --keywords "attempt 4,attempt 5,no such column,parse_failure,planner_validator,container_vanished,doer_output_invalid,stream disconnected,rate_limit,failed,blocked"'
tmux new-session -d -s qk-health-loop \
  'cd /path/to/workspace && while true; do date; quikode daemon status; quikode briefing; sleep 300; done'
```

## Provider routing

`GLM-5.1-zai` is the preferred write-heavy profile when the local LiteLLM
proxy is healthy. The model registry gives it an automatic quota fallback to
`GLM-5.1-wafer`, then direct `gpt-5.3-codex`. Plan 59 reframed quota handling:
the chain is fast-fail at the transport layer (no in-transport sleep — plan
19A's design is RETIRED), so a Z.ai 429 cascades through Wafer to direct
Codex in seconds, not minutes. If ALL THREE return quota, the worker layer
sleeps `cfg.transient_retry_delays_s["quota_exhausted"]` (default 600s)
before the next full chain re-attempt. Tune that key in `.quikode/config.toml`
if 10 min is too aggressive or too patient for the operator's preferred
re-attempt cadence; see `runbook-incident-response.md` § "Plan 59
transient_retry_delays_s tuning".

Plan 47 retired the doer envelope: doer transports run plain apply-patch
(no `--output-schema` / `--json-schema` flag). The diff is the sole evidence;
no envelope-shape repair failures. Read-only roles (planner / checker /
triage / fixup-planner / merge-planner / progress / audit) still run in
JSON output mode; on proxy-routed transports the client-side layer accepts a
schema-valid JSON object even when provider prose or markdown surrounds it.

Watch for write-role transport failures that are not quota failures:

- `quikode show <task-id>` shows repeated `,layer=transport` retries (plan 48 / 51 — the structured retry signature; the legacy `doer_output_invalid` shape is retired).
- The doer produced an empty diff. Plan 51's transport stop-loss BLOCKs after `cfg.subtask_transport_stop_loss_count` (default 3) consecutive empty diffs with an operator-clear message; a new `subtask_empty_diff:<id>` artifact appears in `qk show`.
- Raw task logs include `stream disconnected before completion: error sending request for url (http://host.docker.internal:4000/v1/responses)`.
- In a corrected host probe (`127.0.0.1` base URL), Wafer returned a shell
  command in a code block instead of issuing a tool call, created no file.
  (Schema enforcement no longer applies to doer transports per plan 47 — the
  diff is the evidence; an empty diff is the signal.)

For host-side manual proxy probes on Linux, use `127.0.0.1:4000`. The
`host.docker.internal:4000` provider URL is for task containers, where quikode
adds the Docker host-gateway mapping. The LiteLLM bridge must publish both
`127.0.0.1:4000` and the Docker host-gateway address, usually
`172.17.0.1:4000`; publishing only `127.0.0.1` makes host probes pass while
containerized Codex calls fail.

Immediate mitigation for non-quota provider breakage:

```bash
quikode daemon stop
$EDITOR .quikode/config.toml   # temporarily set subtask_doer_model/conflict_resolver_model to "gpt-5.3-codex"
quikode reset-retries <task-id> <subtask-id>
quikode resume <task-id> --reason 'switch write roles to direct codex after LiteLLM transport failures'
quikode daemon start --detach --max-parallel <N>
```

If many tasks are affected, switch the config first, restart once, then
reset/resume the blocked tasks.

## Post-PR state meanings

Plan 28 streamlined the post-PR slice; plan 58 flattened it further. Post-cutover:

- **`pending_ci`** — PR open, CI running. Daemon polls only `gh pr view` for CI rollup.
- **`awaiting_review`** — CI is green; daemon polls formal GitHub Reviews. The "needs human" state.
- (Plan 58: `addressing_feedback` is REMOVED.) A detected CI failure or non-bot `CHANGES_REQUESTED` review now fires `CI_FIXUP_START` or `REVIEW_FIXUP_START`, transitioning the task to `AUDIT_LOCAL_CI` and entering the unified `_run_audit_cycle` driver. The 5 audit-stage states (`AUDIT_LOCAL_CI` / `AUDIT_RUBRIC` / `AUDIT_STANDARDS` / `AUDIT_ARCHITECTURE` / `AUDIT_BEHAVIOR`) walk in order; on any stage failure the task enters `FIXUP_PLANNING` → `DOING_SUBTASK` etc. and re-enters `AUDIT_LOCAL_CI` after fixup commits land.

Plan 58 also adds a lifecycle phase/cycle layer on top of the state. Every task carries `phase ∈ {INITIAL, PRE_PR_REVIEW, PR_REVIEW}` + `cycle_in_phase: int` + `pr_review_trigger ∈ {NONE, CI_FAILURE, REVIEW_FEEDBACK}`. The TUI / state-log / `qk briefing` render `phase · cycle X · state` so the operator sees both "where in the lifecycle is this task" and "what state is it in right now." The `release_valve_*` config keys (renamed from `pre_pr_release_valve_*`) apply per-phase: PRE_PR_REVIEW cycle 5 → open PR with deferred findings; PR_REVIEW cycle 5 per trigger → push + let GitHub + reviewer decide.

Plan 30's derived signal on `awaiting_review`:

- **review-ready-settled** — task has been in `awaiting_review` for ≥ `cfg.review_ready_settle_s` (default 900s = 15 min). Triggers two things: (1) ntfy push to `cfg.notify_ntfy_topic`; (2) stacked-diff dependents whose only un-met dep is this task become eligible to start.

(Retired by plan 28: `merge_ready`, `triaging_feedback`. Retired by plan 58: `pre_pr_auditing`, `addressing_feedback`. Bot/AI-reviewer line comments are not polling triggers — they bundle as context for the fixup planner when a real review fires.)

Plan 56: if a PR ends up CLOSED-not-merged on GitHub but every commit is reachable from `origin/<base>` (the release-batch flow), the daemon auto-transitions the task to MERGED via ancestry check. `qk detect-merged` is the operator-facing dry-run / `--apply` sweep for retroactive catch-up.

## Stacking-gate behavior at startup

With `cfg.stacking_strategy = "aggressive"` and `cfg.stacking_readiness = "settled"`, expect a **ramp** rather than instant slot saturation:

- The first wave of in-flight tasks is whatever subset of the DAG has all-deps-merged (the "primary tier"). On a fresh seed this is the depth-1 children of seed-merged nodes.
- All other tasks are stacked candidates, gated on their parent reaching `awaiting_review` for ≥ 15 min. Until the first parent settles, the scheduler cannot dispatch them — slots will sit idle.
- Once the first wave settles, ~3–5 dependents per parent become eligible and the funnel widens. Steady state quickly approaches `max_parallel`.

This is by design. Plan 30's safety property: every stacked child starts from a CI-green base the operator could have reviewed. The cost is one cold-start cycle of partial slot fill.

If you've **just tightened the stacking gate** (e.g. flipped `stacking_readiness` from `"speculative"` to `"settled"`, or bumped `stacking_strategy`), see `runbook-incident-response.md` § "Fruit-of-rotten-tree wipe" — pre-tightening worktrees were forked under the looser gate and likely need cleaning.

## Interventions

`resume <id>` — drops a BLOCKED/FAILED task back to PENDING with a resume marker; preserves worktree, branch, subtask rows.

`reset-retries <id> [<subtask>]` — zeroes retry counters on BLOCKED subtasks without discarding committed work. Pair with `resume`.

`rewind <id> <subtask>` — surgical: rewinds branch + worktree to predecessor's commit; resets target + every topo-after subtask to PENDING; preserves prior subtasks' commits. Requires BLOCKED/FAILED. `--dry-run` first.

`replan-cycle <id>` — plan 52 cycle-scoped recovery: deletes only the most-recent planning cycle's subtasks, force-pushes the branch back to before that cycle's first commit, and sets a marker so the worker re-fires the matching planner phase (fixup / replan / merge) on the next scheduling tick. Earlier cycles' commits + retry counters survive. Requires BLOCKED/FAILED with at least one non-initial cycle (cycle ≥ 2). `--dry-run` first. **Default escalation when `rewind` didn't unstick the loop and the failing subtask is in a non-initial cycle (`F-…`, `F-CI-…`, replan output, merge-integration).**

`retry <id>` — last resort: wipes worktree + branch + subtask rows; planner re-plans from scratch. Requires task in BLOCKED/FAILED/ABORTED. For PENDING tasks, `abort` first. Reserve for tasks where even the initial planner output was wrong-shape; otherwise prefer `replan-cycle`, which preserves earlier-cycle work.

`abort <id>` — marks a task ABORTED and tears down its container.

`unblock <id>` — prints forensics + local context for a blocked task.

`mark-merged <id>` — marks already-landed upstream work as merged.

`detect-merged` — plan 56 sweep. Dry-run by default; prints which tasks' commits are reachable from `origin/<base>` (i.e. were absorbed into main via release-batch push even though the PR was closed-not-merged on GitHub). `--apply` auto-merges every such task via the same FSM call `qk mark-merged` uses. Idempotent; safe to run at any time, even with the daemon up.

For decision rules on which intervention to use, see `orientation.md` §3 (Resolving blockers — the intervention decision framework). Escalation order: resume / reset-retries → rewind → replan-cycle → retry. Retry is genuinely reserved for tasks where even the initial planner output was wrong-shape; for cycle-level non-convergence in fixup / replan / merge cycles, `replan-cycle` preserves every earlier-cycle commit.

## ntfy review-ready notifications

Set `notify_ntfy_topic` in `.quikode/config.toml` to a secret topic string and install the ntfy.sh app on your phone (subscribe to the same topic). When any task reaches review-ready-settled, you'll get a push: title `"R-NNNN: ready for review"`, body with the task title + settled minutes + review round count + PR URL, click → PR.

Test the wiring by waiting for the first task to settle, or — for synthetic verification — flip `cfg.review_ready_settle_s` to 0 in config (the next poll tick will fire ntfy on every AWAITING_REVIEW task it sees), then revert.

## Overnight Checklist

Run the validation ladder, initialize a fresh workspace, run `seed-from-base`,
confirm already-landed nodes are `merged`, then start the daemon with the
intended `--max-parallel`. Verify ntfy is wired (config has
`notify_ntfy_topic`, app subscribed). Confirm write-heavy roles are on direct
Codex before leaving the daemon unattended. `qk daemon start` fail-fast
validates launch-critical config: repo/DAG paths, non-empty local CI, loaded
standards profiles, and loaded architecture docs. For tanren, verify
`standards_profiles_dir = "<repo>/profiles"`,
`standards_profiles = ["rust-cargo"]`, and
`architecture_docs_dir = "<repo>/docs/architecture"` before starting. Existing
tasks may have persisted `evaluation_contract.json` files from an earlier bad
config; current workers refresh stale empty audit corpora from launch config
before pre-PR audit instead of letting them fail at runtime. Also
ensure the Playwright cache configured by `playwright_cache_dir` has Chromium
installed and that the quikode dev image has been rebuilt with Playwright's
Chromium OS dependencies. Missing browser binaries and missing shared libraries
are both environment failures; a warmed Z-99 container can mask either one.
Z-99 is the system-injected holistic stabilization subtask; its objective
gate must run `local_ci_command` (for tanren, `just ci`) rather than the
lighter `subtask_check_command`, and a later pre-PR local-CI failure means the
Z-99 pass was not authoritative.
If a behavior-proof subtask repeatedly fails with `classification=NO_COMMAND`,
check whether the DAG expected-evidence row lacks a runnable command. Current
workers recover by executing a matching command from the validated doer envelope
as a last resort; older workers require either a DAG `witness_command` patch or
manual retry after upgrade.
When pre-PR audits fail, confirm the fixup planner maps architecture findings
through `architecture_referenced` rather than treating them as standards refs or
as uncovered findings.
Start the tmux monitor hooks above and
check `quikode briefing` once after the first 10-15 minutes. Expect a slot-fill
ramp over the first ~30 min as the first wave of primaries reaches
review-ready-settled.
