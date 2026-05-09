# Plan 38 — schema-enforced JSON agent layer + role/CLI decoupling + SELF_AUDIT retirement + TUI live-state correctness

## 0. Status

**Master architectural reset.** Replaces the plan-33 SELF_AUDIT contract entirely; supersedes the original plan-38 timeout-config sketch (folded into §3.7); ships alongside plan 35 (standards-profile linking) under one mass `qk retry`. Plan 36 (the SELF_AUDIT short-circuit false-positive carve-out) becomes dead code and is deleted. Plan 37 (`qk monitor`) is independent and stays.

**Trigger:** the SELF_AUDIT contract is structurally broken because we were parsing prose out of a non-JSON-mode agent (opencode doer). Every patch — plan 36's false-positive fix, the LLM-checker mirror false-positive — is cosmetic. The architecture is wrong.

## 1. Diagnosis

### 1.1 Audit of current agent-output handling

Verified by reading the source (`quikode/agents/*.py`) and the live process tree.

| Role | CLI today | Mode today | Touches code? | How output is consumed today |
|---|---|---|---|---|
| Planner | codex | `exec --output-last-message <file>` (free text) | no | `subtask_schema.extract_json` heuristic |
| Subtask doer | opencode | `opencode run` (free text) | **yes** | `parse_self_audit` regex on prose |
| Subtask checker | codex | `--output-last-message` (free text) | no | `extract_json` heuristic |
| Subtask triage | codex | `--output-last-message` (free text) | no | `extract_json` heuristic |
| Pre-PR rubric / standards / behavior | codex | `--output-last-message` (free text) | no | three near-duplicate heuristic JSON extracts (`pre_pr_audit.py:245,381,483`) |
| Fixup planner / merge planner | codex | `--output-last-message` (free text) | no | `extract_json` heuristic |
| Conflict resolver | opencode | `opencode run` (free text) | **yes** | reads diff after |
| Progress | codex | `--output-last-message` (free text) | no | `extract_json` heuristic (`progress.py:154`) |
| Claude (where used) | claude | **`--output-format json`** ✓ | depends | proper JSON envelope parse |
| ccusage | npm | **`session --json`** ✓ | no | proper JSON parse |

**Only claude and ccusage are correct.** Every codex / opencode call that emits structured contract data is being parsed via prose heuristics. This violates the architectural rule recorded in `~/.claude/projects/-home-trevor-github-quikode/memory/feedback_no_stdout_parsing.md`.

### 1.2 What the CLIs actually support (verified at the command line, 2026-05-08)

```
claude  --output-format json --json-schema '<inline>' → {"result":"...", "structured_output":{<schema-shaped>}, "usage":{...}, "total_cost_usd":...}
codex   exec --output-schema <FILE> --json           → JSONL event stream + final assistant message constrained to schema
opencode run --format json                           → JSON event stream (message.part.delta / message.updated / step_start / session.idle)
```

Hello-world tests:
- claude: `structured_output: {"greeting":"Hello!","lucky_number":7}` (schema-validated by CLI). ✓
- codex: final assistant message `{"greeting":"Hello!","lucky_number":7}` (schema-validated by CLI). ✓
- opencode: emits JSON event stream; native schema enforcement is **client-side** via pydantic, not CLI-enforced. Still strictly better than prose parsing.

**All three CLIs support JSON output. Two of three support schema enforcement at the CLI layer.** The architecture must use these everywhere structured output is needed — there is no excuse for `--output-last-message` parsing.

### 1.3 Role-CLI coupling (the second sin)

Today the role-to-CLI assignment is hardcoded by where each agent is constructed: the planner is always codex, the subtask doer is always opencode (per `cfg.subtask_doer_cli = "opencode"`), the conflict resolver is always opencode, etc. Worse, the CLI wrapper modules (`agents/claude.py`, `agents/codex.py`, `agents/opencode.py`) each implement their own invocation pattern with **different JSON-mode shapes**, so swapping one for another is non-trivial.

**The agent role dictates the contract (input prompt + output schema + writes-files-or-not). The CLI is interchangeable.** Today the code can't honor that.

### 1.4 SELF_AUDIT specifically (plan 33's contract)

`quikode/self_audit.py` (531 LoC) parses a prose block emitted by the doer at the end of its output. The contract is fragile by construction:

