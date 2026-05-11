# FSM State-Flattening Audit

Branch: `optimizations` | Files audited: `quikode/fsm.py`, `quikode/fsm_runtime.py`, `quikode/workers/{task_worker,pre_pr,pr_lifecycle,feedback,subtask_execution,subtasks,fixup_coverage,pre_pr_audit_stages}.py`, `orientation.md` §7, `docs/architecture.md` FSM section.

Operator concern: `ADDRESSING_FEEDBACK` (and `PRE_PR_AUDITING`) collapse many distinct internal phases — fixup-planning, per-subtask doer/checker/triage loops, settling — into a single FSM state. The TUI shows only the umbrella label, hiding what is actually happening.

---

## 1. Inventory of top-level FSM states

State list source: `fsm.py:22-50`. Per-state ownership traced through `fsm_runtime.py` helpers and the worker mixins.

| State | What it means | Owner method | Sub-phases / agents | Entry events | Exit events |
|---|---|---|---|---|---|
| `PENDING` | Queued; not yet picked by a slot. | scheduler | none | initial; `RETRY_TASK`/`RESUME_TASK`/`PARENT_ADVANCED` | `START_TASK`, `MARK_MERGED`, `ABORT` |
| `PROVISIONING` | Worktree + container being stood up. | `TaskWorker._provision` (`task_worker.py:219`) | `worktree.add_worktree*` + execution-backend `provision` | `START_TASK` | `ENVIRONMENT_READY`, `CRASH`, `BLOCK_TASK` |
| `PLANNING` | Spec planner agent running. | `SubtaskWorkerMixin._plan` (`subtasks.py:57`) | `planner` agent → validators → `upsert_subtasks` | `ENVIRONMENT_READY` | `PLAN_VALID`, `CRASH`, `BLOCK_TASK` |
| `DOING_SUBTASK` | One subtask's doer agent is running. | `SubtaskExecutionMixin._do_subtask` (`subtask_execution.py:102`) | `subtask_doer` (writes-files) → diff capture → scoped witnesses | `PLAN_VALID`, `RETRY_SUBTASK`, `FIXUP_PLAN_VALID`, `MORE_SUBTASKS` | `DOER_DONE`, `CRASH`, `BLOCK_TASK` |
| `CHECKING_SUBTASK` | Objective gate + LLM checker grading the diff. | `SubtaskExecutionMixin._check_subtask` (`subtask_execution.py:226`) | `subtask_check_command` (e.g. `just check`) → empty-diff classifier → `subtask_checker` agent | `DOER_DONE` | `SUBTASK_PASSED`, `SUBTASK_FAILED`, `CRASH`, `BLOCK_TASK` |
| `TRIAGING_SUBTASK` | Failure dissected by triage agent (or synthesized). | `SubtaskWorkerMixin._record_subtask_triage` (`subtasks.py:406`) + `_triage_subtask` (`subtask_execution.py:506`) | `subtask_triage` agent OR synthesized transport/cannot_reproduce notes | `SUBTASK_FAILED` | `RETRY_SUBTASK`, `RETRY_EXHAUSTED`, `CRASH`, `BLOCK_TASK` |
| `COMMITTING` | Per-subtask commit just landed. | `commit_subtask` via `_handle_passed_subtask` (`subtasks.py:355`) | git commit (no agent) | `SUBTASK_PASSED` | `COMMIT_CREATED`, `CRASH`, `BLOCK_TASK` |
| `PUSHING` | Per-subtask push just landed. | `commit_subtask` flow | git push | `COMMIT_CREATED` | `MORE_SUBTASKS`, `ALL_SUBTASKS_DONE`, `CRASH`, `BLOCK_TASK` |
| `LOCAL_CI_CHECKING` | `cfg.local_ci_command` running pre-PR. | `PrePrAuditStageMixin._execute_audit_stages` via `run_local_ci_gate` (`pre_pr.py:457`, `pre_pr_audit_stages.py:108`) | local-CI subprocess only | `ALL_SUBTASKS_DONE`; also re-entered explicitly at top of each pre-PR cycle | `LOCAL_CI_PASSED`, `LOCAL_CI_FAILED`, `CRASH`, `BLOCK_TASK` |
| `PRE_PR_AUDITING` | **Umbrella.** Local-CI plus 3 audits (rubric / standards / architecture) plus behavior. | `PrePrWorkerMixin._run_pre_pr_pipeline` (`pre_pr.py:432`) and `_execute_audit_stages` (`pre_pr_audit_stages.py:65-164`) | Stages run sequentially: `local_ci` → `rubric` → `standards` → `architecture` → `behavior`; each invokes a JSON-mode audit agent | `LOCAL_CI_PASSED` | `AUDIT_PASSED`, `AUDIT_FAILED`, `MERGE_NODE_BUILT`, `CRASH`, `BLOCK_TASK`, `PARENT_MERGED_OR_CONFLICT` |
| `FIXUP_PLANNING` | Fixup planner emits new fixup subtasks. | `PrePrWorkerMixin._run_fixup_round` (`pre_pr.py:36`) and `_invoke_fixup_planner` (`pre_pr.py:206`) | `fixup_planner` agent + driver loop (`fixup_coverage.run_fixup_planner_loop`) + completeness re-prompt | `LOCAL_CI_FAILED`, `AUDIT_FAILED` | `FIXUP_PLAN_VALID` (→ `DOING_SUBTASK`), `FIXUP_EXHAUSTED`, `CRASH`, `BLOCK_TASK` |
| `PR_OPENING` | `gh pr create` and persist URL. | `PrLifecycleWorkerMixin._open_pr` (`pr_lifecycle.py:29`) | `github.open_pr` + idempotent reuse | `AUDIT_PASSED` | `PR_OPENED`, `CRASH`, `BLOCK_TASK` |
| `PENDING_CI` | PR open, polling for GitHub CI verdict. | `PrLifecycleWorkerMixin._poll_pr_loop` / `_poll_pr_once` (`pr_lifecycle.py:142-171`) | gh poll loop; intent-reviewer agent on parent-merge flag; conflict rebase fallback | `PR_OPENED`, `FEEDBACK_PUSHED`, `REBASE_PUSHED` | `CI_PASSED`, `CI_FAILED`, `PARENT_MERGED_OR_CONFLICT`, `PR_CLOSED` |
| `AWAITING_REVIEW` | CI green; awaiting human review or auto-merge. | same poll loop | gh poll loop only | `CI_PASSED` | `CHANGES_REQUESTED_RECEIVED`, `CI_FAILED`, `MERGED`, `PARENT_MERGED_OR_CONFLICT`, `PR_CLOSED` |
| `ADDRESSING_FEEDBACK` | **Umbrella.** Daemon-triggered response to CI failure OR `CHANGES_REQUESTED`. | `FeedbackWorkerMixin.run_ci_fix_response` (`feedback.py:114`) / `run_changes_requested_response` (`feedback.py:42`); also `_replan_and_resume` (`pr_lifecycle.py:451`) | Re-provision (no-worktree) → `_run_fixup_round` (fixup planner) → per-subtask `_run_subtask_set` (doer/checker/triage × N fixup slices) → commit + push → return to `PENDING_CI` | `CI_FAILED`, `CHANGES_REQUESTED_RECEIVED`; entered explicitly by replan path | `FEEDBACK_PUSHED`, `FEEDBACK_EXHAUSTED`, `CRASH`, `BLOCK_TASK`, `PARENT_MERGED_OR_CONFLICT` |
| `REBASING_TO_MAIN` | Rebase to current main / parent tip. | `RebaseWorkerMixin` (not read deeply; entry via `_handle_parent_rebase_if_needed`) | git fetch/rebase + push | `PARENT_MERGED_OR_CONFLICT`, `RESOLVED` | `REBASE_PUSHED`, `CONFLICT`, `CRASH`, `BLOCK_TASK` |
| `CONFLICT_RESOLVING` | Conflict-resolver agent on rebase failure. | rebase mixin | conflict-resolver agent | `CONFLICT` | `RESOLVED`, `UNRESOLVED`, `CRASH` |
| `MERGED` / `MERGE_NODE_READY` / `MERGE_NODE_RETIRED` / `BLOCKED` / `FAILED` / `ABORTED` | Terminal or merge-node landings. | scheduler / daemon (no inner agents) | — | various | terminal except `RETRY_TASK`/`RESUME_TASK`/`PARENT_ADVANCED` |

