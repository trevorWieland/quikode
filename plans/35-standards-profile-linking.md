# Plan 35 — standards-profile + architecture-alignment linking (additive split, not replacement)

## 0. Status — DO NOT SHIP IN ISOLATION

**Queued; deliberately deferred.** As of 2026-05-08 the first wave of plan-33 tasks is in flight under the current four-stage gauntlet. The standards-vs-architecture misclassification described below is real but not yet known to be the bottleneck — and shipping plan 35 forces a mass `qk retry` (Plan 33 D11 hard-cutover stance), discarding every in-flight plan and worktree.

**Ship-trigger conditions** (any of):

1. The user is rejecting tasks at human review for reasons that trace back to the standards-vs-architecture conflation (e.g., the standards audit is flagging architecture-shaped concerns that should have been caught by a dedicated architecture pass, or vice versa — diffs landing with cross-subsystem coupling that no audit caught).
2. We've identified at least one OTHER plan that also needs a mass `qk retry` to deploy. Plan 35 then rides along under a shared reset, amortizing the retry cost across all queued schema/prompt changes.
3. The current run successfully reaches human-review-ready and validates the post-PR FSM (merges, stacked-diff cascade, parent-tip rebase, multi-parent merge nodes) — at which point we know the upstream pipeline is sound and any remaining issues are localized to planner-prompt quality, where plan 35's bucket separation gives the planner clearer constraints.

**Until a ship-trigger lands, this plan stays queued.** The current system gets to run on its own merits; if it carries 233 tanren tasks to human review without bucket-confusion-driven failures, plan 35 may be deferred indefinitely or restructured in light of what we learned. If retries become necessary for any other reason, batch this in.

## 1. Diagnosis

R-0002's planner emitted `standards_referenced` entries pointing at tanren's **system-architecture** docs (`docs/architecture/subsystems/identity-policy.md§Permissions`, `…/interfaces.md§Error Taxonomy`). Reading the chain end-to-end, that behavior is not random — it's the only path the wiring offers, and the architecture-alignment work it represents is real and load-bearing. The bug is misclassification, not invention.

The chain:

- **`quikode/config.py:445`** — default `pre_pr_standards_profile_globs = ["docs/standards/**/*.md", "docs/architecture/**/*.md", "AGENTS.md", "CONTRIBUTING.md"]`. Tanren has nothing under `docs/standards/`; it does have a fat `docs/architecture/subsystems/` tree. The default glob therefore loads architecture-of-the-system-being-built as if it were standards.
- **`quikode/evaluation_contract.py:_gather_standards_text` (~L424)** — globs the matched files into one 60k-cap blob, stuffs it into `contract.standards.source_text`.
- **`prompts/_evaluation_context.md.j2:_stage_block`** — emits that blob verbatim under "Source text (canonical)".
- **`prompts/planner.md:75,97`** — instructs the planner that `standards_referenced[].doc_path` is "a repo-relative path to a standards doc that exists at planning time" and offers the worked example `docs/standards/web.md§list-views`. With nothing under `docs/standards/` and a 60k blob full of `docs/architecture/subsystems/*.md`, the planner picks the only paths it sees.
- **`quikode/planner_validators.py:validate_standards_paths` (~L240)** — only checks `Path(p).is_absolute()` is false and `(repo_root / p).resolve().is_file()`. It cannot tell a profile doc from a system-architecture doc; it cannot tell a standards doc from `README.md`.
- **`quikode/pre_pr_audit.py:collect_standards_text` (~L256)** — at audit time, prefers `contract.standards.source_text` (same blob) and otherwise re-globs the same patterns. So the audit grades the diff against architecture docs too.
- **`prompts/pre-pr-standards.md`** — the preamble describes a standards profile ("module/crate boundaries, naming conventions, layout rules, deprecated patterns…"). The text it actually receives is `tanren/docs/architecture/subsystems/*.md`. The grader is hallucinating standards out of architecture prose, charitably.
- **Downstream prompts** (`subtask-doer.md`, `subtask-checker.md`, `subtask-triage.md`, `fixup-planner.md`) only echo what the planner cited — they have no independent profile-aware view.

Critically, **two distinct doc kinds are being conflated into one slot:**