- The doer is in non-JSON mode (it has to write files).
- Prose pattern can drift: the doer can put the block in a code fence, double-emit it, omit it entirely, surround it with prose, mis-indent it, etc. R-0023 / R-0003 today give us live examples of every one of those.
- `_RISK_TOKEN_RE` triggers FAIL_FAST on banned tokens that appear in **denials** (plan 36 patched one path; the LLM checker still has the same wrong reasoning at the prompt level — confirmed by R-0023 attempt 8 post-plan-36).
- The 6000-byte trailing-window storage from plan 22 means a long doer output may have its SELF_AUDIT block clipped before it's parsed.
- The SELF_AUDIT block is supposed to *replace* an LLM checker call when the deterministic short-circuit fails fast — but the checker prompt has the same false-positives, so we save nothing.

**SELF_AUDIT is a structurally bad design.** The diff itself is the evidence. A separate JSON-mode judging agent reads the diff + runs witness commands + emits structured judgment.

### 1.5 TUI staleness

User report: `R-0003 ... doing_subtask (running per-subtask doer) ... in-state 30m59s ... edit 12s` — implying the doer is running, when in fact the doer call timed out at 13:22:19, the parser failed, and the worker is now in a re-prompt cycle (or stalled). The TUI's "running per-subtask doer" string is derived solely from the FSM state being `doing_subtask`; it has no signal for "is an agent_call currently in flight." `qk tui` and `qk briefing` are both lying about whether work is happening.

The TUI must reflect reality: when the most recent agent_call has returned (rc set, duration_s set, ts persisted), the display says so, even if the FSM is still nominally in `doing_subtask`.

### 1.6 Stale config defaults (subsumed sub-piece)

`quikode/config_loader.py:101` reads each timeout knob via `int(raw.get(KEY, defaults.KEY))`. When a workspace's `config.toml` has the key, the live value pins forever — pydantic `Field(default=...)` bumps in the source code never propagate. Commit `d06cdcd` (08:15) bumped `subtask_doer_timeout_s` 1200 → 1800; live `quikode-runs/tanren/.quikode/config.toml:48` still pins `1200`; doer calls cap at 1200s (+ ~130s `subprocess.TimeoutExpired` teardown = the observed 1330–1336s rc=124 cluster). Same drift waits to bite the next Field-default bump.

## 2. Decision — architectural principles

These are durable; they govern every future agent change.

1. **Every structured-output agent runs in JSON output mode.** No `--output-last-message` parsing, no prose-shaped JSON extraction, no regex on agent stdout.
2. **The output schema is a Pydantic model owned by the role.** The role's wrapper invokes the CLI with the schema (CLI-enforced for claude/codex; client-side for opencode) and returns the validated `BaseModel` instance. A schema validation failure is a hard re-prompt (max-1) then a hard fail with a structured re-prompt — never a heuristic recovery.
3. **The agent role dictates output mode and schema. The CLI is interchangeable.** Any role × any CLI must work. The `cfg.<role>_cli` knobs become real switches operators can flip per-role.
4. **Agents that write files do not emit structured contract data.** Their `AgentResult.stdout` is bookkeeping, not evidence. The diff is the evidence. A separate JSON-mode judging agent reads the diff + runs witness commands + emits the verdict.
5. **The TUI displays observed reality.** State + agent_call liveness + worktree mtime are independent signals; the display reads each independently and never paints "running" when the latest call has returned.
6. **String parsing of agent output is forbidden.** Existing call sites that parse free-text (`extract_json`, `parse_self_audit`, the three near-duplicate `pre_pr_audit.py` JSON extracts, `progress.py:154`, `_CODEX_TOKENS_RE` and `_GENERIC_TOKENS_RE` in `agents/base.py`) get deleted with their callers rewritten.

## 3. Design

### 3.1 Per-role Pydantic output schemas

New module `quikode/agent_schemas.py` (one BaseModel per role; all `frozen=True, extra="forbid"`):

