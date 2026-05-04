# quikode v3 candidates — observations from F-0002

After F-0002 landed in tanren main on 2026-05-02 (commit 6160cab),
this is a running list of friction points that surfaced when re-attempting
to drive R-* nodes against the new tanren architecture. Each item is an
observed gap between what the doer/checker need to know and what
quikode's prompts/core currently teach them.

Status legend: 🔴 high (R-* will fail without this) · 🟡 medium · 🟢 nice-to-have.

## V3-001 🔴 BDD convention is not in any prompt

**Observation.** F-0002 added a hard contract for `.feature` files:

- One file per behavior at `tests/bdd/features/B-XXXX-<slug>.feature`
- Closed tag allowlist (`@B-XXXX` feature, then `@positive`/`@falsification` plus 1–2 of `@web|@api|@mcp|@cli|@tui`)
- Strict-equality coverage: every interface in the behavior's `interfaces:` set needs both a positive and (when listed) falsification scenario
- `Scenario Outline` / `Examples:` are forbidden; `Background:` / `Rule:` are allowed
- Two-interface scenarios need a `# rationale:` comment line above the tag block
- Three+ interface tags is a hard error

This is enforced by `xtask check-bdd-tags`, wired into `just check`. R-* nodes that don't comply cause `just ci` to fail at the BDD lane, which the orchestrator's `_run_ci_checks` will catch — but the doer/checker won't know what they did wrong unless the prompts teach them.

**Quikode's current state.** `prompts/planner.md` mentions BDD only as "scenarios last." `prompts/doer.md`, `prompts/checker.md`, `prompts/subtask-doer.md` say nothing. The agents will discover the convention by failing `just ci` and reading the validator output, which works but burns retries.

**Proposal.** Add a "BDD convention" section to `prompts/planner.md` and `prompts/doer.md` that links `docs/architecture/subsystems/behavior-proof.md` (BDD Tagging And File Convention) and inlines the rules above. Specifically:
- Planner: when a node `completes_behaviors`, emit a subtask per behavior named `S-NN-bdd-B-XXXX` that creates the .feature file. List which interfaces the behavior touches.
- Doer (subtask): when the subtask is a BDD slice, follow the convention precisely; reference the validator command (`just check-bdd-tags`).
- Checker: explicitly run `just check-bdd-tags` and surface its output verbatim.

**Effort.** ~30 LOC across three prompts. Tests assert the prompts mention `B-XXXX`, `@positive`, `@falsification`, the closed allowlist.

## V3-002 🔴 The doer doesn't know `just check-bdd-tags` exists

**Observation.** F-0002 introduced two new validator commands (`just check-bdd-tags`, `python3 scripts/roadmap_check.py`). The current `prompts/checker.md` instructs the checker to run `just ci` (the aggregate). That's correct in principle, but a fail in the BDD lane is opaque from `just ci`'s output — the agent should be told to run `just check-bdd-tags` directly when investigating BDD failures, and `python3 scripts/roadmap_check.py` for orphan-feature errors.

**Proposal.** Add a "Targeted BDD diagnosis" callout in the checker prompt: "if `just ci` fails in the bdd-tags step, re-run `just check-bdd-tags` standalone and paste the output."

## V3-003 🟡 Planner doesn't surface `interfaces:` to the doer

**Observation.** The DAG's `expected_evidence` items each carry an `interfaces:` field listing which surfaces a behavior witnesses on (e.g., `["web", "api"]`). F-0002's BDD coverage is strict-equality against this set. The current planner template renders `expected_evidence` as a list of bullets but doesn't lift the `interfaces:` to the doer's scope. Result: the doer doesn't know which `@web/@api/@cli/@mcp/@tui` tag combinations to write.

**Proposal.** Add an `interfaces` field to `Subtask` (in `subtask_schema.py`) that the planner populates for BDD subtasks. The doer prompt then renders "Cover these interfaces:" alongside acceptance.

## V3-004 🟡 No first-class behavior-id awareness

**Observation.** Many R-* nodes complete a single behavior (B-XXXX), but a few complete multiple. The planner template has no concept of "this is a multi-behavior node, ship one feature file per behavior" — it just sees `completes_behaviors: ["B-0059", "B-0060"]` and might produce one BDD subtask per behavior or fold them. F-0002's convention requires per-behavior files.

**Proposal.** Planner prompt explicitly instructs: "for each id in `completes_behaviors`, emit one BDD subtask producing `tests/bdd/features/<that-id>-<slug>.feature`."

## V3-005 🟡 The checker plays back `just ci` but not the curl probes