- **System-architecture docs** at `tanren/docs/architecture/subsystems/*.md` — describe *the system being built*. Identity-policy semantics, error taxonomy, interface contracts, observation pipeline. Load-bearing for "did this subtask honor the cross-subsystem contract?"
- **Standards-profile docs** at `tanren/profiles/{rust-cargo,react-ts-pnpm,default}/{global,rust|react|typescript,architecture,testing}/*.md` — describe *how Rust+Cargo or React+TS code should be written, on any project*. Each carries YAML frontmatter (`kind: standard`, `name`, `category`, `importance`, `applies_to`, `applies_to_languages`, `applies_to_domains`). Quikode does not load this directory at all; there is no profile loader, no frontmatter parser, no profile selection. Note: `quikode/profiles.py` is a different concept (project profile = workspace defaults like `default_image`).

Both are real bars. Both deserve a dedicated field, validator, and auditor. R-0002's mistake was to grade architecture-alignment work under a stage prompt written for cross-cutting language standards — different rubric, different doc shape, different finding taxonomy.

## 2. Design

### 2.1 Two distinct doc kinds, two slots, two auditors

Make the distinction first-class everywhere:

- **Standards profiles** (`rust-cargo`, `react-ts`, `python-uv`, …) — language/framework "how to build it" rules. Cited via `standards_referenced`. Graded by the standards audit stage. Universal across projects in that language.
- **Architecture docs** (tanren's `docs/architecture/subsystems/*.md`) — project-specific "what we're building" contracts. Cited via a new `architecture_referenced` field. Graded by a new architecture audit stage. Project-specific.

Field names: `architecture_referenced` (parallel to `standards_referenced`, semantically symmetric, same `[{doc_path, section}]` shape). Considered alternatives: `arch_alignment_referenced` (verbose), `system_design_referenced` (overloads "system"), `subsystem_pinned` (ties the name to tanren's directory layout). `architecture_referenced` wins on parallelism and on matching the audit-stage name (`architecture`).

The pre-PR gauntlet grows from four stages to five:

```
local_ci, rubric, standards, architecture, behavior
```

Ordering rationale: `architecture` slots between `standards` and `behavior` because — like standards — it grades the *diff* against canonical text, not runtime witnesses; and because both audits' findings feed the same fixup-planner shape (`{doc_path, section}` references). Behavior stays last (witness verification is the most expensive stage and runs only on diffs that already cleared the textual audits).

This is **additive** to plan 33's contract. Both audits run on every cycle. A diff that respects standards but drifts from the subsystem boundary fails on `architecture`. A diff that nails the boundary but uses `unwrap()` everywhere fails on `standards`. They cannot deputize for each other.

### 2.2 Configurable doc roots

No hardcoded paths. Two new config knobs (in `quikode/config.py`), both repo-relative, both with sensible defaults:

```python
standards_profiles_dir: Path = Field(
    default=Path("profiles"),
    description="Repo-relative directory containing standards profiles. "
                "Each subdirectory is one profile (e.g. rust-cargo, react-ts); "
                "each profile's *.md files are standards docs with YAML "
                "frontmatter (kind: standard).",
)
standards_profiles: list[str] = Field(
    default_factory=list,
    description="Names of standards profiles that apply to this workspace. "
                "Each must be a subdirectory of standards_profiles_dir. "
                "Empty list = no standards profile loaded; planner cannot "
                "cite standards refs and the standards audit reports a "
                "config_error finding.",
)
architecture_docs_dir: Path = Field(
    default=Path("docs/architecture"),
    description="Repo-relative directory containing system-architecture "
                "documentation for THIS project. The planner sees this "
                "tree as task context; the architecture audit grades the "
                "diff against it. Citations in `architecture_referenced` "
                "MUST resolve under this directory.",
)
architecture_doc_globs: list[str] = Field(
    default_factory=lambda: ["**/*.md"],
    description="Globs (relative to architecture_docs_dir) selecting which "
                "files in the architecture tree count as architecture "
                "documentation. Default: every .md file recursively.",
)
```

**Retired:** `pre_pr_standards_profile_globs` — deleted from `Config`, `config_loader.py`, `evaluation_contract.py`, `pre_pr_audit.py`. Hard cutover per Plan 33 D11. `config_loader.py` raises a friendly error when the legacy key is encountered, naming the three replacements.

Tanren's `quikode.yaml` (added by this plan; today tanren has none and relies on quikode defaults plus `--profile tanren`):

```yaml
standards_profiles_dir: profiles
standards_profiles: ["rust-cargo", "react-ts-pnpm", "default"]
architecture_docs_dir: docs/architecture
architecture_doc_globs:
  - subsystems/*.md
  - "*.md"           # top-level overview docs (assessment, planning, ...)
```

A workspace that has neither profiles nor an architecture tree fails closed with explicit operator-facing messages. Plan 35 ships seed profiles inside quikode (§2.8) so a fresh project can opt in cheaply, but does **not** ship seed architecture docs (architecture is project-specific; defaulting it would be wrong).

### 2.3 Standards-profile storage layer

New module `quikode/standards_profiles.py`:

```python
@dataclass(frozen=True)
class StandardsDoc:
    profile: str
    category: str       # frontmatter `category` (e.g. "rust", "global", "architecture", "testing")
    name: str           # frontmatter `name` (e.g. "error-handling")
    path: Path          # absolute path on disk
    repo_relative: str  # the doc_path the planner cites
    importance: Literal["low", "medium", "high", "critical"]
    applies_to: tuple[str, ...]
    applies_to_languages: tuple[str, ...]
    applies_to_domains: tuple[str, ...]
    body: str
    sections: tuple[str, ...]   # parsed `#`/`##`/`###` headings — used to validate `section`

@dataclass(frozen=True)
class StandardsProfile:
    name: str
    root: Path
    docs: tuple[StandardsDoc, ...]

def load_profiles(cfg: Config) -> tuple[StandardsProfile, ...]: ...
def find_doc(profiles, doc_path) -> StandardsDoc | None: ...
def find_section(doc, section) -> bool: ...
```

Note: tanren's profile tree happens to include a `<profile>/architecture/` *subdirectory* (e.g. `profiles/rust-cargo/architecture/crate-layering.md`) — that's a *standards* category about how to lay out crates in any Rust+Cargo project, distinct from a project's `docs/architecture/` system-design tree. The frontmatter `category: architecture` on those files is a standards-profile categorization; they remain standards docs. No collision in field naming because `architecture_referenced` only resolves under `architecture_docs_dir`, not under `standards_profiles_dir`.

Frontmatter parser: hand-rolled (the format is `^---\n` … `\n---\n`; key:value with `- ` lists). No PyYAML dependency. Malformed or missing required keys → `load_profiles` raises with the offending file path.

### 2.4 Architecture-doc storage layer

Parallel module `quikode/architecture_docs.py` — deliberately simpler than `standards_profiles.py` because architecture docs are free-form (no required frontmatter, no `applies_to` metadata):

```python
@dataclass(frozen=True)
class ArchitectureDoc:
    path: Path                # absolute
    repo_relative: str        # the doc_path the planner cites
    title: str                # parsed from first `# ` heading, or filename stem
    sections: tuple[str, ...] # `#`/`##`/`###` headings — used to validate `section`
    body: str

@dataclass(frozen=True)
class ArchitectureCorpus:
    root: Path
    docs: tuple[ArchitectureDoc, ...]

def load_architecture(cfg: Config) -> ArchitectureCorpus: ...
def find_arch_doc(corpus, doc_path) -> ArchitectureDoc | None: ...
def find_arch_section(doc, section) -> bool: ...
```

Optional frontmatter is tolerated (architecture docs *can* declare frontmatter for richer metadata, but don't have to). This module is intentionally separate from `standards_profiles` to keep the two corpora unconfusable in code as well as in prompts.

### 2.5 EvaluationContract changes

The contract grows from four stage rubrics to five. `StageRubric` stays generic for `local_ci`, `rubric`, `behavior`. Two specialized variants for the textual-audit stages:

```python
@dataclass(frozen=True)
class StandardsStageRubric:
    name: Literal["standards"] = "standards"
    one_line: str
    threshold: str
    grading_template: str
    profiles: tuple[StandardsProfile, ...]
    source_text: str   # rendered profile catalog, capped at 30k

@dataclass(frozen=True)
class ArchitectureStageRubric:
    name: Literal["architecture"] = "architecture"
    one_line: str
    threshold: str
    grading_template: str
    corpus: ArchitectureCorpus
    source_text: str   # rendered architecture-doc TOC + condensed bodies, capped at 30k

class EvaluationContract:
    task_id: str
    local_ci: StageRubric
    rubric: StageRubric
    standards: StandardsStageRubric
    architecture: ArchitectureStageRubric    # NEW
    behavior: StageRubric
```

Token budgets: each textual audit gets a 30k cap (down from 60k for the combined slot) so the planner's full-context render still fits in the same envelope. Standards audits prefer the catalog-of-headings shape (planner navigates by name, doer/checker get the cited section inlined); architecture audits inline more body in the planner render because there's no `applies_to` index for the planner to navigate by.

Build path:

- `_build_standards(cfg)` → `standards_profiles.load_profiles(cfg)`. Empty `cfg.standards_profiles` → `profiles=()` and a fail-closed `source_text`.
- `_build_architecture(cfg)` → `architecture_docs.load_architecture(cfg)`. Missing `architecture_docs_dir` (path doesn't exist or contains no matching files) → empty corpus, fail-closed `source_text`, operator-facing message in the planner prompt.

The persistence shape gains an `architecture` block alongside `standards`. JSON-serialization keeps the determinism guarantee (sorted keys, sorted profile/doc lists).

### 2.6 Schema changes (`quikode/subtask_schema.py`)

Plan 33 added `standards_referenced: tuple[StandardsRef, ...]`. Plan 35 adds the parallel field:

```python
class ArchitectureRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    doc_path: str    # repo-relative; must resolve under architecture_docs_dir
    section: str     # heading; validator confirms it exists in the doc

class Subtask(BaseModel):
    ...
    standards_referenced: tuple[StandardsRef, ...]
    architecture_referenced: tuple[ArchitectureRef, ...]   # NEW
```

Pydantic `extra="forbid"` ensures pre-plan-35 plans (no `architecture_referenced`) get rejected at parse time — same hard-cutover discipline as plan 33.

Z-99 stabilization subtask gains `architecture_referenced=()` by construction (parallel to its empty `standards_referenced`). Z-99 is exempt from architecture-coverage checks (it's the holistic-pass guardian; specificity comes from earlier subtasks).

### 2.7 Validator additions

`quikode/planner_validators.py`:

- **`validate_standards_refs(plan, contract)`** (renamed from `validate_standards_paths`). Three checks per cited entry:
  1. `find_doc(contract.standards.profiles, ref.doc_path)` resolves to a real `StandardsDoc` — otherwise: *"doc_path X does not live under any configured standards profile (loaded: {names}). Standards refs MUST cite profile docs (e.g. profiles/rust-cargo/rust/error-handling.md), not architecture or feature documentation. If you meant to cite a project-architecture doc, use `architecture_referenced` instead."*
  2. The matched doc's frontmatter `kind` is exactly `"standard"`.
  3. `find_section(doc, ref.section)` returns true.

- **`validate_architecture_refs(plan, contract)`** (NEW). Three checks per cited entry:
  1. `find_arch_doc(contract.architecture.corpus, ref.doc_path)` resolves to a real `ArchitectureDoc` — otherwise: *"doc_path X does not live under the configured architecture_docs_dir ({root}). Architecture refs MUST cite project-architecture docs, not standards profiles or feature documentation."*
  2. Path lives under `cfg.architecture_docs_dir` (defensive — `find_arch_doc` already enforces this; the explicit check produces a friendlier error message when the planner cites a doc outside the configured root).
  3. `find_arch_section(doc, ref.section)` returns true.

Both validators share the same failure-aggregation pattern (list every problem in one re-prompt). Both feed the planner's existing max-2-re-prompt budget (Plan 33 D3).

There is **no** validator that *requires* every subtask to declare an `architecture_referenced` — many subtasks (test-only, infrastructure, generic refactor) legitimately have none. Same posture as `standards_referenced`. The audits themselves catch "diff drifts from architecture but no subtask cited the relevant subsystem" via the unreferenced-applicable mechanism (§2.10).

### 2.8 Profile content (starter sketch — standards only)

Tanren's profiles are content-complete; plan 35 does not author them. For workspaces lacking profiles, ship two **starter** profiles inside quikode at `quikode/standards_profiles_seed/`:

```
quikode/standards_profiles_seed/
  rust-cargo/
    global/
      cargo-or-just-ci-gate.md         — single CI command, rc=0
      dependency-management.md         — version pinning, no path overrides in published crates
    rust/
      error-handling.md                — thiserror in libs, anyhow in bins, no panic/unwrap/expect
      naming-conventions.md            — snake_case fns, PascalCase types, SCREAMING_SNAKE consts
      file-and-function-limits.md      — 500-line file cap, function complexity ceiling
      no-unsafe-default.md             — unsafe requires SAFETY comment + scoped justification
    testing/
      three-tier-test-structure.md     — unit / integration / bdd partitioning
      no-test-skipping.md              — #[ignore], skip!() forbidden in committed code
      mock-boundaries.md               — mock at crate boundaries, never within
  react-ts/
    global/
      strict-linting-gate.md           — eslint + tsc must pass on commit
      dependency-management.md         — pnpm; lockfile committed; no caret on majors
    typescript/
      strict-compiler-config.md        — strict: true, noUncheckedIndexedAccess: true
      no-any.md                        — no `any`; use `unknown` + narrowing
      explicit-return-types.md         — exported fns must declare return types
      discriminated-unions.md          — tagged unions over inheritance
    react/
      functional-components-only.md    — no class components in new code
      hook-conventions.md              — rules-of-hooks; custom hooks prefix `use`
      accessibility-enforcement.md     — eslint-plugin-jsx-a11y warnings as errors
    testing/
      three-tier-test-structure.md     — unit / component / e2e
      mock-boundaries.md
      no-test-skipping.md
```

Each file: same frontmatter shape tanren uses (`kind: standard`, `name`, `category`, `importance`, `applies_to`, `applies_to_languages`, `applies_to_domains`). Plan 35 ships **headers + 3-5 line stub bodies + one "Rules" bullet list** per file — full prose authoring deferred to plan 36+; the structure must be in place for tests and for fresh-project bootstrapping. New CLI `qk standards seed --to <path>` copies the seed into an operator's repo so they can fork-and-edit.

No seed for architecture docs — architecture is project-specific; seeding it would be wrong.

### 2.9 Prompt changes

`prompts/_evaluation_context.md.j2` — three changes to `ec_full`:

1. The "standards stage" block emits a **profile catalog** (per-profile header → per-doc bullet listing `repo_relative` path + `applies_to_languages` + section names) instead of full bodies. Dense (~150 chars per doc); planner navigates by name and reads bodies via tool calls when needed.
2. **NEW** "architecture stage" block: TOC of architecture docs (path → first heading) plus a per-subsystem one-line summary parsed from the doc's first non-heading paragraph. Truncate-with-marker at 30k chars.
3. Existing `ec_targeted(contract, subtask)` macro upgraded: when rendering a `standards_referenced[]` entry, **inlines the full body of the matching `StandardsDoc.section`**; same treatment for `architecture_referenced[]` against the matching `ArchitectureDoc.section`. This is the fix to "downstream agents only echo the planner's citations" — they now see the actual rule prose / contract text at the cited section.

`prompts/planner.md`:

- §2.5 (NEW) — coverage demand: every subtask SHOULD declare `architecture_referenced` when the work touches a subsystem boundary, but no validator-level partition requirement (parallel to standards). Worked example expanded:
  ```jsonc
  "standards_referenced": [
    { "doc_path": "profiles/rust-cargo/rust/error-handling.md", "section": "Rules" }
  ],
  "architecture_referenced": [
    { "doc_path": "docs/architecture/subsystems/identity-policy.md", "section": "Permissions" }
  ]
  ```
- The §3 "What each subtask must declare" gains the new field; the §7 output schema adds it.
- Hard rule added: *"`standards_referenced` cites only standards profile docs (under standards_profiles_dir). `architecture_referenced` cites only project-architecture docs (under architecture_docs_dir). The validators reject the wrong-bucket placement with a re-prompt; if you mis-route on retry-2, the plan BLOCKs."*

`prompts/pre-pr-standards.md` — jinja vars become `{{ profile_catalog }}` + `{{ standards_refs_in_diff }}` (cited sections inlined). Preamble updated: "you grade against pinned profile passages — language/framework standards — not project-specific architecture (that's the architecture audit's job)." `standards_doc_ref` field renamed in spec to `profile_doc_ref` to match.

`prompts/pre-pr-architecture.md` (NEW) — parallel structure to `pre-pr-standards.md` but framed for system-architecture grading:
- Preamble: "you grade the diff against this project's documented subsystem contracts and architecture. Misalignment with module boundaries, undocumented cross-subsystem coupling, deviations from documented interface contracts, missing telemetry the architecture mandates."
- Out-of-scope explicitly enumerates the other four stages (mirrors `pre-pr-standards.md`'s discipline). Cross-link: "language/framework standards (e.g. `unwrap()` usage, `any` typing) belong to the standards audit, not here."
- Same JSON envelope as `pre-pr-standards.md`, with `architecture_doc_ref` instead of `standards_doc_ref`.

Doer/checker/triage/fixup-planner pick up the upgraded `ec_targeted` macro and the new `architecture_referenced` field automatically — no per-prompt rewrite needed beyond the macro change.

### 2.10 Architecture audit pipeline

`quikode/pre_pr_audit.py` gains `run_architecture_audit(...)` parallel to `run_standards_audit(...)`:

- Renders `pre-pr-architecture.md` with `{{ architecture_corpus }}` (the rendered TOC + condensed bodies) + `{{ architecture_refs_in_diff }}` (the union of `architecture_referenced` across the task's subtasks, with cited section bodies inlined) + the diff.
- Same agent role as standards audit (`cfg.triage`, claude-opus-class) — same structural-reasoning load.
- Output envelope: `{ findings: [...], overall_assessment: "..." }`. Each finding carries `architecture_doc_ref`.
- Severity calibration mirrors standards audit but with architecture-specific examples in the prompt (cross-subsystem coupling = high, missing required telemetry per subsystem doc = high, naming drift from the subsystem's stated convention = medium).
- **Unreferenced-applicable detection** for both audits: when the diff touches a file matching a profile doc's `applies_to` glob but no subtask cited it → finding `unreferenced-applicable-standard` (severity medium). Same idea for architecture: when the diff touches a path under `crates/<X>/` and a subsystem doc at `docs/architecture/subsystems/<X>.md` exists but no subtask cited it → finding `unreferenced-applicable-architecture` (severity medium). Path-to-subsystem heuristic is configurable via an optional `architecture_path_map: dict[glob, doc_path]` field on Config (default empty; tanren can populate it post-deploy).

Audit-cycle integration in `quikode/workers/pre_pr.py`: stages run in declared order (`local_ci, rubric, standards, architecture, behavior`). Each stage's outcome contributes to the same `pre_pr_audit_summary` shape; fixup-planner sees both `standards:*` and `architecture:*` finding namespaces and dispatches via the existing finding-coverage validator (which already supports namespace dispatch — `validate_finding_coverage` in `planner_validators.py:271` — and gains an `architecture:` case in the same shape as `standards:`).

### 2.11 Hard-cutover semantics (Plan 33 D11 stance preserved)

- `pre_pr_standards_profile_globs` removed; `config_loader.py` raises on the legacy key.
- `validate_standards_paths` → `validate_standards_refs`; rejects any path not under a loaded profile, regardless of whether the file exists. Architecture-doc citations in pre-existing in-flight plans will fail validation on resume — no auto-routing shim.
- The planner cannot mis-route. If an architecture doc lands in `standards_referenced`, `validate_standards_refs` re-prompts with the bucket-correction message. Two re-prompts; then BLOCK.
- Pydantic `extra="forbid"` rejects pre-plan-35 plans missing `architecture_referenced`.
- Mass `qk retry` at deploy on every non-merged task, including `kind="merge"` rows.

## 3. Concrete file list

**Modify:**
- `quikode/config.py` — add `standards_profiles_dir`, `standards_profiles`, `architecture_docs_dir`, `architecture_doc_globs`, `architecture_path_map`; remove `pre_pr_standards_profile_globs`.
- `quikode/config_loader.py` — wire new fields; raise on legacy key.
- `quikode/evaluation_contract.py` — `_build_standards` calls `standards_profiles.load_profiles`; `_build_architecture` calls `architecture_docs.load_architecture`; `EvaluationContract.architecture` field; persistence shape gains the `architecture` block; serialization stable.
- `quikode/subtask_schema.py` — add `ArchitectureRef`; add `Subtask.architecture_referenced`; Z-99 builder adds `architecture_referenced=()`.
- `quikode/planner_validators.py` — rename `validate_standards_paths` → `validate_standards_refs`; profile-membership + section check; new `validate_architecture_refs`; `_classify_finding_coverage` gains `architecture:` namespace.
- `quikode/pre_pr_audit.py` — `collect_standards_text` retired; `run_standards_audit` switches to profile catalog + cited sections; new `run_architecture_audit` parallel function.
- `quikode/workers/pre_pr.py` — five-stage pipeline; `pre_pr_audit_summary` shape gains `architecture` block.
- `quikode/workers/subtasks.py` — wire `validate_architecture_refs` into the planner-driver loop alongside the existing validators.
- `prompts/_evaluation_context.md.j2` — standards block becomes profile catalog; new architecture block; `ec_targeted` inlines cited section bodies for both standards and architecture refs.
- `prompts/planner.md` — coverage demands include architecture; worked example covers both fields; output schema gains `architecture_referenced`; bucket-routing hard rule added.
- `prompts/merge-planner.md`, `prompts/fixup-planner.md`, `prompts/subtask-doer.md`, `prompts/subtask-checker.md`, `prompts/subtask-triage.md` — pick up the upgraded `ec_targeted` automatically; verify in tests.
- `prompts/pre-pr-standards.md` — jinja vars + preamble update.
- `quikode/cli_commands/` — new `qk standards seed --to <path>` command.

**New:**
- `quikode/standards_profiles.py` — profile loader, `StandardsDoc`/`StandardsProfile`, frontmatter parser, `find_doc`/`find_section`.
- `quikode/architecture_docs.py` — architecture-doc loader, `ArchitectureDoc`/`ArchitectureCorpus`, `find_arch_doc`/`find_arch_section`.
- `prompts/pre-pr-architecture.md` — new audit prompt parallel to `pre-pr-standards.md`.
- `quikode/standards_profiles_seed/rust-cargo/...`, `quikode/standards_profiles_seed/react-ts/...` — seed profile content per §2.8.
- `tests/test_standards_profiles.py` — frontmatter parsing, `find_doc`, `find_section`, malformed-frontmatter raises with file path.
- `tests/test_architecture_docs.py` — corpus loading, section parsing, fail-closed on missing dir.
- `tests/test_planner_validators_refs.py` — accepts profile-doc citations in `standards_referenced` and architecture-doc citations in `architecture_referenced`; rejects each in the wrong bucket with the bucket-correction message; rejects unknown sections.
- `tests/test_evaluation_contract_five_stage.py` — build/persist/load round-trip with all five stages; architecture corpus stable.
- `tests/test_pre_pr_architecture_audit.py` — render with new vars; `unreferenced-applicable-architecture` finding fires when `architecture_path_map` is populated.
- `tests/test_pre_pr_standards_audit.py` — analogous, plus `unreferenced-applicable-standard`.

## 4. PR sizing

**Two PRs.** Plan 33 split when the LoC and the semantic seam justified it; plan 35's seam is similar.

**PR-A (~700 LoC):** schema + contract + loaders + validators + planner-side prompt changes.
- All `quikode/`-side modifications (config, subtask_schema, evaluation_contract, planner_validators, standards_profiles.py, architecture_docs.py, workers/subtasks.py).
- `prompts/_evaluation_context.md.j2` upgraded.
- `prompts/planner.md` rewritten for the dual-bucket field set.
- Seed profiles shipped.
- Tests: standards_profiles, architecture_docs, planner_validators, evaluation_contract.
- Acceptance: planner produces five-stage-aware plans; the audit pipeline still runs four stages (architecture audit not yet wired); `validate_architecture_refs` rejects wrong-bucket placements; ladder green.

**PR-B (~500 LoC):** audit-side wiring + architecture audit prompt + downstream prompt-render verification.
- `prompts/pre-pr-architecture.md` (new), `prompts/pre-pr-standards.md` (rewrite).
- `quikode/pre_pr_audit.py` — `run_architecture_audit`, retargeted `run_standards_audit`.
- `quikode/workers/pre_pr.py` — five-stage pipeline.
- `prompts/fixup-planner.md` — finding-coverage validation extended for `architecture:` namespace (mostly automatic via `_classify_finding_coverage`).
- Tests: pre_pr_architecture_audit, pre_pr_standards_audit, end-to-end five-stage smoke.
- Acceptance: full five-stage audit runs; both unreferenced-applicable findings fire correctly on test fixtures; ladder green.

Rationale for split: PR-A is shippable on its own (the new field exists, the planner uses it, the validator gates it; only the audit doesn't yet read it). PR-B layers on the audit without re-touching the schema or contract. Mirrors plan 33's "structure first, runtime second" sequencing. Single-PR alternative would be ~1300 LoC of schema + audit + prompts touching one concept; the diff review surface argues for the split.

Migration (after PR-B): mass `qk retry` per Plan 33 D11.

## 5. Validation ladder + new tests

`ruff check` + `ruff format --check` + `ty check` + `pytest tests/ -q` all green at each PR.

New test coverage targets (consolidating §3's list):
1. Frontmatter parser handles tanren's actual profile files round-trip.
2. `StandardsDoc.sections` / `ArchitectureDoc.sections` parse `#`/`##`/`###` correctly.
3. `find_doc` returns `None` for any path outside loaded profiles, including architecture-doc paths.
4. `find_arch_doc` returns `None` for any path outside `architecture_docs_dir`, including profile-doc paths.
5. `validate_standards_refs` rejects R-0002's exact citation (`docs/architecture/subsystems/identity-policy.md§Permissions`) with the bucket-correction message.
6. `validate_architecture_refs` accepts the same citation when it appears in `architecture_referenced`.
7. `validate_standards_refs` accepts `profiles/rust-cargo/rust/error-handling.md§Rules`.
8. `EvaluationContract` build → persist → load yields equal corpora across all five stages.
9. Five-stage audit pipeline runs in order; an architecture-failing diff doesn't get its findings duplicated by the standards audit.
10. `unreferenced-applicable-architecture` fires when `architecture_path_map` says `crates/identity-policy/**` → `docs/architecture/subsystems/identity-policy.md`, the diff touches that path, and no subtask cited the doc.

Test fixtures live under `tests/fixtures/standards_profiles/` and `tests/fixtures/architecture_docs/` mirroring the relevant trees.

## 6. Confidence calibration

**High confidence:** The two-bucket diagnosis fits an existing artifact on disk (tanren's `profiles/` tree and `docs/architecture/` tree). The R-0002 misclassification is unambiguous. The validator + audit symmetry is mechanical. Plan 33's `_classify_finding_coverage` already supports namespace dispatch — adding `architecture:` is a one-line extension.

**Medium confidence:** Whether the planner can reliably bucket-route on first emit when both fields are equally available. Mitigation: the worked example in `planner.md` shows both fields side-by-side with concrete tanren-shaped citations; the bucket-correction validator message is explicit; max-2 re-prompts allows a recovery cycle. The `unreferenced-applicable` mechanism for both audits is novel and could over-fire on benign refactors. Mitigation: ship at severity `medium`, calibrate after first 5-10 cycle outcomes, drop to `low` if noise floor is too high.

**Speculative:** Whether five sequential audit stages stays inside the operator's tolerance for cycle latency (today's four stages already dominate cycle time on rubric+standards+behavior). Mitigation: architecture audit is structurally identical to standards audit and reuses the same agent role; expected wall-clock add is ~one standards-audit duration per cycle. If this becomes a budget pressure, a follow-up plan can parallelize the textual audits (standards + architecture in parallel — they don't share state).

**The DIRECTION (separate fields, separate auditors, configurable doc roots) is high-confidence.** The exact unreferenced-applicable severity calibration is a tuning knob to set after first observations.