- `PlannerOutput` — node_id, summary, gauntlet_strategy, subtasks (list of structured subtask dicts).
- `SubtaskCheckerOutput` — verdict, findings (list of finding dicts), overall_assessment.
- `SubtaskTriageOutput` — failure_layer (closed enum), root_cause, file_line_cites, teaching_narrative.
- `PrePRRubricAuditOutput` — categories list with predicted/observed scores + rationale.
- `PrePRStandardsAuditOutput` — findings with `standards_doc_ref`.
- `PrePRBehaviorAuditOutput` — behaviors with witness verdict.
- `FixupPlannerOutput` / `MergePlannerOutput` — same shape as PlannerOutput plus the source findings that drove the fixup.
- `ProgressVerdict` — verdict (closed enum), rationale.
- `DoerEnvelope` (NEW, lightweight) — `{summary: str, files_touched: list[str], witness_commands_run: list[str], notes: str}`. NOT a contract for grading. Just bookkeeping. The doer emits it as JSON when run via the JSON-mode wrapper (writes-files agents still get JSON output; the structured part is metadata, not evidence).

The plan-33 `EvaluationContract` stays. Its `gauntlet_strategy`, `rubric_targets`, `standards_referenced`, `behavior_evidence_advanced` fields stay. What goes is the SELF_AUDIT contract from `Subtask` and the `quikode/self_audit.py` module.

### 3.2 JSON-mode CLI abstraction — unified protocol

**Two enforcement tiers** (verified at the command line on 2026-05-08 against codex 0.128.0 + LiteLLM 1.83.10):

- **Tier 1 — CLI-native schema enforcement.** Direct OpenAI Responses API: `codex exec --profile gpt5/codex --output-schema <schema>` returns schema-conformant JSON to `--output-last-message`. Same for `claude -p --output-format json --json-schema "$(...)"`. The CLI guarantees structural validity.
- **Tier 2 — client-side pydantic validation.** Proxy-routed providers (z.ai, Wafer Pass, etc.) translate Responses API → Chat Completions via LiteLLM (`model: together_ai/<MODEL>` is the working prefix as of 1.83.10; `openai/` does NOT translate, hits `<base>/responses` as 404). LiteLLM's `drop_params: true` discards the Responses-API `output_schema` parameter during translation, so the upstream model returns free text. The agent layer parses with `model_validate_json`, re-prompts ONCE on validation failure with a structured error containing the pydantic exception + the schema, then surfaces `parse_failure` to the worker if the re-prompt also fails. Verified working for small hello-world/schema probes with the wafer-hosted profiles (`glm-wafer`, `minimax`, `deepseek`, `qwen`). Live write-heavy `WritesFilesAgent` runs later exposed transport disconnects on `glm-zai` / `glm-wafer`; a host-side Wafer probe with `127.0.0.1` reached LiteLLM but returned a shell command in a code block instead of performing the file write or honoring the schema. These proxy-routed models should remain limited to JSON/read-only roles until the LiteLLM write-role path is fixed.

Both tiers expose the same `JsonAgent` interface; the worker doesn't know which tier a profile uses. Tier 1 is strictly cheaper (one CLI roundtrip, never re-prompts on schema). Tier 2 pays at most one extra roundtrip on schema drift.

New module `quikode/agents/json_protocol.py` defining one `JsonAgent` abstract base class:

```python
class JsonAgent(Protocol):
    name: str  # "claude" | "codex" | "opencode"

    def invoke(
        self,
        prompt: str,
        *,
        output_schema: type[BaseModel],
        handle: ExecutionSandbox,
        log_path: Path | None = None,
        timeout: int | None = None,
    ) -> JsonAgentResult: ...

@dataclass(frozen=True)
class JsonAgentResult:
    structured: BaseModel | None       # None iff CLI failed before producing a parse-able envelope
    raw_envelope: dict | None          # full JSON envelope from CLI for debugging/storage
    rc: int
    transient: bool
    duration_s: float
    tokens_input: int | None
    tokens_output: int | None
    cost_usd: float | None
    parse_errors: tuple[str, ...]      # non-empty iff CLI returned non-conforming JSON; triggers one re-prompt
```

Three concrete CLI shims, each a thin transport implementation. None is exposed to roles directly — the model_registry selects them.