Critical fact for the operator's concern: while `ADDRESSING_FEEDBACK` is the parent state in the FSM, the per-subtask FSM helpers (`enter_doing_subtask`, `enter_checking_subtask`, `enter_triaging_subtask`) **deliberately no-op** when the parent is `ADDRESSING_FEEDBACK` (`fsm_runtime.py:27,45,51`, and the comment block at `subtasks.py:343-353`). This means: while addressing feedback, the row never transiently visits `DOING_SUBTASK` / `CHECKING_SUBTASK` / `TRIAGING_SUBTASK`. The TUI sees `addressing_feedback` for the entire fixup-planner-plus-N-subtask sequence. This is by design (Plan 54 patched the original `InvalidTransition: addressing_feedback → committing` crash by adding the gate) — but the design choice is the visibility hole the operator is reporting.

---

## 2. Flattening assessment

| State | Class | Recommendation |
|---|---|---|
| `PENDING` | Atomic | Keep. |
| `PROVISIONING` | Atomic | Keep. (Worktree + container are one provisioning act.) |
| `PLANNING` | Atomic | Keep. One planner agent + validators. |
| `DOING_SUBTASK` | Sequential umbrella (doer → diff capture → witnesses) | Keep atomic. Witnesses are part of "the doer's attempt." Sub-phases here are infrastructure, not decisions; phase tracking would clutter without operational value. |
| `CHECKING_SUBTASK` | Sequential umbrella (objective gate → empty-diff classifier → LLM checker) | Keep atomic. The empty-diff dispatch matters for the operator only when classification fails, and the artifact prefixes already encode the decision. |
| `TRIAGING_SUBTASK` | Atomic | Keep. |
| `COMMITTING`, `PUSHING` | Atomic | Keep. |
| `LOCAL_CI_CHECKING` | Atomic | Keep. |
| `PRE_PR_AUDITING` | **Sequential umbrella with iterative outer loop** | **Phase tracking.** Inside one cycle the stages are linear (`local_ci → rubric → standards → architecture → behavior`); the outer cycle counter already exists (`pre_pr_audit_summary` JSON, `store_planning_cycle.py:91`). The TUI just doesn't render it. Promoting each stage to a top-level state would multiply transitions by 5 and re-introduce the `enter_*` race class. See §4. |
| `FIXUP_PLANNING` | Atomic-ish (planner agent + completeness re-prompt) | Keep atomic. The driver loop in `fixup_coverage.run_fixup_planner_loop` is one agent's retries, not a phase change. |
| `PR_OPENING` | Atomic | Keep. |
| `PENDING_CI` | Atomic-ish (poll + intent-reviewer side effects) | Keep. The intent reviewer is rare and bounded. |
| `AWAITING_REVIEW` | Atomic | Keep. |
| `ADDRESSING_FEEDBACK` | **Iterative umbrella** — fixup-planning + per-subtask-loop × N fixup slices + commit + push | **Phase tracking + better artifact surfacing.** The internal flow is identical to a `FIXUP_PLANNING → DOING_SUBTASK → … → PUSHING` sub-cycle but cannot reuse those states because the parent task is locked in `ADDRESSING_FEEDBACK` to keep the post-PR FSM intact (Plan 28). Promoting to top-level states would re-introduce duplicate transitions (`addressing_feedback → committing → pushing → feedback_pushed`) and the `InvalidTransition` race class Plan 54 just patched. Phase tracking is the surgical fix. |
| `REBASING_TO_MAIN` | Atomic | Keep. |
| `CONFLICT_RESOLVING` | Atomic | Keep. |
| Terminal states | Atomic | Keep. |

