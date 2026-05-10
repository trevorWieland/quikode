# Plan 55 — fresh container per pre-PR audit cycle + agent self-clean primitive

## Why

Today's R-0040 / F-CI-1 incident exposed a class of failures the
reactive contract can't solve cleanly: GitHub CI fails on a fresh
runner, doer's local container has accumulated state that masks the
drift, doer correctly reports "no diff needed, all gates pass" —
but the diff that would actually unstick GitHub never gets produced.

Plan 53 ships the reactive signal (`cannot_reproduce` failure layer
at K=2) so the operator gets notified fast. But the failure mode
still happens. To preempt the class entirely, **the local container's
state at audit time must mirror the fresh-runner environment GitHub
uses.**

Equally important: the user's specific guidance — "the top concern is
being able to replicate the error locally. If agents can't, they
can't solve it without guessing blindly." A fresh container per
audit cycle is a STARTING state; agents (esp. the doer working a
fixup_ci subtask) must also be able to RESET their environment
mid-investigation if they need to re-probe whether a failure
reproduces.

## What ships

### Part 1: fresh container provisioning per pre-PR audit cycle

The pre-PR audit cycle is the canonical "should match GitHub" moment.
Before entering `PRE_PR_AUDITING` for a given audit cycle:

- Discard the task's current dev container (if any). `docker rm -f`.
- Re-provision a fresh container from `cfg.image_tag` mounting the
  task's worktree at `/workspace`. This is the same provisioning
  pipeline used at task start (`PROVISIONING` state).
- Wait for `wait_dev_ready` (or equivalent). Don't proceed until
  the container is healthy.
- Run a project-configurable **bootstrap command** inside the fresh
  container before the audit gauntlet starts: new config field
  `audit_bootstrap_command` (default empty). For tanren this might
  be `pnpm install --frozen-lockfile && just regenerate-all` or
  whatever the project deems "clean state." Project decides; quikode
  doesn't prescribe what it means.
- If the bootstrap command produces a worktree diff, auto-commit +
  push it as `audit-bootstrap: <one-line summary>` before continuing.
  This is the proactive drift-preempt: regenerated artifacts ship to
  the PR branch before the audit gauntlet sees them.
- The audit gauntlet then runs against the fresh state.

Subsequent fixup-cycle subtasks within the same audit cycle reuse
this fresh container (no re-provision per fixup subtask). The audit
cycle's container is discarded again at the start of the NEXT pre-PR
audit cycle, or when the task transitions to AWAITING_REVIEW /
terminal state.

**Cost trade-off acknowledgement:** fresh provisioning costs ~5-10
min of overhead per audit cycle. Typical tasks have 1-3 audit cycles
before settling, so 5-30 min of provisioning cost per task. This is
amortized across the cycle's audit + fixup work (which runs for
20-60 min typically), so the relative overhead is 10-30%. Net
positive vs. the 30+ min cost of an env-drift crash loop that plan
53 catches reactively.

### Part 2: agent self-clean primitive

The doer working a `fixup_ci` subtask (per plan 53's reproduce-before-
fix rule) needs the ability to reset its working environment if it
suspects a failure isn't reproducing because of stale local state.

Two pieces:

**Doer prompt update (`prompts/subtask-doer.md`):**
- The §6a fixup_ci section (added by plan 53) gains a paragraph:
  "If you have run the failing CI recipe locally and it does not
  reproduce the GitHub failure, suspect environmental drift. You can
  re-run the project's clean-state bootstrap command inside the
  container before re-running the CI recipe — this mirrors a fresh
  GitHub runner. The bootstrap command is configured at
  `cfg.audit_bootstrap_command`. The orchestrator already ran it at
  audit-cycle start, but caches can drift from your own intermediate
  edits; rerunning is safe."

**Worker / container-exec helper:**
- No new tool — the doer already has shell access via codex CLI's
  `exec_command` tool. The doer just runs the bootstrap command
  itself when it decides to. quikode doesn't need a special tool
  invocation; the project's command is a normal shell line.

The "agent self-clean" capability is therefore just (1) making the
clean command discoverable/named via config, and (2) prompting the
doer to use it when appropriate.

### Part 3: configuration

`quikode/config.py`:
- New field `audit_bootstrap_command: str = Field(default="")`.
  Empty string disables the bootstrap step (back-compat — existing
  workspaces without this set get the current behavior).
