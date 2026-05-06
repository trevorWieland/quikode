# Plans index — overnight tanren run, May 2026

Plans written while the long-haul daemon does its thing. Each is sized to be a single
PR's worth of work; sequencing notes call out dependencies. Order = priority.

| # | Title | Why it matters | Depends on |
|---|---|---|---|
| 01 | retry-landscape | Landscape map. Read first. | — |
| 02 | network-rate-limit-backoff | gh/git polls have no backoff; one 429 → infinite retries. **Shipped** as `quikode/net_retry.py`; wired into github.py + github_graphql.py. | — |
| 03 | conflict-cap-and-force-push | Hard-coded 6 ignores config; force-push has no retry. | 02 |
| 04 | supervision-stall-event | Stall reset uses wrong FSM event, loses evidence. | — |
| 05 | poisoned-worktree-wipe | "Wipe rather than carry forward" — the user's explicit ask. | 06 |
| 06 | progress-check-signal-quality | Deterministic locality fingerprint, not just agent verdict. | — |
| 07 | observability-overnight | Heartbeat-watch + cluster detection in briefing. | — |
| 08 | budget-tightening | Cut hard_max from 50 to 20 once 05+06 are in place. | 05, 06 |
| 09 | prompt-level-leverage | Doer/triage/planner edits to reduce retry rate. | — |
| 10 | resume-correctness | Orphan recovery edge cases: worktree, planner staleness. | — |
| 11 | stream-agent-output-to-log | `qk tail` is currently silent for 10+ min until the agent exits. Tee `Popen` to log. | — |
| 12 | no-ci-leak-invariant | Prompt-level fix for R-0005 BLOCKED. Bakes "branch owns every commit" / "no pre-existing failure" into doer/triage/checker/planner/progress. Shipped. | — |
| 13 | scope-review-gate-fix-rule | Follow-up to 12. Scope reviewer was nuking the cross-file fixes the new doer/triage prompts authorize, leaving the system in a churn loop. Added "gate-keeping cross-file fixes are ALWAYS legitimate" to `scope-review.md`. Shipped. | 12 |
| 14 | checker-must-not-fabricate | Walked back plan 12's "synthetic FAIL bullet" guidance for the subtask checker. It was inventing criteria the doer couldn't satisfy (e.g. R-0021/S-08 spent 11 attempts because the checker fabricated a "surface startup" criterion the BDD scenario already mocked via in-process harnesses by tanren design). New rule: fail on observed gate failures only; never fabricate. Shipped. | 12 |
| 15 | qk-show-markup-safety | `qk show` crashed with `MarkupError` whenever an artifact contained bracketed paths like `[/workspace/...]` (rustc, BDD, checker output). Operator/agent couldn't read triage at all. Fix: print artifact bodies with `markup=False, highlight=False`. Shipped. | — |
| 16 | dev-container-readiness-timeout | 16-way parallel boot of dev containers exceeded the 60s `wait_dev_ready` ceiling under I/O contention; tasks marked FAILED while their containers actually finished entrypoint a moment later, then held the budget as zombies. Fix: bump call-site to 240s and default to 120s. Shipped. | — |

## Suggested ship order (no priorities crossing each other)

1. **02 + 04 + 07A** (heartbeat watch). Pure additions, no semantics change. Highest
   leverage during overnight runs; lowest risk.
2. **03**. Two-line config wiring + plan-02-dependent retry-with-backoff.
3. **06 + 09 (A, B)**. Detection upgrade + prompt edits land together; one reinstall.
4. **05**. Wipe-and-replan FSM event. Only after 06 ships, since 05 reuses 06's
   locality fingerprint.
5. **08**. Tighten budgets once the safer detectors are in place.
6. **10**. Recovery hardening — surgical, needs care.

## Things explicitly NOT planned

- Multi-host parallelism (the user is single-host).
- Switching agent CLIs around mid-run (existing config supports it, no plan needed).
- DAG editor / planner that proposes new nodes (out of scope for quikode-the-runner).
- Tanren-specific code edits (constraint: state-machine and prompts only).