- **`CodexDirectJsonAgent`** (transport=`codex_direct`, used for OpenAI's own models like `gpt-5.5` / `gpt-5.3-codex`): writes an OpenAI strict-compatible JSON Schema to a tmp file, invokes `codex exec --profile <profile> --output-schema <tmp> --output-last-message <out> --skip-git-repo-check ...`, reads `<out>`, parses via `output_schema.model_validate_json`. CLI-native enforcement — schema violation is impossible at this layer. The schema file is produced by `codex_output_schema()`, which strips pydantic defaults, recursively sets `additionalProperties: false`, and lists every object property in `required` because the Responses strict schema validator rejects defaulted/optional properties.
- **`CodexLitellmJsonAgent`** (transport=`codex_litellm`, used for any non-OpenAI provider routed through the local LiteLLM proxy): same codex invocation as above with `--profile <glm-zai|glm-wafer|minimax|...>` and the same `codex_output_schema()` normalization before writing `--output-schema`. The `--output-schema` parameter is passed but is dropped during litellm's Responses → Chat Completions translation (verified 2026-05-08 against litellm 1.83.10) AND the upstream provider does not honor `response_format: json_schema` either (verified for wafer's GLM-5.1 deployment via direct curl). So at this transport, the response is free text. The agent layer parses with `output_schema.model_validate_json`. On `ValidationError`, the agent re-prompts ONCE with a structured error: *"Your previous response failed schema validation: `<pydantic error>`. Respond ONLY with valid JSON matching this schema: `<schema dump>`."* A second failure surfaces `parse_failure` to the worker. Pydantic + structured re-prompt is the floor; no regex, no heuristic JSON extraction.
- **`ClaudeJsonAgent`** (transport=`claude`, used for any Anthropic model — primarily `claude-opus-4-7`): invokes `claude -p --output-format json --json-schema "$(...)"` → parses the envelope's `structured_output` field via `output_schema.model_validate`. CLI-native enforcement — Anthropic returns `structured_output` already validated against the schema.

Each subclass implements ONLY the CLI-specific shell incantation + envelope-extraction step. All retry, quota-cascade, transient-detection, ccusage enrichment lives in the base class — exactly once.

The legacy free-text agent path (`Agent.run` from `agents/base.py`) stays for one purpose only: writes-files agents (the doer, the conflict resolver). They use a NEW `WritesFilesAgent` interface that returns `(rc, summary_envelope: DoerEnvelope, transient)` — same JSON wrappers as the structured-output path, but the schema is `DoerEnvelope` (lightweight bookkeeping) and the diff is the real evidence.

### 3.3 Role-MODEL binding (CLI is invisible to the role)

**The role-binding axis is MODEL, never CLI.** The CLI is a transport detail derived from the model name. Roles only know about their pydantic schema and which model they want; the agent layer figures out which CLI shim to invoke. NO ROLE EVER REFERENCES A CLI BY NAME.

The model name encodes everything the agent layer needs. opencode is dropped entirely — every model is reachable via codex (direct OpenAI for `gpt-5.5` / `gpt-5.3-codex`, codex+litellm for everything else) or claude (for opus). The model registry knows which transport each model takes.

New modules:

