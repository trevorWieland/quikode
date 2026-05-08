# Plan 33: Rubric-First Information Architecture

> Status: design-complete, ready for implementation. Two-PR rollout (PR-A + PR-B), then a hard reset of all in-flight work. No backwards compatibility.

---

## 1. Why

### 1.1 The diagnosis

Today, every upstream agent in the pipeline — planner, subtask-doer, subtask-checker, subtask-triage, fixup-planner, merge-planner — is graded against a four-stage post-implementation gauntlet (`local_ci`, `rubric`, `standards`, `behavior`) that **they cannot see**. They produce work product, that work product hits the audit, the audit fails, and a fixup planner tries to repair the gap from finding-text alone. The audit gauntlet is the truth-of-record for whether work ships, but every agent upstream of it operates on a degraded shadow of that truth.

This is the studying-for-the-test vs. cheating distinction. The fix is to **show every upstream agent the actual rubric they will be graded against, in their context window, at decision time.** The audit gauntlet should become the EXCEPTION, not the rule.

### 1.2 The failure-mode evidence

- The tanren overnight run produced **zero PRs across an entire week** despite continuous daemon activity.
- Recurring BLOCK patterns dominated the logs:
  - **R-0010 (disclaim)** — doer commits a partial implementation, narrates "I left X for follow-up."
  - **R-0008 (simulated-BDD)** — behavior witnesses fabricate evidence the code doesn't actually produce.
  - **R-0024 (scope-loop)** — scope review oscillates the same file in/out of lane until same-signature stop-loss fires.
- "Carry forward weak plans" pathology: a planner emits a vague subtask list, the doer can't reason about what will satisfy the audit, the checker can't either (it only sees what the planner wrote), the audit fires and finds gaps, the fixup planner tries to patch — but the original information deficit is never repaired, only papered over. Same plan, more rounds, no real progress.

### 1.3 The thesis

If the four-stage audit's grading rubrics — verbatim — are the **first thing every upstream agent reads**, the planner can write subtasks that map cleanly onto rubric coverage, the doer can self-grade before the checker sees the work, the checker can verify against the same bar the audit will use, the triage can teach concretely instead of speculating, and the fixup planner becomes a rare-path tool. The audit gauntlet shifts from being the only place the rubric is articulated to being a confirmation of work that already met the rubric upstream.

---

## 2. User-resolved decisions (the contract)

These are load-bearing. Implementation agents cite this section by number.

