# future work — open candidates

Replaces the old `v3-candidates.md` (now in `archive/`) as the
open-items tracker. Items are grouped by source: things observed during
the v3 3-run E2E that didn't land in scope, and items carried forward
from `v3-candidates.md` that are still open.

Status legend: 🔴 high · 🟡 medium · 🟢 nice-to-have

---

## From v3 E2E observations (2026-05-03)

### ✓ DONE Coalescing rebase triggers within a 30s window

Implemented 2026-05-02. New column
`tasks.last_rebase_scheduled_ts` (additive migration in
`Store._migrate`) plus `cfg.rebase_coalesce_window_s` (default 30,
range 0–600). `Orchestrator._schedule_rebase_to_main` reads the
last-trigger timestamp at entry and short-circuits when the elapsed
time is under the window — the in-flight rebase already covers any
back-to-back parent/sibling merge. Setting the window to 0 disables
coalescing for tests/debugging. 5 new tests in
`tests/test_rebase_coalescing.py`.

### 🟢 Race protection between worker push + force-push

Theoretical concern: if the worker is pushing while the orchestrator's
rebase worker is force-pushing the same branch, both could collide.
`--force-with-lease` is the standard mitigation and is already used.

Open question: is this a real failure mode in practice or theoretical?
Worth a stronger lock only if it actually bites.

### 🟢 Cost rolling-average in regular TUI dashboard

`quikode briefing` shows ccusage costs. The DAG viewer's stats panel
already has rolling avg per task. The regular TUI dashboard does not.
Small UX win.

### 🟢 Daemon webhook on BLOCKED

Opt-in slack/discord notification when a task BLOCKs. Lets humans
respond without polling. ~50 LOC, a single curl + a config knob.

### 🟢 Multi-workspace daemon

`quikode daemon` is single-workspace today. Could supervise N
workspaces from one daemon process. Future; not a friction point yet.

### 🟢 Stack depth >6

Cap is currently 6 (`cfg.stacking_max_depth`). Lifted from 4 for
tanren's chains. Could go higher if a future DAG needs it; would need
breadth-aware safety to avoid pathological stacks.

---

## Carried over from v3-candidates.md

### V3-005 ✓ DONE Manual probes — checker auto-runs curl probes

Implemented 2026-05-02. New module `quikode/manual_probe.py` exposes
`ManualProbe` (pydantic), `ManualProbeRunner` (with `start_service` /
`run_probe` / `teardown_services` + context-manager lifecycle), plus
`collect_probes_from_evidence` and `render_probe_block` helpers. The
worker's `_check()` calls `_run_manual_probes()` BEFORE the agent
checker, threads the rendered `MANUAL_PROBE_RESULTS` block into the
checker prompt as objective evidence. Defensive: any parse / runner /
exec failure degrades to the pre-runner behavior (empty block, no
crash).

**Contract**: each `expected_evidence` entry with `kind == "manual"`
becomes a probe. Two shapes accepted:

  1. Structured (preferred): `{kind, service, command, expected, description}`.
     `command` may use `$PORT_<service>` placeholders that the runner
     substitutes with the allocated port.
  2. Free-text fallback: when only `description` is set, a regex
     extracts `curl ...` and `expected "..."`. If extraction fails the
     probe is logged + skipped.

Credentials come from `manual_probe.credentials_from_env([...])` —
worker pulls `TANREN_MCP_API_KEY` / `TANREN_API_KEY` from `os.environ`
explicitly. The runner deliberately doesn't sweep the full env to
avoid silent leakage.

23 new tests in `tests/test_manual_probe.py` covering parsing,
classification, service start/teardown, port substitution, and the
worker integration path.

### V3-010 ✓ DONE Pre-warm sccache

Implemented 2026-05-02 as the periodic-warm option. New CLI:
`quikode warm-cache [--timeout 1800] [--no-fetch] [--branch <ref>]`.
Spins up a transient `qk-warm-<6hex>-warm` container against
`cfg.image_tag` with `cfg.repo_path` (RW) + `cfg.sccache_dir` (RW)
mounted, runs `git fetch origin <base_branch>`, `git checkout
origin/<base_branch>`, `cargo build --workspace --locked`, then
`sccache --show-stats`, and tears the container down (even on cargo
failure). Suitable for cron / nightly invocation; no postgres or
agent CLIs needed. 6 new tests in `tests/test_warm_cache.py`.

The image-bake option remains available as a future iteration if the
periodic warm proves operationally heavy; the helper
`docker_env.start_warm_cache_container` is reusable from a Dockerfile
build step too.

### V3-006 🟢 xtask extension prompt awareness

**Status: OPEN.** F-0002 added `xtask/src/bdd_tags/`. Future tanren
nodes will likely add more xtask subcommands. The doer should know
where they plug in. ~5 LOC in `prompts/doer.md`.

### V3-007 🟢 Retry-with-reason

**Status: OPEN.** `quikode retry --reason "<note>"` would log the
reason in `state_log` so we can later analyze whether prompt updates
correlate with convergence. ~10 LOC.

### V3-008 🟢 DAG drift reconciliation

**Status: OPEN.** If the DAG removes a node, quikode's store keeps the
orphan row. `quikode reconcile-dag` would prune those rows with a
confirm prompt. Low priority — DAG nodes rarely get removed.

### V3-009 🟢 TUI DAG-version banner / `/reload` slash command

**Status: OPEN.** When the DAG file changes mtime, the TUI should
surface a banner: "DAG file changed — reload?" and a `/reload` slash
command that reseeds the store without losing in-flight task state.

---

## Already shipped (formerly v3-candidates)

- **V3-001 BDD convention in prompts** ✅ DONE. `prompts/planner.md` and `prompts/subtask-doer.md` both reference `B-XXXX`, the closed tag allowlist, and `xtask check-bdd-tags`.
- **V3-002 doer/checker awareness of `just check-bdd-tags`** ✅ DONE. Checker prompt instructs running `just check-bdd-tags` standalone on BDD failures.
- **V3-003 planner surfaces `interfaces:` to doer** ✅ DONE. `Subtask` model carries the field; rendered in subtask-doer prompt.
- **V3-004 first-class behavior-id awareness** ✅ DONE. Planner explicitly emits one BDD subtask per id in `completes_behaviors`.

---

## Suggested sequencing

1. **V3-007 retry-with-reason** + **V3-006 xtask prompt** — single small commit, clean ergonomic wins.
2. **V3-010 pre-warm sccache** — ship as image-bake step. Operationally large impact.
3. **Coalescing rebase triggers** — schema migration + bookkeeping. Worth doing before scaling parallelism past 3.
4. **V3-005 manual-probe runner** — bigger lift; design before implementing. Block on a tanren node that actually needs it.
5. **V3-008, V3-009** — nice-to-haves; do as needed.