```python
# quikode/model_registry.py
@dataclass(frozen=True)
class ModelSpec:
    name: str                           # "gpt-5.5", "gpt-5.3-codex", "GLM-5.1-zai", "GLM-5.1-wafer", "claude-opus-4-7", "MiniMax-M2.7", ...
    transport: Literal["codex_direct", "codex_litellm", "claude"]
    schema_enforcement: Literal["cli_native", "client_side"]   # cli_native iff codex_direct or claude
    codex_profile: str | None           # for codex transports: which codex --profile to pass
    claude_model_id: str | None         # for claude transport: --model arg

MODELS: dict[str, ModelSpec] = {
    "gpt-5.5":         ModelSpec("gpt-5.5",         "codex_direct",  "cli_native",   codex_profile="gpt5"),
    "gpt-5.3-codex":   ModelSpec("gpt-5.3-codex",   "codex_direct",  "cli_native",   codex_profile="codex"),
    "GLM-5.1-zai":     ModelSpec("GLM-5.1-zai",     "codex_litellm", "client_side",  codex_profile="glm-zai"),
    "GLM-5.1-wafer":   ModelSpec("GLM-5.1-wafer",   "codex_litellm", "client_side",  codex_profile="glm-wafer"),
    "MiniMax-M2.7":    ModelSpec("MiniMax-M2.7",    "codex_litellm", "client_side",  codex_profile="minimax"),
    "DeepSeek-V4-Pro": ModelSpec("DeepSeek-V4-Pro", "codex_litellm", "client_side",  codex_profile="deepseek"),
    "Qwen3.5-397B-A17B": ModelSpec(..., "codex_litellm", "client_side",  codex_profile="qwen"),
    "claude-opus-4-7": ModelSpec("claude-opus-4-7", "claude",        "cli_native",   claude_model_id="claude-opus-4-7[1m]"),
    # ... extend as new models / providers come online
}

# quikode/agent_registry.py
@dataclass(frozen=True)
class RoleSpec:
    name: str
    output_schema: type[BaseModel] | None  # None for writes-files roles
    writes_files: bool
    default_model: str                     # default model name from MODELS — operator overrides via cfg.<role>_model
    timeout_s_field: str

ROLES: dict[str, RoleSpec] = {
    "planner":           RoleSpec("planner",           PlannerOutput,           False, "gpt-5.5",       "planner_timeout_s"),
    "subtask_doer":      RoleSpec("subtask_doer",      DoerEnvelope,            True,  "GLM-5.1-zai",   "subtask_doer_timeout_s"),
    "subtask_checker":   RoleSpec("subtask_checker",   SubtaskCheckerOutput,    False, "gpt-5.5",       "subtask_checker_timeout_s"),
    "subtask_triage":    RoleSpec("subtask_triage",    SubtaskTriageOutput,     False, "gpt-5.5",       "subtask_triage_timeout_s"),
    "pre_pr_rubric":     RoleSpec(...,                 PrePRRubricAuditOutput,  False, "gpt-5.5",       ...),
    "pre_pr_standards":  RoleSpec(...,                 PrePRStandardsAuditOutput, False, "gpt-5.5", ...),
    "pre_pr_behavior":   RoleSpec(...,                 PrePRBehaviorAuditOutput, False, "gpt-5.5", ...),
    "fixup_planner":     RoleSpec(...,                 FixupPlannerOutput,      False, "gpt-5.5",       ...),
    "merge_planner":     RoleSpec(...,                 MergePlannerOutput,      False, "gpt-5.5",       ...),
    "conflict_resolver": RoleSpec(...,                 DoerEnvelope,            True,  "GLM-5.1-zai",   ...),
    "progress":          RoleSpec(...,                 ProgressVerdict,         False, "gpt-5.5",       ...),
}

def make_agent(role: str, cfg: Config) -> JsonAgent | WritesFilesAgent:
    role_spec = ROLES[role]
    model_name = getattr(cfg, f"{role}_model", role_spec.default_model)
    model_spec = MODELS[model_name]
    schema = role_spec.output_schema
    timeout_s = getattr(cfg, role_spec.timeout_s_field)
    if model_spec.transport == "codex_direct":
        cli = CodexDirectJsonAgent(profile=model_spec.codex_profile, timeout=timeout_s)
    elif model_spec.transport == "codex_litellm":
        cli = CodexLitellmJsonAgent(profile=model_spec.codex_profile, timeout=timeout_s)
    else:  # "claude"
        cli = ClaudeJsonAgent(model_id=model_spec.claude_model_id, timeout=timeout_s)
    if role_spec.writes_files:
        return WritesFilesAgent(cli=cli, envelope_schema=schema)
    return JsonOutputAgent(cli=cli, output_schema=schema, enforcement=model_spec.schema_enforcement)
```

**Operator surface:** `cfg.planner_model = "GLM-5.1-zai"` Just Works. The role doesn't know or care that GLM-5.1-zai is reached via codex+litellm with client-side validation. Same for any role × any model — the model registry handles transport selection. The `cfg.<role>_cli` knob does NOT exist. Roles are bound to MODELS, only and forever.

**Schema-enforcement tier is a property of the MODEL, not the role.** A `cli_native` model gives Tier 1 (CLI guarantees schema); a `client_side` model gives Tier 2 (pydantic validation + re-prompt-once). Both surface the same `JsonAgentResult` to the role. The role never knows which tier its model is in — it just gets a validated `BaseModel` instance or a structured `parse_failure`.

**Adding a new model is a one-line edit to `MODELS`.** Adding a new provider (e.g., a new OpenAI-compatible endpoint via litellm) is: register the upstream in `~/.codex/litellm_config.yaml`, add a codex profile in `~/.codex/config.toml`, add a `MODELS` entry. No role / agent_registry change.

### 3.4 SELF_AUDIT retirement

Hard delete:

- `quikode/self_audit.py` (531 LoC) — gone.
- `quikode/workers/subtask_execution.py` — the `parse_self_audit` + `short_circuit_decision` path deleted; replaced by direct LLM-checker call against the diff + witness output.
- `prompts/subtask-doer.md` — strip the SELF_AUDIT requirements section; replace with "emit a JSON envelope `DoerEnvelope` describing files touched, witnesses run, summary; you are NOT being graded on this envelope, only on the diff." Doer prompt is much shorter.
- `prompts/subtask-checker.md` — already grades against the diff; just remove the "verify SELF_AUDIT claims against diff" framing and replace with "grade the diff directly against the rubric / standards / behavior contract." The checker still runs in JSON mode and emits `SubtaskCheckerOutput`.
- `prompts/subtask-triage.md` — similar scope-tightening; remove the "self_audit_mismatch" failure layer enum value (no more SELF_AUDIT to mismatch); add a `parse_failure` value for cases where checker output failed schema validation.
- Plan 36's `_scan_risk_tokens` carve-out is deleted with the rest of `self_audit.py`.

### 3.5 Eliminate every prose-parsing call site

Catalogued from `grep "json.loads\|extract_json\|parse_tokens" quikode/`:

- `quikode/subtask_schema.py:243 extract_json` — DELETED. Callers (`subtask_schema.py:407,468`) rewritten to consume `JsonAgentResult.structured` (a typed `PlannerOutput`).
- `quikode/agents/progress.py:154 json.loads(snippet)` heuristic — DELETED. Progress agent runs as `JsonAgent` against `ProgressVerdict` schema.
- `quikode/pre_pr_audit.py:245,381,483` three near-duplicate JSON extracts — DELETED. Each audit role gets a distinct `JsonAgent` invocation against its dedicated schema (`PrePRRubricAuditOutput`, `PrePRStandardsAuditOutput`, `PrePRBehaviorAuditOutput`).
- `quikode/agents/base.py _CODEX_TOKENS_RE`, `_GENERIC_TOKENS_RE`, `parse_tokens` — DELETED. Token data comes from CLI envelope (claude) / ccusage (codex/opencode). Free-text token regex is the same anti-pattern at smaller scale.

`grep -rn "json.loads\|json.load(" quikode/` after the change should show only filesystem/store/CLI-helper call sites — never an agent-output prose extract.

### 3.6 TUI live-state correctness

`quikode/tui_app.py` (and `cli_briefing_dev.py` for the briefing equivalent):

- The "what's running" indicator must derive from a **fresh signal**, not the FSM state. Specifically: query `agent_calls` for the latest row per (task_id, subtask_id) where the call is the in-flight one; "in flight" iff the row's `ts` is missing OR `duration_s` is null AND `<state_dir>/<task>/.last_call_started` is fresher than `<state_dir>/<task>/.last_call_returned`. (New marker files written by the worker around each agent_call.)
- New display fields per task: `last_call_started_ago`, `last_call_returned_ago`, `last_call_phase`, `last_call_rc`. Aggregate display: "running per-subtask doer" only when `last_call_started > last_call_returned`; otherwise "doer returned (rc=N) — orchestrator post-processing" or "stalled — last call returned Xs ago."
- `qk briefing`'s "in-state" line keeps the FSM in-state clock but adds a parallel "agent in-flight: N seconds / not running" line so the operator can see at a glance.

### 3.7 Stale config defaults (subsumed sub-piece)

Two-line surgery:

- `quikode/config_loader.py` — reads each scalar via the same `raw.get(KEY, defaults.KEY)` shape (the defaults source-of-truth wins when the key is absent), but emits `log.info("config[%s] = %d (overrides Field default %d)", KEY, raw_v, default_v)` for every timeout knob actually overridden by the toml. Audit trail at daemon-start.
- `quikode/config_template.py` — bump seeded literals to match every post-`d06cdcd` Field default (subtask_doer_timeout_s 1200→1800, etc.). New regression test (`tests/test_config_schema.py::test_template_seeds_match_field_defaults`) iterates every `int Field(default=…)` in `Config` and asserts the template's seeded literal matches. Locks the seed-vs-default invariant going forward.

Operational at deploy: edit the live `quikode-runs/tanren/.quikode/config.toml` to delete every timeout-knob line that pins below the post-bump default. Documented in §6.

## 4. Concrete file list