- **D1.** A single shared `EvaluationContract` object, built once when a task enters `PROVISIONING`, persisted as a per-task artifact, loaded by every prompt-render entry point. Lives at `quikode/evaluation_contract.py` (new). Shape per Section 3.
- **D2.** Schema changes per Section 4: add `rubric_targets`, `standards_referenced`, `behavior_evidence_advanced` to `Subtask`; add `gauntlet_strategy` to `Plan`; demote `files_to_touch` to advisory; delete `addresses_findings` (folded into the three new stage-typed fields).
- **D3.** Three hard validators on the planner output: `validate_rubric_coverage`, `validate_evidence_partition`, `validate_standards_paths`. Max 2 re-prompts before BLOCK.
- **D4.** Plan 24's Z-99 stabilization subtask survives, with explicit `rubric_targets=[(cat, cfg.pre_pr_rubric_min_score) for cat in cfg.pre_pr_rubric_categories]` — the holistic-pass guardian.
- **D5.** Scope review retired entirely (NOT a separate plan). Files deleted: `quikode/scope_review.py`, `prompts/scope-review.md`, `tests/test_scope_review.py`. Plumbing removed from `quikode/worktree.py` and `quikode/workers/subtask_completion.py`. The `_classify_empty_staging` helper survives (still load-bearing for Z-99's gate-only success path).
- **D6.** No `files_to_touch` deterministic ceiling. No multiplier check. No soft warning. The audit gauntlet is the truth.
- **D7.** Per-subtask loop: doer codes → SELF_AUDIT block (deterministic parser) → fast-fail short-circuit if `predicted_score < min` OR `RISK/STUB` → otherwise LLM checker (different model, adversarial verification) → on FAIL, LLM triage (senior-engineer-tutoring-junior) → next doer attempt with full context.
- **D8.** Doer self-audit is a deterministic short-circuit only. The LLM checker still runs after a clean self-audit because we want the adversarial different-model verification before moving on.
- **D9.** Triage is "the senior engineer tutoring the junior" — concrete, with file:line cites, teaching the doer the concept they missed. Stays LLM-driven. Prompt shrinks from ~70 lines to ~25-30, but keeps the LLM's reasoning. The next doer attempt addresses every single part, leaving none for later.
- **D10.** The per-subtask checker runs **scoped witness commands** for the `behavior_evidence_advanced` items this subtask claims to advance — only this subtask's claimed advances, not the parent task's full `expected_evidence` set. Catches stubs that LLM code-reading misses.
- **D11.** Hard reset: clean cutover, zero backwards compat. Validator rejects pre-plan-33 plans by construction. Mass `qk retry` on every non-merged task at deploy, including in-flight `kind="merge"` rows.
- **D12.** SELF_AUDIT block format per Section 6. Deterministic parser. Re-prompt with one targeted message before invoking LLM checker if parse fails. Max 1 re-prompt.
- **D13.** Replace "don't do X" with "do Y, here's the rubric" wherever it appears in prompts. Specifically:
  - "pre-existing failure trap" subsection in `prompts/subtask-doer.md` is deleted (SELF_AUDIT structurally prevents it).
  - "What NOT to put in the plan" coda in `prompts/planner.md` is replaced with positive coverage demands.
  - "Two non-negotiable invariants" framing in `prompts/subtask-doer.md` collapses into positive-framed §1 ("Your job in one sentence") + §6 ("The local-CI gate").
  - `prompts/subtask-triage.md`'s scope-reviewer-as-failure-layer enumeration is deleted; the layer set becomes `{local_ci, rubric, standards, behavior, self_audit_mismatch, transport}`.
- **D14.** Preserved (still load-bearing): plan 12 (no-CI-leak invariant), plan 14 (no-fabricate checker), plan 17 (clean-loop prompt arch), plan 18 (doer inspect-actual-diff — now `SELF_AUDIT.diff_reconcile`), plan 22 (doer prior-output carry-forward, fed structured SELF_AUDIT), plan 23 (same-signature stop-loss), plan 24 (Z-99 with full `rubric_targets`), plan 26 (Z-99 resume guard, TUI fixes), plan 28 (post-PR FSM; the driveby doer-prompt-fix retired since SELF_AUDIT structurally prevents the disclaim trap).

---

## 3. The `EvaluationContract` abstraction

### 3.1 Shape

```python
# quikode/evaluation_contract.py (new module)

@dataclass(frozen=True)
class StageRubric:
    name: Literal["local_ci", "rubric", "standards", "behavior"]
    one_line: str            # "what this stage measures"
    threshold: str           # "rc=0", "every category >= 7", etc.
    grading_template: str    # the agent-facing JSON schema or grader prompt fragment
                             # (verbatim from pre-pr-{rubric,standards,behavior}.md)
    source_text: str         # the canonical doc the stage references (cap'd)

@dataclass(frozen=True)
class EvaluationContract:
    task_id: str
    local_ci: StageRubric    # built from cfg.local_ci_command
    rubric: StageRubric      # built from cfg.pre_pr_rubric_categories + min_score
    standards: StageRubric   # full text of cfg.pre_pr_standards_profile_globs (60k cap)
    behavior: StageRubric    # rendered from node.expected_evidence
```

### 3.2 Lifecycle

- **Constructor:** `evaluation_contract.build_for(node, cfg) -> EvaluationContract`. Called exactly once per task, at the `PROVISIONING → PLANNING` transition in `quikode/workers/task_worker.py`. Pure function: same `(node, cfg)` → same contract; no side effects beyond returning the dataclass.
- **Persistence:** serialized to a per-task artifact at `<workspace>/state/<task_id>/evaluation_contract.json`. Persisted via the existing per-task store conventions in `quikode/state.py` / `quikode/store_tasks.py`.
- **Loader:** `EvaluationContract.load(store, task_id) -> EvaluationContract`. Called by every prompt-render entry point (planner, subtask-doer, subtask-checker, subtask-triage, fixup-planner, merge-planner). Cached on `Task` after first load to avoid re-parsing within a worker tick.
- **Build inputs:**
  - `local_ci.threshold` = `"rc=0"`. `local_ci.grading_template` includes `cfg.local_ci_command`.
  - `rubric.threshold` = `f"every category >= {cfg.pre_pr_rubric_min_score}"`. `rubric.grading_template` lifted from `prompts/pre-pr-rubric.md` (the JSON schema fragment). `rubric.source_text` is `cfg.pre_pr_rubric_categories` rendered as a list with each category's blurb.
  - `standards.threshold` = `"no drift from any cited section"`. `standards.source_text` = full text of every doc matching `cfg.pre_pr_standards_profile_globs`, capped at 60k chars (truncate-with-marker; emit a WARN log if truncated).
  - `behavior.source_text` = `node.expected_evidence` rendered as a witness-list. `behavior.grading_template` lifted from `prompts/pre-pr-behavior.md`.

### 3.3 Render variants

A single jinja partial `prompts/_evaluation_context.md.j2` with three macros:

- `{% call ec_full(contract) %}` — emits all four stage cards in full. Used by planner, fixup-planner, merge-planner.
- `{% call ec_stage_card(contract, stage_name) %}` — emits one stage's card. Used by subtask-doer (called four times, in order, woven into the doer's task framing).
- `{% call ec_targeted(contract, subtask) %}` — for the subtask-checker and subtask-triage. Filters: only the rubric categories in `subtask.rubric_targets`, only the standards refs in `subtask.standards_referenced`, only the witnesses in `subtask.behavior_evidence_advanced`. Plus always-on `local_ci` card.

Token budgets:
- Full: ~6-9k tokens (60k char standards doc dominates).
- Stage card: ~1-2k each.
- Targeted: ~2-4k.

The partial uses Jinja's `StrictUndefined` (already configured in `quikode/prompts.py`) so missing fields fail loudly at render time — never silently empty.

---

## 4. Schema changes (`quikode/subtask_schema.py`)

### 4.1 New types

```python
class RubricTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    category: str           # must be in cfg.pre_pr_rubric_categories
    predicted_score: int    # planner's projection; doer's self_audit_threshold

class StandardsRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    doc_path: str           # must exist at planning time
    section: str            # heading or anchor; free-form
```

### 4.2 `Subtask` deltas

Add:
- `rubric_targets: tuple[RubricTarget, ...]` — categories planner expects this slice to materially advance, with predicted scores. Empty allowed only on `kind="fixup-*"` subtasks where the fix is purely transport/CI.
- `standards_referenced: tuple[StandardsRef, ...]` — pinned standards passages governing this slice.
- `behavior_evidence_advanced: tuple[str, ...]` — canonical ids of `node.expected_evidence` items this subtask delivers a witness for. Each id appears in **exactly one** subtask across the plan (partition, not cover).