**Headline:** only two umbrella states actually mask multiple agent invocations (`PRE_PR_AUDITING` and `ADDRESSING_FEEDBACK`). Everything else is either already atomic or the sub-phases (e.g. `DOING_SUBTASK`'s doer → diff → witnesses) are infrastructure rather than meaningful state.

---

## 3. `ADDRESSING_FEEDBACK` vs `PRE_PR_AUDITING`'s `local_ci` stage

Operator's specific question: does CI-failure handling in `ADDRESSING_FEEDBACK` (post-PR) use the same primitives as CI-failure handling in pre-PR's `local_ci` stage?

| Dimension | Pre-PR (`LOCAL_CI_FAILED → FIXUP_PLANNING`) | Post-PR `CI_FAILED → ADDRESSING_FEEDBACK` |
|---|---|---|
| Entry-point method | `PrePrWorkerMixin._run_pre_pr_pipeline` (`pre_pr.py:432`) detects `local_ci` stage fail and calls `_run_fixup_round(kind="fixup-pre-pr-audit", trigger="pre_pr_audit", …)` (`pre_pr.py:530`). For polled-PR CI failures the same call site fires from `PrLifecycleWorkerMixin._handle_polled_ci_failure` (`pr_lifecycle.py:259`) with `kind="fixup-ci", trigger="ci"`. | `FeedbackWorkerMixin.run_ci_fix_response` (`feedback.py:114`); the daemon's review-watcher tick fires `CI_FAILED` before invoking. Internally calls the same `_run_fixup_round(kind="fixup-ci", trigger="ci", …)` (`feedback.py:139`). |
| Fixup-planner inputs | `triage_root_cause` = merged failed-stage report block (`pre_pr.py:503-526`) augmented with required finding-coverage instructions; `expected_finding_ids` populated from the failed stages. | `ci_excerpt` = last 80 lines of GitHub failed-check logs (`feedback.py:128-132`); `local_ci_at_head` captured (Plan 53); no `expected_finding_ids` (CI failures don't carry typed finding ids). |
| Subtask emission pattern | Kind label `fixup-pre-pr-audit` for audit failures; `fixup-ci` when the failing stage is local-CI. Subtasks appended via `store.append_subtasks` with `planning_kind="fixup"` / `"fixup_ci"` (`pre_pr.py:171-187`). | Kind label `fixup-ci`; same `store.append_subtasks` call path; same `planning_kind="fixup_ci"`. |
| Doer/checker/triage loop | `_run_subtask_set(list(fixup_plan.subtasks))` → `SubtaskWorkerMixin._run_one_subtask` → `_do_subtask` / `_check_subtask` / `_record_subtask_triage`. | **Identical** call path — `_run_fixup_round` returns `self._run_subtask_set(...)`. Only difference: while the parent task is `ADDRESSING_FEEDBACK`, the per-subtask FSM events are suppressed (`subtasks.py:343-388`); pre-PR they fire normally. |
| Cycle budget / release valve | `cfg.pre_pr_audit_max_cycles` outer loop; each cycle re-runs all stages after fixups. Release-valve path (`release_valve_report`) opens the PR with deferred-findings notice. Structural-failure path BLOCKs the task. | `cfg.triage_budget_per_phase` (`pr_lifecycle.py:144`) bounds CI-fix attempts inside the poll loop. No release valve — the only outcomes are "push fixes, return to PENDING_CI" or BLOCKED. The `_run_fixup_round`-returned BLOCK is downgraded to "return to PENDING_CI with a warning note" in the feedback path (`feedback.py:146-159`). |
| `failure_layer` + signature semantics | Used uniformly in `subtask_execution.py:506-580`. Layers: `local_ci, rubric, standards, architecture, behavior, parse_failure, transport, cannot_reproduce`. Same `retry_classify.classify_retry` call in `subtasks.py:467-478`. | **Identical** — the per-subtask loop is shared code, so layer / signature stop-loss applies uniformly. |

**Synthesis:** the two flows are **semantically unified at the inner level** — they share `_run_fixup_round`, the fixup planner, the per-subtask loop, the triage layers, and the same stop-loss machinery. The differences are at the **outer wrapping**:

1. Pre-PR re-runs the full gauntlet after each fixup cycle. Post-PR `ADDRESSING_FEEDBACK` runs one fixup round, then leaves "did it work?" to GitHub Actions on the next `PENDING_CI` poll. This divergence is intentional (GitHub CI is the source of truth post-PR; rerunning local audits would double-count and contradict the "PR is the artifact" contract).
2. Pre-PR exposes its progress via `pre_pr_audit_summary` JSON + the gauntlet's own stages. Post-PR exposes nothing beyond `addressing_feedback`. **This divergence is accidental** — the data exists (subtask rows tagged `fixup-ci` / `fixup-review`, agent-call records, `review_round`, `ci_triage_retries`), it just isn't surfaced as a phase signal on the task row.
3. The post-PR flow does NOT re-audit (no rubric/standards/architecture/behavior on fixup output before pushing back). For a typo fix in CI, that's the right call. For a `CHANGES_REQUESTED` review that flags an architectural problem, it means the response can ship without the same gate the pre-PR pipeline would have demanded. Whether to harmonize is a design call — see §6.

So: the inner mechanics ARE unified. The visibility gap and the asymmetric "no re-audit on post-PR fixups" are the real divergences.

---

## 4. Proposed visibility improvements

### Path A — promote sub-phases to top-level FSM states

Concrete additions:

- For `PRE_PR_AUDITING`: split into `PRE_PR_LOCAL_CI`, `PRE_PR_RUBRIC`, `PRE_PR_STANDARDS`, `PRE_PR_ARCHITECTURE`, `PRE_PR_BEHAVIOR`. Add `AUDIT_STAGE_PASSED` event (×4 internal) + retain `AUDIT_PASSED` / `AUDIT_FAILED` as terminal cycle exits.
- For `ADDRESSING_FEEDBACK`: split into `FEEDBACK_FIXUP_PLANNING`, `FEEDBACK_DOING_SUBTASK`, `FEEDBACK_CHECKING_SUBTASK`, `FEEDBACK_TRIAGING_SUBTASK`, `FEEDBACK_COMMITTING_PUSHING`. Add five new events.

FSM-event-table growth: roughly **9 new states + ~22 new transitions** (each new state needs `CRASH`, `BLOCK_TASK`, `PARENT_MERGED_OR_CONFLICT` rows too). `TRANSITIONS` currently has ~70 entries; this is +30%.

Race-class risk: **high**. Plan 49/54 patched two `InvalidTransition: enter_*` races by making helpers re-read state and no-op on the parent-locked state. Every new state added duplicates the pattern that produced those races. The Plan-54 comment block at `subtasks.py:343-388` is already managing the gate on per-subtask helpers when the parent is `ADDRESSING_FEEDBACK`; adding `FEEDBACK_DOING_SUBTASK` etc. means duplicating that gate logic at the new helpers AND at every existing helper that re-reads state. The likely failure mode is "feedback flow crashes in the middle because a stale read fired `SUBTASK_PASSED` from `FEEDBACK_COMMITTING_PUSHING`". The plan 57 candidate generalization (re-read just before apply) would be required to ship Path A safely.

### Path B — phase tracking on the task row

Add one nullable text column on `tasks`: `current_phase`. Worker writes it at known boundaries:

- `PRE_PR_AUDITING`: `phase = "local_ci"` / `"rubric"` / `"standards"` / `"architecture"` / `"behavior"` / `"fixup_planning"`. (The data is already collected in `pre_pr_audit_summary`; this just hoists the in-flight stage to a top-level column for the TUI.)
- `ADDRESSING_FEEDBACK`: `phase = "provisioning"` / `"fixup_planning"` / `"doing_subtask:<id>"` / `"checking_subtask:<id>"` / `"triaging_subtask:<id>"` / `"committing_pushing"`.

TUI renders `addressing_feedback · checking_subtask:F-0003`. Briefing renders the same. `state_log` already records FSM transitions; phase changes can either share that channel (new `phase_changed` synthetic event) or live in a small ring buffer (`task_phases` table).

FSM unchanged → zero new `InvalidTransition` race surface.

### Hybrid

- Promote nothing in the FSM.
- Add `current_phase` column.
- Inside `_execute_audit_stages` (`pre_pr_audit_stages.py:65`) call a one-line `store.set_phase(self.node.id, stage_name)` per stage.
- Inside `_run_fixup_round` / `_run_subtask_set` (when parent is `ADDRESSING_FEEDBACK`), call the same helper with `"fixup_planning"` / `"doing_subtask:<id>"` / etc.
- Persisted artifacts (`subtask_doer:<id>`, `fixup_planner_output:<kind>:<round>:<attempt>`, `subtask_checker:<id>`) are already there; TUI / `qk show` should index by `phase` so an operator hitting `qk show R-0002` while it's in `addressing_feedback · checking_subtask:F-0002` sees that subtask's artifacts inline.

### Recommendation: **Path B (with the hybrid surfacing tweaks)**

Reasoning:

- **Race-class budget.** Plan 49/54 traffic shows the codebase is at the limit of what the current `enter_*` race-class can absorb. Path A multiplies the surface; Path B leaves the FSM alone.
- **Operator goal.** The operator wants the TUI to stop lying. Phase tracking achieves that directly — `addressing_feedback · checking_subtask:F-0002 (attempt 2)` is exactly the level of detail the concern asks for, and is achievable without touching `fsm.py`.
- **Architecture budget.** Files are at the 600-line cap (`pre_pr.py` 591, `pr_lifecycle.py` 597, `subtasks.py` 582). Phase tracking is a one-column schema migration + a 5-line helper + ~6 single-line annotation sites. Path A inflates `fsm.py`, `fsm_runtime.py`, and every worker mixin.
- **Ship cost.** Path B is small (1-2 days of focused work, includes schema migration + TUI/briefing/state_log render touch-ups + a few test assertions). Path A is large (state additions cascade into the orphan-recovery table in `recover_after_crash`, the `recover_after_crash` post-PR side-state bridging in `mark_merged`, the orchestrator's `ACTIVE_STATES` membership tests, the rebase-cascade `PARENT_MERGED_OR_CONFLICT` blanket transitions, and the architecture-doc auto-check against `fsm.py`).

The unification work in §5 is independent and can ship before or after Path B.

---

## 5. Unification opportunities

The audit found one accidental divergence worth fixing alongside (or before) visibility work: **`ADDRESSING_FEEDBACK` does not re-audit fixup output the way `PRE_PR_AUDITING` does.**

Today: `run_ci_fix_response` and `run_changes_requested_response` push fixup commits and return to `PENDING_CI`, relying on GitHub Actions to re-grade. For `fixup-ci` that is correct — GitHub IS the ground truth. For `fixup-review` (response to a `CHANGES_REQUESTED` review) it is weaker than the pre-PR contract: a review might say "refactor the X helper to match docs/architecture/foo.md §3" and the response can ship without the architecture audit checking that.

Proposed refactor (medium):

- Extract a `_run_fixup_then_settle` helper on `PrePrWorkerMixin` that, after `_run_subtask_set` returns clean, optionally runs a scoped re-audit (just the stages relevant to the fixup kind: `behavior` always; `architecture` if any subtask cited an architecture ref; `standards` if any cited a standards ref).
- `FeedbackWorkerMixin.run_changes_requested_response` invokes that helper instead of dropping straight back to `PENDING_CI`.
- `FeedbackWorkerMixin.run_ci_fix_response` skips the re-audit (GitHub CI will run).

Regression risk: low for `run_ci_fix_response` (no behavior change). Medium for `run_changes_requested_response`: scoped re-audit could BLOCK on its own findings, surfacing what today silently passes. That's the goal, but it changes block rates. Gate behind `cfg.changes_requested_re_audit` (default off until measured).

Sequencing: Path B (visibility) ships first. Re-audit lands after, against a TUI that can now show `addressing_feedback · re_audit:architecture`. Otherwise the operator can't see the new failure mode.

---

## 6. Open questions for the operator

1. **Is `addressing_feedback`'s lack of a re-audit gate intentional?** §5 calls it "accidental"; the historical Plan-28 retirement of `TRIAGING_FEEDBACK` was about removing a per-thread classifier, not about removing a re-audit. Confirm whether the design intent was "GitHub re-grades, full stop" or "we'll add it back when we observe a problem."
2. **Phase-tracking key collisions.** When `ADDRESSING_FEEDBACK` is processing multiple fixup slices in series, do you want one row-level `current_phase` clobbered per subtask, or a phase stack rendered as `addressing_feedback · F-0001 (done), F-0002 (checking), F-0003 (pending)`? The latter is more useful but needs more than a single column.
3. **Stage-level FSM in pre-PR.** §4 dismissed promoting the 5 audit stages because the data is in `pre_pr_audit_summary`. If you want the auto-checked Mermaid diagram (`docs/architecture.md`) to be the operator-readable spec of "what the task is doing", phase-tracking won't satisfy that — it's a row column, not an FSM edge. Acceptable to keep the diagram silent on stage detail, or does the diagram need to grow?
4. **Block-on-replan-budget vs. block-on-feedback-exhausted.** `_replan_and_resume` (`pr_lifecycle.py:451`) enters `ADDRESSING_FEEDBACK` but on success returns to `PENDING_CI` via `enter_pending_ci`, NOT via `FEEDBACK_PUSHED`. That means a replan that lands cleanly never fires `FEEDBACK_PUSHED`; the audit trail loses the "feedback addressed" marker. Bug or intentional?
5. **`fixup-ci` from the pre-PR `local_ci` stage vs. from the post-PR poll.** Both use `kind="fixup-ci"` but the prompts see different `local_ci_at_head` semantics (pre-PR: gate just failed in this container; post-PR: GitHub CI failed, local may still be green → `cannot_reproduce`). The `_is_fixup_ci_subtask` checker (`subtask_execution.py:86`) is correct, but is the planner prompt branching on `trigger` to take advantage of the distinction?

---

End of audit. No code modified.