**New:**
- `quikode/agent_schemas.py` — every role's pydantic output model.
- `quikode/agents/json_protocol.py` — `JsonAgent` ABC, `JsonAgentResult`, `WritesFilesAgent`.
- `quikode/agents/json_claude.py` — `ClaudeJsonAgent`.
- `quikode/agents/json_codex.py` — `CodexJsonAgent`.
- `quikode/agents/json_opencode.py` — `OpencodeJsonAgent`.
- `quikode/agent_registry.py` — `RoleSpec`, `ROLES`, `make_agent`.
- `tests/test_agent_schemas.py` — round-trip every schema; `extra="forbid"` rejects unknown keys; closed enums reject typos.
- `tests/test_json_agents_contract.py` — for each (role × CLI) pair, given a fixed prompt and a stub CLI runner, assert the wrapper returns a validated BaseModel instance and that prose-only output triggers exactly one re-prompt before failing.
- `tests/test_role_cli_decoupling.py` — operator flips `cfg.planner_cli = "opencode"`; assert `make_agent("planner", cfg)` returns an `OpencodeJsonAgent` configured with the `PlannerOutput` schema.

**Modified (significant):**
- `quikode/agents/claude.py` → wraps `ClaudeJsonAgent` for back-compat; the schema-aware path replaces the legacy entry-points incrementally.
- `quikode/agents/codex.py` → same; `--output-last-message` parsing path retired.
- `quikode/agents/opencode.py` → same; the SELF_AUDIT-emitting prose path retired.
- `quikode/agents/base.py` — `parse_tokens`, `_CODEX_TOKENS_RE`, `_GENERIC_TOKENS_RE` deleted. `_exec` stays for `WritesFilesAgent`.
- `quikode/workers/subtask_execution.py` — SELF_AUDIT parse-and-short-circuit path deleted; direct call to JSON-mode subtask checker against the diff.
- `quikode/pre_pr_audit.py` — three heuristic JSON extracts deleted; three JSON-mode wrappers via `make_agent("pre_pr_rubric"/"pre_pr_standards"/"pre_pr_behavior")`.
- `quikode/agents/progress.py` — JSON-mode agent against `ProgressVerdict` schema.
- `quikode/subtask_schema.py` — `extract_json` removed; planner-output ingestion comes pre-validated.
- `quikode/config.py` — add `<role>_cli` and `<role>_model` fields per role (defaults from `RoleSpec.default_cli`).
- `quikode/config_loader.py` — log every toml-overridden int knob; add the new role config fields.
- `quikode/config_template.py` — bump every timeout seed to match Field defaults.
- `quikode/tui_app.py`, `quikode/cli_briefing_dev.py` — live agent-call signal per §3.6; new "agent in-flight" line.
- `prompts/subtask-doer.md` — SELF_AUDIT section replaced with `DoerEnvelope` JSON output requirement.
- `prompts/subtask-checker.md` — graded against the diff directly, no SELF_AUDIT plumbing.
- `prompts/subtask-triage.md` — failure_layer enum updated (`self_audit_mismatch` removed; `parse_failure` added).

**Deleted:**
- `quikode/self_audit.py` — gone.
- `tests/test_self_audit.py` — gone.
- The `_classify_empty_staging` Z-99 carve-out from plan 33 if it referenced SELF_AUDIT specifically (verify and remove).

## 5. PR breakdown

Three sequential PRs, each small enough to review surgically.

**PR-A — schemas + JsonAgent layer (~1500 LoC)**
- All new files in §4.
- All three `Json*Agent` classes wired and tested with stub CLI runners.
- Existing `claude.py` / `codex.py` / `opencode.py` left in place but unused by new code paths.
- Hello-world contract tests: each (role × CLI) returns a validated schema instance.
- No worker, no prompt, no SELF_AUDIT changes yet.
- Acceptance: validation ladder green; `make_agent("planner", cfg)` works for all three CLIs.

**PR-B — SELF_AUDIT retirement + worker rewrite + prose-parser delete (~1500 LoC)**
- `quikode/self_audit.py` and tests deleted.
- `subtask_execution.py` rewritten: doer (writes-files) → checker (JSON-mode against diff) → triage (JSON-mode) on fail.
- `pre_pr_audit.py` three heuristic extracts deleted; three JSON-mode wrappers in.
- `progress.py` JSON-mode.
- All prompts updated.
- Plan 36 dead code deleted.
- Acceptance: validation ladder green; new end-to-end test simulates a doer run + LLM checker grade against a diff + triage failure (no SELF_AUDIT in the path).