Demote:
- `files_to_touch` — keep the field (still useful as advisory metadata in the doer prompt and in `qk show` rendering), drop all semantic enforcement. No commit-time gating against it. No multiplier cap.

Delete:
- `addresses_findings` — folded into the three new stage-typed fields. Fixup-pre-pr-audit subtasks now declare which rubric/standards/behavior gaps they close via `rubric_targets`, `standards_referenced`, and `behavior_evidence_advanced` directly.

### 4.3 `Plan` deltas

Add:
- `gauntlet_strategy: str` — free-text 200-500 word section: "How this plan is positioned to pass the four-stage audit on cycle 1." Validator enforces 200-2000 chars; below 200 → re-prompt; above 2000 → truncate with WARN.

### 4.4 Validators on `Plan` (hard-fail with re-prompt; max 2 re-prompts; then BLOCK)

- `validate_rubric_coverage(plan, contract) -> None`
  - Every category in `contract.rubric.source_text`'s category list appears in **at least one** subtask's `rubric_targets`.
  - Failure message format: `"rubric category {cat!r} is not advanced by any subtask; assign it to at least one subtask's rubric_targets"`.
- `validate_evidence_partition(plan, node) -> None`
  - Every id in `node.expected_evidence` appears in **exactly one** subtask's `behavior_evidence_advanced`.
  - Failure messages distinguish missing (no subtask claims it) from duplicated (two subtasks both claim it).
- `validate_standards_paths(plan, repo_root) -> None`
  - Every `standards_referenced[].doc_path` resolves to an existing file under `repo_root` at planning time.
  - Failure includes the offending path and the subtask id.

### 4.5 Z-99's new construction

`_build_stabilization_subtask` (existing) gains:
```python
"rubric_targets": [{"category": cat, "predicted_score": cfg.pre_pr_rubric_min_score}
                   for cat in cfg.pre_pr_rubric_categories],
"standards_referenced": [],
"behavior_evidence_advanced": [],
```
This is the explicit holistic-pass guardian. Z-99 is exempt from `validate_evidence_partition` (it claims no witnesses; all evidence is partitioned across earlier subtasks) and from the no-empty-rubric-targets requirement (in fact it has the maximally broad rubric_targets list).

### 4.6 Migration

- Validator rejects pre-plan-33 plans by construction (missing `rubric_targets` / `behavior_evidence_advanced` / `gauntlet_strategy` → `extra="forbid"` on Pydantic models triggers).
- Per D11, mass `qk retry` at deploy on every non-merged task. See Section 12 for the migration sequence.

---

## 5. Prompt rewrites (section outlines)

For each prompt, the implementation agent fills in the prose; this outline determines structure and jinja variables.

### 5.1 `prompts/planner.md` (replaces today's 173-line template)

Jinja vars in: `node`, `contract` (EvaluationContract), `repo_root`, `prior_attempt_notes` (optional).

