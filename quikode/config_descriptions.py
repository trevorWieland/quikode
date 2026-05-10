"""Long `Config` field descriptions kept out of the schema class."""

SUBTASK_DOER_TIMEOUT = (
    "Per-subtask doer agent timeout. Plan 33 calibration (after the "
    "tanren deploy where 7 consecutive opencode/glm-5.1 doer calls "
    "rc=124'd at duration_s ~= 1314s, hitting the prior 1200s "
    "ceiling): bumped to 1800s (30 min). The doer prompt's targeted "
    "rubric / standards / architecture context makes the call "
    "meaningfully heavier than the pre-Plan-33 shape, and smaller "
    "models need the headroom to land both the diff and the "
    "DoerEnvelope JSON before SIGTERM."
)

SUBTASK_CHECKER_TIMEOUT = (
    "Per-subtask checker agent timeout. Plan 33 calibration: bumped "
    "from 600s to 900s alongside the doer bump — the targeted "
    "EvaluationContract (rubric grading template + standards refs) "
    "makes the checker's reasoning surface bigger too, and we want "
    "the proportional headroom so the checker doesn't false-fail "
    "doer work that just barely fit in the new doer ceiling."
)

SUBTASK_SAME_SIGNATURE_BLOCK = (
    "If the last N non-transient retry_reasons share the same "
    "(category, signature) tuple, BLOCK the subtask. Independent of "
    "the progress-check verdict — catches deadlocks where each "
    "attempt produces different-but-equally-invalid output that "
    "the progress-check agent rates 'progressing'. Plan 23."
)

FIXUP_PLANNER_TIMEOUT = (
    "Per-invocation timeout for the fixup planner. Plan 33 "
    "calibration (after the tanren deploy doer-timeout incident): "
    "bumped from 1200s to 1800s. The fixup-planner now renders the "
    "full EvaluationContract (planner-equivalent prompt) in addition "
    "to the audit-bundle decomposition; the planner-equivalent "
    "prompt growth + structured JSON output for multi-finding "
    "decomposition needs the same headroom as the doer."
)

FIXUP_PLANNER_OUTPUT_RETRIES = (
    "Driver-level re-prompts when the fixup planner emits malformed "
    "JSON or a plan that fails runtime validators. Output violations "
    "are retried in-place; BLOCKED is the final escape hatch after "
    "this budget is exhausted."
)

REVIEW_READY_SETTLE = (
    "Seconds in AWAITING_REVIEW before the review-ready-settled signal fires. "
    "Triggers ntfy notification AND unblocks stacked-diff dependents in "
    "stacking_readiness='settled' mode."
)

CONFLICT_RESOLVER_MAX_ITERATIONS = (
    "Max conflict-resolver iterations within ONE rebase attempt before "
    "aborting + BLOCK. Each iteration runs the resolver agent on the "
    "current conflict markers and continues `git rebase --continue`."
)

REBASE_MAX_ATTEMPTS = (
    "Max OUTER rebase attempts (each containing up to "
    "`conflict_resolver_max_iterations` resolver iterations) before "
    "BLOCKING. Plan 31 split this from the resolver-iteration cap; "
    "they were the same knob pre-plan-31, which made the budget gate "
    "ambiguous."
)

SUBTASK_WITNESS_TIMEOUT = (
    "Plan 33 §7.2: per-witness wall-clock cap for the per-subtask "
    "scoped witness runner. Per-subtask total budget is derived as "
    "`2 * len(behavior_evidence_advanced) * subtask_witness_timeout_seconds`. "
    "Default 15s suits unit-shaped witnesses; bump for BDD-heavy suites."
)

PRE_PR_AUDIT_OUTPUT_RETRIES = (
    "Driver-level retries when a pre-PR audit emits malformed JSON "
    "or otherwise fails schema validation. Output violations are "
    "retried in-place before parse_failure is used as the final "
    "escape hatch."
)

PRE_PR_RELEASE_VALVE_AFTER_CYCLES = (
    "Open the PR after this many pre-PR cycles when only configured "
    "quality stages are failing. Set -1 to disable."
)