**PR-C — TUI live-state + config-loader audit log + role-CLI knobs (~600 LoC)**
- TUI fields per §3.6.
- `config_loader.py` per-knob log emission.
- `config_template.py` seed bumps.
- New regression test for template-vs-default invariant.
- Acceptance: validation ladder green; `qk tui` and `qk briefing` correctly distinguish "agent running" from "post-processing" from "stalled."

## 6. Deploy

Mass `qk retry` is required because PR-B changes the per-subtask schema (no SELF_AUDIT → re-planning under the new contract). One restart for all three PRs together once they're merged.

```
# 1. Stop daemon, drain in-flight
cd /home/trevor/github/quikode-runs/tanren
qk daemon stop

# 2. Reinstall
cd /home/trevor/github/quikode
bash scripts/reinstall.sh --skip-tests

# 3. Operational config edits in tanren workspace
cd /home/trevor/github/quikode-runs/tanren
# Edit .quikode/config.toml — delete subtask_doer_timeout_s = 1200
# (Field default 1800 will now apply; restart picks it up.)

# 4. Mass retry every non-merged task (including kind="merge" merge nodes)
qk retry --all-non-merged

# 5. Restart with retry-failed flag
qk daemon start --detach --max-parallel 12 --retry-failed
```

PR-A by itself is shippable (no contract change, additive layer). PR-B forces the retry. PR-C is independent of both.

Plan 35 (standards profiles) ships in the same wave — it ALSO requires `qk retry` for the new schema fields, so amortize the restart. The two plans together ship as one batch:
- PR-A (json layer)
- PR-B (SELF_AUDIT retirement + worker rewrite)
- Plan 35 PR-A (standards profile loader + schema + planner)
- Plan 35 PR-B (architecture-alignment auditor)
- PR-C (TUI + config audit log)

Five PRs across two plans, single deploy boundary.

## 7. Validation

`uv run ruff check quikode tests` + `uv run ruff format --check quikode tests` + `uv run ty check quikode tests` + `uv run pytest tests/ -q` — all green at every PR.

New test coverage targets:
1. Each role's pydantic schema round-trips JSON; rejects unknown keys (`extra="forbid"`); rejects typo'd enum values.
2. Each `JsonAgent` (claude/codex/opencode), given a stub-CLI runner that emits a fixture envelope, returns a validated `BaseModel` instance.
3. Schema-validation failure triggers exactly one re-prompt; the re-prompt also failing surfaces a structured `parse_failure` to the worker.
4. Role-CLI matrix smoke: `make_agent(<every_role>, cfg_with_each_cli)` constructs without error; the CLI receives the role's schema; the wrapper returns the role's BaseModel type.
5. Worker integration: subtask doer → diff → JSON-mode checker → triage on fail. No SELF_AUDIT artifact written, no `_scan_risk_tokens`, no `extract_json` reachable from the per-subtask path.
6. TUI live-state: when an agent_call returns, the TUI's "agent in-flight" line updates within one render tick; when no call is running, the indicator says so.
7. `tests/test_config_schema.py::test_template_seeds_match_field_defaults` iterates every int Field default and asserts the seed matches.
8. End-to-end smoke: a full subtask run under stub agents emits a clean diff, checker passes, no prose-parsing path is traversed (assertion: zero `extract_json` / `parse_self_audit` / `_RISK_TOKEN_RE` invocations during the test).

## 8. Confidence

**High confidence** on the design direction. All three CLIs verified to support JSON output mode at the command line; two of three support CLI-side schema enforcement; the third (opencode) supports JSON event streaming and we validate via pydantic client-side. The architectural rule (no prose parsing) is concrete and falsifiable.

**Medium confidence** on PR sizing. The three PRs are large but each has a clean seam. PR-A is purely additive. PR-B is the destructive cleanup but the contract change is one-shot and well-specified. PR-C is small.

**Speculative** on the TUI live-state design — the marker-file approach is a sketch; real implementation may want to read `agent_calls` directly, or the worker may want to publish a heartbeat keyed to (task_id, current_role). Implementation-time call.

**The DIRECTION is high-confidence.** PR sequencing and the operational config edit at deploy are both well-defined. The `qk retry` cost is real but bounded — no in-flight worktree value is being preserved; under SELF_AUDIT the contract is broken anyway.