Outline:
1. **Your job in one sentence** — decompose this node into 4-8 subtasks that, executed in order, will pass the four-stage audit on cycle 1.
2. **The bar you are studying for (verbatim)** — `{% call ec_full(contract) %}`. The four stage cards in full. This is the test. Write subtasks that pass it.
3. **The DAG node** — `node.title`, `node.spec`, `node.expected_evidence` rendered as a list with ids.
4. **What each subtask must declare** — schema explainer with the new fields. Worked micro-example showing one subtask with all three stage-typed fields populated.
5. **Coverage demands (positive framing — replaces today's "What NOT to put")** —
   - "Every rubric category appears in at least one subtask's `rubric_targets`."
   - "Every behavior evidence id appears in exactly one subtask's `behavior_evidence_advanced`."
   - "Every cited standards doc path exists in the repo."
6. **`gauntlet_strategy` field** — write a 200-500 word section explaining how this plan is positioned to pass each stage on cycle 1. Specifically: which subtasks carry rubric weight, how standards alignment is preserved, where the witnesses come from, and what local-CI risks exist.
7. **Output schema (JSON)** — full schema with the new fields.
8. **Hard rules** — JSON only inside ```json fences; no narration outside; valid Plan or re-prompt.

### 5.2 `prompts/subtask-doer.md` (replaces today's 148-line template)

Jinja vars in: `node`, `plan`, `subtask`, `contract`, `triage_notes` (optional), `prior_self_audit` (optional, structured).

Outline:
1. **Your job in one sentence** — implement this subtask such that its claimed rubric_targets, standards_referenced, and behavior_evidence_advanced will withstand adversarial review by a different model.
2. **The subtask** — title, boundary, acceptance, files_to_touch (advisory).
3. **The rubric you will be graded against (verbatim, scoped)** — `{% call ec_targeted(contract, subtask) %}`. Only the categories, standards refs, and witnesses this subtask owns. Plus the always-on `local_ci` card.
4. **Plan context** — full plan summary + `gauntlet_strategy` excerpt + the other subtasks' titles (so this doer knows what neighbors are doing).
5. **Prior attempt (if any)** — `triage_notes` + `prior_self_audit` rendered structurally.
6. **The local-CI gate (positive framing — replaces "non-negotiable invariants")** — you must run `cfg.local_ci_command` and confirm rc=0 before claiming completion. The SELF_AUDIT block proves it.
7. **The SELF_AUDIT block (mandatory output)** — exact format per Section 6. Examples of well-formed and ill-formed entries.
8. **What "address every single part, leaving none for later" means** — if you cannot complete a piece, the SELF_AUDIT records it as `RISK` or `STUB` and the deterministic short-circuit will fail you fast — no narrative disclaim is acceptable. (This replaces the deleted "pre-existing failure trap" subsection per D13.)
9. **Output expectations** — your work as a unified diff (existing convention) plus the SELF_AUDIT block.

### 5.3 `prompts/subtask-checker.md` (replaces today's 53-line template)

Jinja vars in: `node`, `subtask`, `contract`, `self_audit` (parsed structured object), `diff_text`, `witness_results` (per-evidence-id rc + output excerpt).

Outline:
1. **Your job in one sentence** — verify the doer's SELF_AUDIT claims against the actual diff and witness outputs. Plan 14: never invent criteria the planner didn't write.
2. **What you were given** — the subtask's targeted contract, the doer's SELF_AUDIT, the unified diff, and pre-run witness results.
3. **Verification matrix** — for each `rubric_target`: does the diff substantively advance this category? PASS / FAIL / UNKNOWN with one-line rationale. For each `standards_referenced`: does the diff align with the cited section? PASS / FAIL / UNKNOWN. For each `behavior_evidence_advanced`: did the witness command emit substantive output (not a stub)? Use `witness_results`.
4. **Output schema (JSON)** — `{ overall: PASS|FAIL, per_target: [...], per_standards_ref: [...], per_witness: [...], notes: str }`.
5. **Hard rule (plan 14 preserved)** — you may only verify what the planner wrote and what the doer claimed. You may not invent new acceptance criteria. If the planner's coverage looks wrong to you, say so in `notes` but still grade against what's there.

### 5.4 `prompts/subtask-triage.md` (replaces today's 65-line template; target ~25-30 lines)

Jinja vars in: `subtask`, `contract`, `self_audit`, `checker_verdict`, `diff_text`.

Outline:
1. **Your job in one sentence** — given the predetermined fact that this work is not right (the checker said so), tell the next doer exactly where they went wrong, with file:line cites, and teach them the concept they missed.
2. **Inputs** — the targeted contract, the SELF_AUDIT, the checker's verdict, the diff.
3. **The senior-engineer-tutoring-junior framing** — your tone is concrete and specific. Not "consider X" but "at file.py:L142 the function returns early when foo is None; the rubric category 'edge-case-handling' demands a fallback here. Add a default that …".
4. **Output schema** — `{ failure_layer: local_ci|rubric|standards|behavior|self_audit_mismatch|transport, root_cause: str, file_line_cites: [str], teaching_narrative: str }`. (Failure-layer enumeration per D13: scope_review removed; self_audit_mismatch added for cases where the doer's claimed scores didn't match the diff reality.)
5. **Plan 14 preserved** — you do not prescribe code. The next doer attempt has autonomy to choose how to fix; you tell them what's wrong and why.

### 5.5 `prompts/fixup-planner.md` (replaces today's 126-line template)

Jinja vars in: `node`, `original_plan`, `contract`, `audit_bundle` (the failed audit's per-stage findings).

Outline:
1. **Your job in one sentence** — emit additive subtasks that close the gaps the audit found. Use the same schema as the spec planner (`rubric_targets`, `standards_referenced`, `behavior_evidence_advanced`), so the per-subtask checker can verify the fix the same way.
2. **The audit bundle** — per-stage findings, each with finding-id and offending-file-line.
3. **The bar (verbatim)** — `{% call ec_full(contract) %}`. Same contract the spec planner saw. The audit failed against this; your fixup must close that gap.
4. **Coverage demand** — every finding-id in the audit bundle must be addressed by exactly one subtask (declare via the stage-typed fields, not a separate `addresses_findings` list — that field is gone per D2).
5. **Output schema** — `FixupPlan` with the new fields. `findings_addressed` field stays (audit-driven completeness check still unions across subtasks).

### 5.6 `prompts/merge-planner.md` (replaces today's 141-line template; same upgrade as planner.md)

Jinja vars in: `merge_node`, `parent_branches_diffs`, `contract`, `repo_root`.

Outline mirrors planner.md exactly (sections 1-8), but framed for merge nodes:
- "The DAG node" → "The merge node" (with parent diffs rendered).
- Coverage demands and validators apply identically.
- `gauntlet_strategy` here addresses how the merge resolves cross-branch conflicts without breaking any parent's rubric_targets / standards / witnesses.

---

## 6. SELF_AUDIT format + deterministic parser

### 6.1 Format (mandatory in every doer output)

```
SELF_AUDIT:
  gate_local_ci: rc=<n> (cmd: <command>)
  gate_rubric:
    <category>: predicted_score=<n>  rationale: <one line>  evidence: <file:line>
    ...
  gate_standards:
    <doc§section>: aligned (cite paragraph) | drifted (and why fixed)
    ...
  gate_behavior:
    <evidence_id>: witnessed_by=<command run>  output_excerpt=<...>
    ...
  diff_reconcile:
    <every file in `git diff HEAD --stat`>: in_lane | gate_fix(<gate>) | <fixed_in_place>
```

### 6.2 Parser

Lives at `quikode/self_audit.py` (new). Public surface:

```python
@dataclass(frozen=True)
class ParsedSelfAudit:
    gate_local_ci_rc: int | None
    gate_local_ci_cmd: str
    gate_rubric: dict[str, RubricRow]      # category -> predicted_score, rationale, evidence
    gate_standards: dict[str, StandardsRow]  # "doc§section" -> aligned/drifted, citation
    gate_behavior: dict[str, BehaviorRow]    # evidence_id -> witnessed_by, excerpt
    diff_reconcile: dict[str, str]           # file_path -> in_lane|gate_fix(...)|fixed_in_place
    raw: str
    parse_errors: list[str]                  # empty on clean parse

def parse_self_audit(text: str) -> ParsedSelfAudit: ...
def short_circuit_decision(parsed: ParsedSelfAudit, *, contract: EvaluationContract,
                           subtask: Subtask) -> ShortCircuit:
    """Returns ShortCircuit.FAIL_FAST if any predicted_score < min OR any
    RISK/STUB token appears in rationale/evidence/excerpt. Else PROCEED."""
```

### 6.3 Re-prompt-on-parse-failure loop

- Max 1 re-prompt. On parse error, send the doer one targeted message: `"Your SELF_AUDIT block was missing or malformed: <parse_errors[0]>. Re-emit the SELF_AUDIT block in the exact format. Do not change the diff."`
- If second attempt also fails → fail the subtask with `failure_layer="self_audit_mismatch"`. Triage runs.

### 6.4 Deterministic short-circuit

- If `gate_local_ci_rc != 0` → FAIL FAST, skip checker, run triage with `failure_layer="local_ci"`.
- If any `gate_rubric[cat].predicted_score < cfg.pre_pr_rubric_min_score` → FAIL FAST, skip checker, run triage with `failure_layer="rubric"`.
- If any RISK/STUB token (case-insensitive, regex `\b(RISK|STUB|TODO|FIXME|XXX)\b`) appears in any rubric/standards/behavior row → FAIL FAST, skip checker, run triage with `failure_layer="self_audit_mismatch"`.
- Else PROCEED to LLM checker.

### 6.5 Hand-off into LLM checker

The LLM checker receives the `ParsedSelfAudit` rendered as structured Jinja context (not just raw text), plus the `witness_results` dict (Section 7), plus the diff. The checker can compare the doer's claimed `evidence: <file:line>` against actual diff lines.

---

## 7. Per-subtask checker enhancements

### 7.1 LLM checker

- Verifies SELF_AUDIT claims against the diff (per Section 5.3).
- Plan 14 preserved: never fabricates criteria.

### 7.2 Witness re-running

- Before invoking the LLM checker, the worker (in `quikode/workers/subtask_execution.py`) iterates `subtask.behavior_evidence_advanced`:
  - For each `evidence_id`, look up the witness command on `node.expected_evidence[evidence_id]`.
  - Run it inside the worktree's container (existing `quikode/docker_env.py` plumbing). Capture rc + first 4KB of stdout/stderr.
  - Cap total witness runtime for one subtask at **30 seconds wall-clock**, individual witness at **15 seconds**. On exceed, mark that witness as `TIMEOUT` and proceed.
- Result: `witness_results: dict[evidence_id, {rc, stdout_excerpt, stderr_excerpt, runtime_ms}]`.
- Passed to the LLM checker as Jinja context. Catches stub-shaped diffs that look right to a code reader but produce empty/error witness output at runtime.

### 7.3 Output schema

```json
{
  "overall": "PASS" | "FAIL",
  "per_rubric_target": [{"category": "...", "verdict": "PASS|FAIL|UNKNOWN", "rationale": "..."}],
  "per_standards_ref": [{"doc_section": "...", "verdict": "PASS|FAIL|UNKNOWN", "rationale": "..."}],
  "per_behavior_witness": [{"evidence_id": "...", "verdict": "PASS|FAIL|UNKNOWN",
                            "witness_rc": 0, "rationale": "..."}],
  "notes": "freeform"
}
```

---

## 8. Triage rewrite (senior-engineer-tutoring-junior)

### 8.1 Inputs

- The targeted `EvaluationContract` slice (only what this subtask owns).
- The structured `ParsedSelfAudit`.
- The `checker_verdict` JSON from Section 7.3.
- The unified `diff_text`.

### 8.2 Output

- `failure_layer ∈ {local_ci, rubric, standards, behavior, self_audit_mismatch, transport}`.
- `root_cause: str` — concrete, with file:line cites.
- `file_line_cites: [str]` — list of `path:line` references.
- `teaching_narrative: str` — explains the concept the doer missed, in language the next doer attempt can apply.

### 8.3 Length target

- Prompt length target: ~25-30 lines (down from today's ~70).
- Achieved by deleting:
  - The scope-reviewer-as-failure-layer enumeration (D5, scope review retired).
  - The deterministic categorization heuristics (the user explicitly chose LLM-driven over deterministic).
  - The "do not prescribe code" guardrail repeated five times — say it once.

### 8.4 Plan 14 preservation

Triage tells the doer **what's wrong and why**, not what code to write. The next doer attempt has autonomy.

---

## 9. Worker pipeline updates

### 9.1 `quikode/workers/task_worker.py`

- At `PROVISIONING → PLANNING`: call `evaluation_contract.build_for(node, cfg)`, persist to `<workspace>/state/<task_id>/evaluation_contract.json`.
- On every prompt-render call, load the contract via `EvaluationContract.load(store, task_id)` and pass to the renderer.

### 9.2 `quikode/workers/subtasks.py` (planner driver)

- Pass `contract` into `prompts.render_planner(...)`.
- After parsing, run the three new validators (Section 4.4). On failure, re-prompt with the validator message; max 2 re-prompts; then BLOCK with `failure_reason="planner_validator_<which>"`.

### 9.3 `quikode/workers/subtask_execution.py`

- After doer LLM call returns:
  1. Parse SELF_AUDIT via `quikode.self_audit.parse_self_audit(...)`.
  2. On parse error: re-prompt once (Section 6.3); if still bad, fail with `failure_layer="self_audit_mismatch"`.
  3. Compute `short_circuit_decision(...)`. If `FAIL_FAST`, skip directly to triage; do not invoke the LLM checker.
  4. Else: run scoped witnesses (Section 7.2), assemble `witness_results`.
  5. Invoke LLM checker with `(contract, subtask, parsed_self_audit, diff_text, witness_results)`.
  6. On checker FAIL: invoke triage with `(targeted contract, parsed_self_audit, checker_verdict, diff_text)`.
  7. On any FAIL path, the next doer attempt receives the prior `parsed_self_audit` (structured) plus `triage_notes`.

### 9.4 `quikode/workers/subtask_completion.py`

- Remove all `_apply_lane_review` invocations and the `lane_review_fn` parameter.
- `commit_subtask` now commits without per-file gating. The `_classify_empty_staging` helper survives (used by Z-99's gate-only success path).

### 9.5 `quikode/workers/pre_pr.py` and `quikode/pre_pr_audit.py`

- Audit stages still run (the four-stage gauntlet stays); they're now usually pass-on-cycle-1, sometimes pass-on-cycle-2, rarely beyond.
- The `EvaluationContract` is the same one the upstream agents saw; the audit reads it from the same per-task artifact (single source of truth).

### 9.6 `quikode/workers/merge_node_worker.py`

- Same EvaluationContract integration as `task_worker.py`. Merge nodes get a contract built at merge-PROVISIONING.
- `merge-planner.md` rewrite (Section 5.6) consumes it.

### 9.7 `quikode/worktree.py`

- Remove `lane_review_fn` parameter from any function signature that takes it.
- Remove imports of `quikode.scope_review`.

---

## 10. Scope-review retirement (folded into plan 33)

### 10.1 Files deleted

- `/home/trevor/github/quikode/quikode/scope_review.py` (193 lines, whole module)
- `/home/trevor/github/quikode/prompts/scope-review.md` (82 lines)
- `/home/trevor/github/quikode/tests/test_scope_review.py`
- `/home/trevor/github/quikode/quikode/prompts.py`: remove the `render_scope_review(...)` function (if present).

### 10.2 Plumbing removed

- `quikode/worktree.py`: remove `lane_review_fn` parameter and any call sites that thread it through.
- `quikode/workers/subtask_completion.py`: remove `_apply_lane_review` function + all callers.
- `quikode/workers/task_worker.py`: remove any scope-review dispatch.

### 10.3 Tests touched (not deleted, but updated)

- `tests/test_pre_commit_gate.py`: remove scope-review assertions, keep gate-classification assertions.
- `tests/test_per_subtask_commit.py`: remove scope-review assertions.
- `tests/test_worktree.py`: remove scope-review path tests.
- `tests/test_progress_check.py`: remove scope-review observability assertions (paired with plan 21 retirement of the observability hooks).

### 10.4 What survives

The `_classify_empty_staging` helper in `subtask_completion.py` (load-bearing for Z-99's gate-only success path: when Z-99 runs and the gate is already green and there's nothing to commit, that's a legitimate success, not a failure).

---

## 11. First-cycle pass model — worked example

### 11.1 Setup

Hypothetical tanren task **R-0050** — "Project archival": users can mark projects as archived, archived projects are excluded from default list views across web/api/cli, but still retrievable by id and listable with an explicit `--include-archived` flag.

- `node.expected_evidence` = 6 ids:
  - `B-0061-web-positive`: archive a project, list view excludes it.
  - `B-0061-web-falsification`: archived project remains retrievable by id.
  - `B-0061-api-positive`: api `GET /projects` excludes archived.
  - `B-0061-api-falsification`: `GET /projects/:id` returns archived project.
  - `B-0062-cli-positive`: `cli list-projects` excludes archived.
  - `B-0062-cli-falsification`: `cli list-projects --include-archived` includes archived.

### 11.2 EvaluationContract (built at PROVISIONING)

- `local_ci.threshold` = `"rc=0"`, command = `"bash scripts/test.sh"`.
- `rubric.threshold` = `"every category >= 7"`, categories = (e.g.) `[code-quality, edge-case-handling, test-coverage, schema-discipline, observability, security]`.
- `standards.source_text` = full text of `docs/standards/web.md`, `docs/standards/api.md`, `docs/standards/cli.md`, `docs/standards/data-model.md` (~45k chars combined; under 60k cap).
- `behavior.source_text` = the 6 expected_evidence ids rendered as a witness list.

### 11.3 Planner output (6 subtasks + Z-99)

| id | title | rubric_targets (cat, score) | standards_referenced | behavior_evidence_advanced |
|---|---|---|---|---|
| S-01-schema | Add `archived_at` column + index | (schema-discipline, 9), (data-model, 8) | data-model.md§archival | — |
| S-02-domain | Domain-layer archival service + repository filter | (code-quality, 8), (edge-case-handling, 8) | data-model.md§soft-delete | — |
| S-03-api | API endpoints + filter behavior | (code-quality, 8), (security, 7) | api.md§filtering | B-0061-api-positive, B-0061-api-falsification |
| S-04-web | Web list view filter + retain detail-view access | (code-quality, 8), (edge-case-handling, 8) | web.md§list-views | B-0061-web-positive, B-0061-web-falsification |
| S-05-cli | CLI list-projects filter + `--include-archived` flag | (code-quality, 8), (observability, 7) | cli.md§flags | B-0062-cli-positive, B-0062-cli-falsification |
| S-06-tests | Cross-cutting test coverage | (test-coverage, 9) | — | — |
| Z-99 | Stabilize spec gate | all categories @ min_score | — | — |

`gauntlet_strategy` excerpt:

> Cycle-1 strategy: schema (S-01) lands first to give every interface a single source of archival truth, eliminating the standards-drift risk of three different archival predicates. The domain service (S-02) centralizes filter logic so the api/web/cli surfaces (S-03/04/05) become thin adapters — each one's witness is a thin trace through one well-tested filter, not three independent reimplementations. S-06 owns the holistic test-coverage rubric. Z-99 runs the spec gate after all six and patches any cross-subtask integration drift before audit.

### 11.4 Validators

- `validate_rubric_coverage`: every category appears in at least one subtask's `rubric_targets`. PASS (Z-99 covers all by construction; the surface subtasks add specificity).
- `validate_evidence_partition`: 6 evidence ids, each appearing in exactly one subtask. PASS.
- `validate_standards_paths`: 4 doc paths, all exist. PASS.

### 11.5 Per-subtask loop (S-04-web as example)

1. Doer reads targeted contract: rubric (code-quality, edge-case-handling), standards (web.md§list-views), witnesses (B-0061-web-positive, B-0061-web-falsification), local_ci card.
2. Doer codes the filter; emits SELF_AUDIT:
   ```
   gate_local_ci: rc=0 (cmd: bash scripts/test.sh)
   gate_rubric:
     code-quality: predicted_score=8  rationale: filter goes through DomainService, no duplication  evidence: web/projects/list.tsx:L42
     edge-case-handling: predicted_score=8  rationale: explicit branch for archived-but-id-requested  evidence: web/projects/[id].tsx:L18
   gate_standards:
     web.md§list-views: aligned (cite ¶3 "list views default to non-archived")
   gate_behavior:
     B-0061-web-positive: witnessed_by=npm run test:e2e -- list-excludes-archived  output_excerpt=PASS (1.2s)
     B-0061-web-falsification: witnessed_by=npm run test:e2e -- detail-view-still-works  output_excerpt=PASS (0.9s)
   diff_reconcile:
     web/projects/list.tsx: in_lane
     web/projects/[id].tsx: in_lane
     web/projects/__tests__/archival.spec.tsx: in_lane
   ```
3. Parser succeeds. Short-circuit: all scores >= min, no RISK/STUB. PROCEED.
4. Worker runs witnesses: B-0061-web-positive rc=0, B-0061-web-falsification rc=0. `witness_results` populated.
5. LLM checker (different model, adversarial) verifies: per-rubric-target PASS, per-standards-ref PASS, per-behavior-witness PASS. `overall=PASS`.
6. Subtask commits.

### 11.6 Z-99

- Runs spec gate (`bash scripts/test.sh`). If green, empty-staging is the legitimate success path.
- If red, Z-99 fixes whatever cross-subtask integration broke (e.g. a migration ordering issue between S-01 and S-02).

### 11.7 Pre-PR audit cycle 1

- `local_ci`: rc=0 (Z-99 already verified). Confidence: 95%+.
- `rubric`: every category >= 7. Confidence: ~75% (the LLM grader is the noise floor; rubric_targets give the planner a way to pre-shape the bar but the audit grader is independent).
- `standards`: no drift from cited sections. Confidence: ~80% (subtasks pinned the relevant sections; Z-99 didn't introduce drift).
- `behavior`: all 6 witnesses pass. Confidence: ~90% (witnesses ran clean per-subtask).
- **Joint cycle-1 pass probability: ~50-65%.** Honest read; Section 14.

---

## 12. PR sequencing + migration

### 12.1 PR-A (~700 LoC)

Files added/modified:
- **NEW** `quikode/evaluation_contract.py`
- **NEW** `prompts/_evaluation_context.md.j2`
- **MOD** `quikode/subtask_schema.py` — schema deltas (Section 4)
- **MOD** `prompts/planner.md` — full rewrite (Section 5.1)
- **MOD** `prompts/merge-planner.md` — full rewrite (Section 5.6)
- **MOD** `quikode/workers/task_worker.py` — build/persist contract at task pickup
- **MOD** `quikode/workers/subtasks.py` — pass contract; new validators
- **MOD** `quikode/workers/merge_node_worker.py` — contract for merge nodes
- **MOD** `quikode/prompts.py` — `planner_prompt`/`merge_planner_prompt` take `contract`; remove any `render_scope_review` if present
- **DEL** `quikode/scope_review.py`
- **DEL** `prompts/scope-review.md`
- **DEL** `tests/test_scope_review.py`
- **MOD** `quikode/worktree.py` — remove `lane_review_fn` parameter
- **MOD** `quikode/workers/subtask_completion.py` — remove `_apply_lane_review`
- **MOD** test files in §10.3 — remove scope-review assertions
- **NEW** `tests/test_evaluation_contract.py` — contract construction, persistence round-trip, render variants
- **NEW** `tests/test_planner_validators.py` — rubric coverage, evidence partition, standards paths

Acceptance: all four ladder stages green.

### 12.2 PR-B (~600 LoC)

Files added/modified:
- **MOD** `prompts/subtask-doer.md` — full rewrite (Section 5.2)
- **NEW** `quikode/self_audit.py` — deterministic parser + short-circuit
- **MOD** `prompts/subtask-checker.md` — full rewrite (Section 5.3)
- **MOD** `prompts/subtask-triage.md` — shrunk rewrite (Section 5.4)
- **MOD** `prompts/fixup-planner.md` — full rewrite (Section 5.5)
- **MOD** `quikode/workers/subtask_execution.py` — parser integration, short-circuit, witness runner
- **MOD** `quikode/prompts.py` — render functions take `contract`, `parsed_self_audit`, `witness_results` where appropriate
- **MOD** `quikode/pre_pr_audit.py` — load contract from per-task artifact (no rebuild)
- **NEW** `tests/test_self_audit.py` — parser unit tests, short-circuit semantics, re-prompt loop
- **NEW** `tests/test_witness_runner.py` — runtime caps, container isolation
- **NEW** `tests/test_subtask_loop_integration.py` — end-to-end smoke using the R-0050 fixture from Section 11

Acceptance: all four ladder stages green.

### 12.3 Migration (after PR-B merges and reinstall)

Hard cutover. No backwards compatibility.

1. `qk daemon stop` (already done at the user's direction).
2. Mass wipe:
   ```
   qk briefing --json | jq -r '.tasks[] | select(.state != "merged") | .id' \
     | xargs -I {} bash -c 'qk abort {} ; qk retry {}'
   ```
   Targets non-merged tasks INCLUDING `kind="merge"` rows (D11). The `qk abort` step handles non-terminal states cleanly so retry can proceed.
3. `bash scripts/reinstall.sh --skip-tests`.
4. `qk daemon start --detach --max-parallel 12 --retry-failed`.
5. Watch first 5-10 plans land. Read each `gauntlet_strategy` + cycle-1 audit outcome. Calibrate.

---

## 13. Open questions / known unknowns (tactical)

1. **SELF_AUDIT parser strictness on whitespace and indentation.** YAML-like but not YAML. Decision for the implementer: tolerate trailing whitespace and 2/4-space indent variations; reject missing required keys. Document the exact tolerance grammar in `quikode/self_audit.py`'s docstring. Suggested: a hand-rolled line-oriented parser, not YAML — the `<...>` placeholders in literal output text would confuse YAML.
2. **Witness runtime cap interaction with slow integration tests.** 30s/subtask cap is appropriate for unit-shaped witnesses; some BDD witnesses may legitimately need 60s+. Decision for the implementer: per-witness cap should be configurable via `cfg.subtask_witness_timeout_seconds` (default 15) and the per-subtask total cap derived as `2 * len(behavior_evidence_advanced) * per_witness_cap`. Add a config field, document the default, leave room for tuning.
3. **What counts as a "category-advancing" diff for the planner's `rubric_targets`.** This is judgmental. Implementer should bias toward "the planner declares what they intend; the checker verifies whether the diff supports that intent." If the planner declares `(security, 8)` for a subtask whose diff adds no security-relevant code, the checker will mark it FAIL — that's working as intended.
4. **Standards-doc 60k cap when standards genuinely exceed 60k.** Implementer should emit a WARN, truncate at line boundaries (not mid-sentence), and add a marker line `[STANDARDS DOC TRUNCATED at line N of M]`. Long-term fix is RAG/index, but punt that to a follow-up plan.
5. **Triage failure_layer when both rubric and behavior fail.** Pick the most severe; rubric > behavior > standards > local_ci > self_audit_mismatch > transport (severity = how upstream the gap is). Implementer can encode this as an enum ordering.

---

## 14. Confidence calibration

Honest read on what's high-confidence vs. speculative.

**High confidence (the design):**
- The diagnosis is correct: information-architecture mismatch is the root cause of the zero-PR week. The recurring BLOCK patterns (R-0010 disclaim, R-0008 simulated-BDD, R-0024 scope-loop) all share the shape "agent X couldn't see what agent Y was going to grade them on."
- The single-shared-`EvaluationContract` abstraction is the right shape — alternatives (rebuild per render, three separate contracts, etc.) all have known failure modes that this avoids.
- The SELF_AUDIT block + deterministic short-circuit is the right shape: it gives the doer a structured surface to commit to, and gives the worker a fast-fail path for the obvious cases.
- The senior-engineer-tutoring-junior triage framing is the right shape: it's what good code review looks like, applied to the LLM-loop.
- Scope-review retirement is correct: the new structure (rubric_targets + behavior_evidence_advanced + planner gauntlet_strategy) makes the "is this file in lane" question moot — every file is in lane if it advances some category or fixes some gate, and that's verified by the checker against the SELF_AUDIT, not by a separate adjudicator.

**Speculative (the numbers):**
- Cycle-1 pass-probability of 50-65% in Section 11 is an educated estimate, not a measurement.
- The 4 stages' bar-clearing rates depend on calibration that has not happened yet — specifically how strictly the rubric grader (already an LLM) reads against the new pre-shaped subtask claims, vs. how strictly it read against the old free-form diffs. The bar may need to soften (rubric_min_score 7 → 6) or harden (8) once we see the first 10 cycle-1 outcomes.
- The witness-runtime caps in Section 7.2 (15s/witness, 30s/subtask) are guesses calibrated against typical npm-test-e2e shapes; they may be too tight for BDD-heavier suites.
- The gauntlet_strategy field is novel — I'm betting that asking the planner to articulate cycle-1 strategy in prose will sharpen the subtask decomposition, but the only way to know is to read the first 10 of them.

**The DIRECTION (rubric-first information architecture) is high-confidence. The EXACT cycle-1 numbers are speculative.** First 5-10 plans after migration are the calibration window.

---

## Critical files for implementation

- /home/trevor/github/quikode/quikode/evaluation_contract.py
- /home/trevor/github/quikode/quikode/subtask_schema.py
- /home/trevor/github/quikode/quikode/self_audit.py
- /home/trevor/github/quikode/quikode/workers/subtask_execution.py
- /home/trevor/github/quikode/prompts/_evaluation_context.md.j2
