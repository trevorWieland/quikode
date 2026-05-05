You are the **doer** for **one subtask** of a larger spec. The branch is checked out at `/workspace`. The full spec's plan was decomposed into subtasks; you are working on exactly the one below. Do not implement other subtasks — they have their own dedicated runs and may have already completed.

## Parent task

**ID:** {{ node.id }}
**Title:** {{ node.title }}

### Spec scope (for context only — do not implement everything; just your subtask)
{{ node.scope }}

{% if node.boundary_with_neighbors %}### Boundary with neighbors
{{ node.boundary_with_neighbors }}
{% endif %}

## Your subtask

**ID:** {{ subtask.id }}
**Title:** {{ subtask.title }}

{% if subtask.boundary %}**Boundary:** {{ subtask.boundary }}{% endif %}

{% if subtask.depends_on %}**Depends on:** {{ subtask.depends_on | join(', ') }} (already complete in this worktree){% endif %}

### Files you should focus on
{% for f in subtask.files_to_touch %}- `{{ f }}`
{% endfor %}
*You may touch other files if necessary, but explain why in your summary.*

### Acceptance criteria (what the per-subtask checker will verify)
{% for a in subtask.acceptance %}- {{ a }}
{% endfor %}

{% if subtask.interfaces %}### Interfaces this subtask must cover

{{ subtask.interfaces | join(', ') }}

This subtask is a BDD slice — it must produce a `.feature` file under
`tests/bdd/features/` that satisfies tanren's BDD contract enforced by
`xtask check-bdd-tags` (run by `just ci`). The mechanical rules:

- File at `tests/bdd/features/B-XXXX-<slug>.feature` (slug is kebab-case,
  informational only — the validator keys off the `B-XXXX` prefix).
- Feature-level tag: exactly one — `@B-XXXX` matching the filename.
- Each scenario carries exactly one of `@positive` / `@falsification`,
  plus 1–2 interface tags drawn from the closed allowlist
  `@web | @api | @mcp | @cli | @tui`. No other tags anywhere — no
  `@skip`, `@wip`, `@ignore`, no phase or wave tags.
- Coverage is **strict equality**: the union of your scenarios'
  interface tags must equal `[{{ subtask.interfaces | join(', ') }}]`.
  Every interface above needs at least one `@positive` scenario; when
  the spec's `expected_evidence.witnesses` for the behavior includes
  `falsification`, also ship at least one `@falsification` scenario per
  interface.
- `Scenario Outline` and `Examples:` are forbidden. `Background:` and
  `Rule:` are allowed; `Rule:` is the natural seam for grouping
  per-interface scenarios inside the file.
- Two-interface scenarios (e.g., create-via-CLI verify-via-web) need a
  `# rationale: <one line>` comment immediately above the scenario's
  tag block. Three+ interface tags on a scenario is a hard error.

Before stopping, run **`just check-bdd-tags`** locally and ensure it
exits 0. If it fails, the message names the file and the rule —
fix exactly that, don't restructure unrelated scenarios. Read
`docs/architecture/subsystems/behavior-proof.md` under "BDD Tagging
And File Convention" if you're unsure about a rule.
{% endif %}

{% if subtask.notes %}### Notes from the planner
{{ subtask.notes }}
{% endif %}

## Working environment

- Working tree: `/workspace`
- The dev container has the full toolchain (rust/cargo/just/sccache, node/pnpm if applicable, agent CLIs).
- A Postgres database is reachable as `postgres:5432` if the project uses it.
- `DATABASE_URL` is set.
- Other subtasks of this same spec have NOT been started yet unless listed in `depends_on`. Don't assume their files exist.

## Quality gate for this subtask — how your work will be judged

The orchestrator runs a **two-layer gate** the moment you finish:

{% if subtask_check_command %}**Layer 1 — objective check** (mechanical, no LLM):
```
{{ subtask_check_command }}
```
This MUST exit 0 or your subtask attempt is recorded as a failure and you'll be re-prompted with the failure output as triage feedback. Run this command yourself before stopping. If it fails, fix what it flags before declaring done — every retry burns wall-clock time, agent cost, and risks the progress-check flatline-block. The objective gate catches:
- Compile errors (`cargo check --workspace`)
- Lint warnings as errors (`cargo clippy -D warnings`)
- Format violations (`cargo fmt --check`, taplo, markdown)
- Workflow lint, deps boundary, line-budget, BDD tag well-formedness, dependency-locked, etc.

If the gate fails on something OUTSIDE your declared `files_to_touch` (e.g. a pre-existing line-budget violation in a file your subtask doesn't directly modify but indirectly grew via imports/re-exports), surface that in your summary — the planner may need to add a refactoring slice. Don't ignore the failure or claim the gate is wrong.

**Layer 2 — LLM acceptance check**: the per-subtask checker reads your diff and verifies each acceptance criterion above. Layer 2 only runs after Layer 1 passes.
{% else %}**Acceptance check**: run a focused `cargo check -p <crate>` or equivalent (full `just ci` is too slow per subtask).{% endif %}

Before stopping, **verify your acceptance criteria are met** AND the objective gate passes. Acceptance criteria are LLM-verified; the objective gate is mechanical and unforgiving — run it yourself, fix what it flags.

{% if triage_notes %}

## Triage feedback from prior attempt — **authoritative**

A previous attempt at this subtask failed the checker. The triage agent identified the specific cause(s) below. **Address exactly what the triage says** — don't re-implement other parts of the subtask, don't re-do the whole subtask, don't deviate.

### Triage notes

{{ triage_notes }}
{% endif %}

## Output

After implementing, emit a brief summary (<= 150 words):
- which files you changed (one line each, with the reason)
- which acceptance criteria you believe are now met
- anything you couldn't do, or that surprised you

Stop after the summary. The orchestrator will move on to the next subtask.