**Observation.** F-0002 `expected_evidence` has manual probes ("curl /health returns 200", "curl /mcp without Authorization returns 401"). Those are a third class of evidence that neither `just ci` nor the doer runs. The checker prompt says "real HTTP/CLI/MCP calls" but the existing playbook is sparse on how to actually run them — `tanren-mcp` needs to be running on a port, with `TANREN_MCP_API_KEY` set.

**Proposal.** Update the checker prompt for any node whose `expected_evidence.kind == 'manual'` to spin up the relevant binary in the background, run the probe, capture stdout, and verify against the expected response. This is a non-trivial doer/checker change — the runtime needs:
- A way to background-start a tanren binary inside the container
- Port allocation (`tanren-mcp` defaults somewhere)
- API key injection
- Cleanup on completion

For v3, defer to a "manual-probe runner" subagent. For v2 stop-gap: have the checker emit a clear `MANUAL_PROBE_REQUIRED` verdict when manual evidence is in scope, surface that to the user, and allow human override via `quikode mark-merged`.

## V3-006 🟡 No knowledge of `xtask` extension surface

**Observation.** F-0002 added `xtask/src/bdd_tags/{data,parser,mod}.rs`. The xtask binary is the project-specific tool runner. Future tanren nodes will likely add more xtask subcommands (e.g., behavior-catalog generators, DAG validators). The doer needs to know:
- xtask lives at `xtask/src/main.rs`; new subcommands plug in there
- The bdd_tags subcommand is the template for adding others
- Subcommands are wired into `just check` via the `justfile`

**Proposal.** Add a brief "xtask extension" note in `prompts/doer.md` so the agent knows where to add new validators when the spec asks for them.

## V3-007 🟢 Re-attempt budget should reset on prompt updates

**Observation.** R-0001 is currently BLOCKED in quikode's view after exhausting retry budget across two days of attempts. With F-0002 landed and prompts (presumably) updated to cover the new convention, R-0001 should get a fresh budget. Today: `quikode retry R-0001` resets it manually. Future: when the user knows there's been a prompt update worth re-running against, `quikode retry --reason "F-0002 landed"` could record the reason in state_log so we can later analyze whether prompt updates correlate with convergence.

**Proposal.** Add an optional `--reason "<note>"` to `quikode retry` that gets logged with the transition. Low effort.

## V3-008 🟢 Workspace migration when DAG schema evolves

**Observation.** F-0002 added F-0002 itself to the DAG (so the node count went from 232 to 233). Quikode's existing tanren workspace had F-0001 plus 231 R-* tasks already seeded. F-0002 wasn't seeded until I ran a command after the pull — quikode does seed-on-demand from the DAG, which is fine. But: if the DAG ever *removes* a node, quikode's store keeps the orphan row. We should have a `quikode reconcile-dag` command that prunes rows for nodes no longer in the DAG (with a confirm prompt).

**Proposal.** New CLI command + slash command. Low priority — DAG nodes don't get removed often.

## V3-009 🟢 TUI should show "DAG version" / "config drift" warnings

**Observation.** When tanren's DAG changes (or when the user updates quikode's config.toml), the TUI's view can become stale until the next poll picks it up. For really structural changes (DAG nodes added/removed, config sections changed), it would be nice to surface a banner: "DAG file changed at HH:MM:SS — reload?" and a `/reload` slash command.

**Proposal.** Hash the DAG file mtime, expose a `/reload` slash command that reseeds without losing state.

## V3-010 🟡 Containerized `just check-bdd-tags` may need network

**Observation.** F-0002's HTTP MCP migration means `tanren-mcp` is a network service. `just ci` includes `cargo build --workspace --locked`, which will compile the new axum/rmcp transport-streamable-http-server deps. The first-time compile of these crates inside a quikode container will be slow (10-15 min on cold sccache). The shared sccache helps cross-container, but the first container of the day pays the full bill.

**Proposal.** Update `docker/Dockerfile` (tanren flavor) to pre-warm sccache with a `cargo build --workspace --locked` of the current main. This adds 10 min to image build but saves 10-15 min off every cold-cache task. Or: ship a periodic `quikode warm-cache` command that pulls main + builds, run nightly.

---

## Suggested v3 sequencing

1. **V3-001 + V3-002** in one prompt update (BDD convention awareness). Lowest effort, biggest unblock for R-* nodes.
2. **V3-003 + V3-004** as a `Subtask.interfaces` schema bump + planner update.
3. **V3-007** retry-with-reason — small ergonomic win.
4. **V3-005 (manual probes)** — bigger lift; design before implementing.
5. **V3-010 (sccache warming)** — separate from prompt work; can land independently.
6. **V3-006, V3-008, V3-009** — nice-to-haves to do as needed.

After (1) and (2), the right validation step is to retry R-0001 with the updated prompts and watch via the new TUI. That's the closed-loop test.