- New field `audit_fresh_container: bool = Field(default=False)`.
  Off by default so existing workspaces don't get the provisioning
  overhead unless they opt in. Tanren's workspace config will set
  it `true` after this lands.

`quikode/config_loader.py`:
- Plumb both new fields. Plan 50's audit warns will catch you if
  you forget.

`quikode/config_descriptions.py`:
- Brief descriptions for both fields.

### Part 4: worker wire-up

`quikode/workers/pre_pr.py`:
- Find the state-entry to `PRE_PR_AUDITING` (likely
  `enter_pre_pr_auditing` calls). Before that transition, if
  `cfg.audit_fresh_container == True`:
  1. Discard the current dev container via `docker_env`
     helper.
  2. Re-provision via the existing provisioning code path. Reuse
     `docker_env.ensure_dev_container_running` or the bootstrap
     flow.
  3. If `cfg.audit_bootstrap_command` is non-empty, run it inside
     the container. Use `_tw.exec_in` with a generous timeout
     (~15 min) — bootstrap can be heavy.
  4. If the worktree diff is non-empty after bootstrap, commit +
     push it (orchestrator handles the commit; doer not involved).
     Use the existing per-subtask commit/push helpers — or extract
     a thin "auto-commit if dirty" helper. Commit message:
     `audit-bootstrap: <kind>` where `<kind>` is the audit cycle
     number / kind.
  5. Proceed into `enter_pre_pr_auditing` and the gauntlet.

The PROVISIONING state at task start already runs the dev container
bootstrap; this just adds a similar bootstrap at each audit cycle
entry. Don't duplicate logic if you can avoid it — extract the
shared bootstrap helper into `workers/dev_container_bootstrap.py`
or similar if cleanliness demands.

### Tests

- `tests/test_workers_pre_pr_fresh_container.py` (new):
  - When `audit_fresh_container=False`: pre-PR audit runs against
    the existing container; no re-provision; no bootstrap call.
  - When `audit_fresh_container=True` + `audit_bootstrap_command=""`:
    re-provisions container but skips bootstrap step. Audit proceeds.
  - When both set: re-provisions + runs bootstrap. If bootstrap
    produces no diff, audit proceeds normally. If bootstrap produces
    a diff, the diff is auto-committed + pushed with
    `audit-bootstrap:` prefix, THEN audit runs.
  - Bootstrap failure (rc != 0): synthesizes a clear failure (not a
    spurious audit failure — the task should fail-fast with a
    "bootstrap-failed" reason, distinct from audit failures).
- `tests/test_prompts.py` (or sibling):
  - `subtask-doer.md` rendered for a `fixup_ci` subtask includes
    the new agent-self-clean paragraph mentioning
    `cfg.audit_bootstrap_command`.
- `tests/test_config_loader.py` (or sibling):
  - Both new fields override correctly from TOML.

### Plans index + orientation

- Add plan 55 row to `plans/00-INDEX.md`.
- `orientation.md` §7 invariants: new bullet describing the
  audit-cycle fresh-container contract and the project's
  bootstrap-command obligation.
- `orientation.md` §5.5 (operator-mediated worktree fixes): note
  that the project's bootstrap command should produce a clean state
  end-to-end — it's the canonical "match what GitHub does" step.

## Operational followup (manager handles)

After the agent ships:
1. Validation ladder green.
2. Commit + push.
3. Reinstall + daemon restart (cfg loaded fresh).
4. Set `audit_fresh_container = true` + `audit_bootstrap_command =
   "pnpm install --frozen-lockfile && just regenerate-all"` (or
   whatever's right) in the tanren workspace config.
5. Next pre-PR audit cycle for any task picks up the fresh-container
   semantics. Watch the first ~5 cycles for behavior — overhead,
   bootstrap-failure cases, false drift commits.
6. Plan 53's `cannot_reproduce` signal should drop to near zero
   under the new contract; if it persists, the bootstrap command is
   incomplete and needs project-side iteration.

## Out of scope

- Per-subtask fresh container (heavier, only worth doing if audit-
  cycle granularity isn't enough).
- A separate orchestrator-side "agent-invoked clean" tool (the doer
  just runs the configured bootstrap command via its existing shell
  access — no new mechanism).
- Plan 56 candidate: generalize plan 49's FSM-state guard pattern
  across all remaining unguarded `enter_*` call sites in
  worker/watcher paths.
- Plan 57 candidate: generic fallback-chain config for ANY agent
  role (not just doer-specific), with intelligent in-flight
  recovery when an agent's primary transport persistently fails.
