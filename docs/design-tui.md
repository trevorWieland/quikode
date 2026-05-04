# quikode TUI design

> **Status: MOSTLY IMPLEMENTED.** This document is kept as architectural reference for the design decisions.
> The v1 mission-control dashboard, slash commands, settings modal, and DAG viewer all ship today (`quikode tui`).
> v1.1 milestone overlay items (e.g., DAG-version banner / `/reload`) are still pending — see
> [`future-work.md`](future-work.md). Current architecture lives in [`architecture.md`](architecture.md).

**Goal:** mission control. The user's hands-on view into the orchestrator. Glanceable health, drill-in detail, and direct controls — open a PR, retry a blocked task, swap a model, raise the retry budget — without leaving the TUI.

For the existing CLI surface that this builds on, see `architecture.md`. For data shapes, `quikode/types.py` and `quikode/state.py`.

## Design constraints

1. **Mirror the CLI vocabulary.** Slash commands match CLI commands 1:1 by default (`/run`, `/status`, `/show`, `/explain`, `/export`, etc.). Some are inflected for interactive use (e.g. `/show` defaults to the selected task; `/export` writes + opens the file).
2. **No state owned by the TUI.** All state is in `.quikode/quikode.db`. The TUI is a live view of that DB plus a thin command layer that calls existing `quikode/*.py` functions. This means: no consistency drift, no separate event store, no risk of TUI vs CLI showing different things, and any user can flip between TUI and CLI freely.
3. **Single-workspace v1.** One workspace per TUI process. `cd` to a different `.quikode` dir and re-launch to switch. Multi-workspace tabs are explicitly v2.
4. **Don't replace the orchestrator.** The TUI doesn't run agents or schedule tasks itself — it talks to a running orchestrator (or kicks one off via `/run`). The orchestrator is still a long-running background process. The TUI is the cockpit.
5. **Async-first, no blocking.** Textual's async loop. Long actions (open PR, run reset) launch in background workers and emit progress events back to the UI.

## v1 layout

```
┌─ quikode · workspace: /home/trevor/github/quikode-runs/tanren ───────────────────────────────────┐
│ stacking: within-milestone · max-parallel 3 · cpu 4/12GB per task · auto-retry: on               │
│ in-flight: 3 · awaiting: 2 · blocked: 1 · merged: 18/232 (8%) · tokens this run: 4.7M ($N/A)     │
├─────────────────────────────────────────────────────────────────────────────────────────────────┤
│ Tasks                                                                            ↑↓ select · /sort│
│ ┏━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓│
│ ┃ ID    ┃ State         ┃ in-state┃ Mtime  ┃ Retries ┃ Branch / PR / Note                       ┃│
│ ┡━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩│
│ │•R-001 │ doing_subtask │  2m14s  │   12s  │ 0/0/0   │ quikode/r-0001-aaffaf · S-03/8           ││
│ │ R-002 │ awaiting_humn │ 14m02s  │ 1m08s  │ 1/0/0   │ #57 (green) · MERGEABLE                  ││
│ │ R-003 │ blocked       │  3m45s  │   —    │ 3/0/0   │ exhausted do/check budget (S-04 stuck)   ││
│ │ R-004 │ rebasing      │   23s   │   —    │ 0/0/0   │ rebase quikode/r-0004 onto main          ││
│ │ R-005 │ awaiting_humn │ 26m     │ 2m14s  │ 0/0/0   │ #58 (CONFLICTING) · waits parent PR     ││
│ │ ...                                                                                            ││
│ ┕━━━━━━━┷━━━━━━━━━━━━━━━┷━━━━━━━━━┷━━━━━━━━┷━━━━━━━━━┷━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛│
├──────────────────────────────────────┬───────────────────────────────────────────────────────────┤
│ Activity                             │ Resources                                                 │
│ 14:32:01 R-001 → checking_subtask    │   host: 18 cores · 78 GB                                  │
│ 14:31:45 R-002 → awaiting_merge (PR  │   per-task cap: 4 cores · 12 GB                          │
│            #57 opened)               │   max parallel auto: 5 · current 3                        │
│ 14:30:14 R-003 → blocked (S-04)      │                                                           │
│ 14:30:01 R-001 stalled warn          │   live containers:                                        │
│            (worktree quiet 6m)       │     R-001  cpu 38%  rss 6.2/12 GB                         │
│ 14:29:55 R-004 → rebasing            │     R-002  (idle, awaiting merge)                         │
│ ...                                  │     R-004  cpu 4%   rss 0.9/12 GB                         │
├──────────────────────────────────────┴───────────────────────────────────────────────────────────┤
│ R-001 detail (selected)                                                          [Tab to swap]   │
│   plan summary: Add account/sign-in across 5 interfaces (B-0043).                                │
│   subtasks:                                                                                       │
│     ✓ S-01-domain                  done  · 1 retry                                                │
│     ✓ S-02-events                  done  · 0 retries                                              │
│     ✓ S-03-store                   done  · 0 retries                                              │
│     ▶ S-04-app-services           doing · attempt 1, started 2m ago                              │
│       S-05-api-routes           pending                                                           │
│       S-06-cli-subcommands      pending                                                           │
│       S-07-mcp-tools            pending                                                           │
│       S-08-tui-screens          pending                                                           │
│       S-09-bdd-features         pending                                                           │
│   tail (last 8 lines of /workspace activity):                                                     │
│     [...streaming log lines from the doer's stdout...]                                           │
│                                                                                                   │
│ [r]etry  [a]bort  [o]pen PR  [d]ump (export)  [t]ail log  [v]iew plan  [c]onfig                  │
└───────────────────────────────────────────────────────────────────────────────────────────────────┘
[/]command · ↑↓ select · Enter drill in · Tab swap detail · ? help · q quit
```

### Region purpose

- **Header (always)** — the one-glance answer to "is this run healthy?" Counts + global config + cumulative cost. Pulses red if `blocked > 0`, yellow if any task hasn't progressed in `stall_warn_seconds`.
- **Tasks table** — primary navigation surface. One row per task in the workspace. Sort by state / age / retries / cost. Filter via slash commands (`/sort`, `/filter blocked`).
- **Activity feed** — the last ~20 state-log entries. Color-coded by transition type.
- **Resources panel** — host caps, live container CPU/RSS sparkline, max-parallel computation. The new max-RSS column from `agent_calls`/`container_stats` lives here.
- **Selected detail (Tab toggles)** — drill-in for the highlighted task. Tab cycles: `subtasks` → `agent calls` → `tail log` → `plan`.
- **Command bar (footer)** — slash command input, suggestions (autocomplete from CLI catalog), keybinding hints.

## Slash commands

Catalog. Aliases follow CLI; redirected ones explicit:

| Slash | Behavior in TUI | Notes |
|---|---|---|
| `/run [--only ID]` | Spawn orchestrator in background; live header transitions to "running" | If already running, no-op + toast |
| `/stop` | Send SIGINT to orchestrator; tasks teardown gracefully | TUI-only; no CLI equivalent yet |
| `/status` | Already shown in main view. Bare `/status` → focus tasks panel | |
| `/watch` | Same; bare focus | (legacy alias) |
| `/show <id>` | Focus drill-in on `<id>`; switch detail to "agent calls" | If `<id>` omitted: show selected |
| `/explain <id>` | Modal: deps tree + descendants + state | |
| `/ready` | Modal: list of nodes with all deps merged | |
| `/dag-stats [--by milestone\|layer]` | Modal: bar chart per group | |
| `/briefing` | Modal: same content as CLI briefing, scrollable | |
| `/export <id>` | Run export; toast on completion with path; open in `$EDITOR` if available | |
| `/tail <id>` | Detail panel switches to "tail log" + auto-scroll | |
| `/retry <id>` | Confirm modal → reset state + clean worktree; selected task highlights | If `<id>` omitted: selected |
| `/abort <id>` | Confirm modal → mark ABORTED + cleanup containers | |
| `/mark-merged <id>` | Confirm modal → state→MERGED in store | Useful when manually merging on GitHub |
| `/reset [--close-prs]` | **Strong** confirm; tear down everything, drop SQLite | Destructive — requires double-confirm |
| `/prune` | Run prune; show before/after disk-usage delta | |
| `/disk-usage` | Modal | |
| `/resources` | Modal: live container stats + projected max-parallel | |
| `/subtasks <id>` | Detail panel switches to "subtasks" view | |
| `/clean-containers` | Confirm → cleanup_all_quikode | |
| `/dev-test` | Spawn fixture run in side panel | |
| `/build-image --flavor X` | Spawn docker build with progress stream | |
| `/doctor` | Modal | |
| `/init` | (CLI-only — `quikode init` outside the TUI) | Redirected message |

### TUI-specific slash commands

| Slash | Behavior |
|---|---|
| `/sort [state\|age\|cost\|retries]` | Re-sort tasks panel |
| `/filter <state>` | Show only tasks in given state(s) |
| `/clear-filter` | Show all |
| `/open-pr [<id>]` | `xdg-open` (or pbopen) the task's PR URL |
| `/open-log [<id>]` | Open the per-task log file in `$EDITOR` |
| `/open-worktree [<id>]` | `cd` into the task's worktree dir in a new pane (uses `$SHELL`) |
| `/set-model <phase> <cli>:<model>` | Live-edit the active config; persists to `.quikode/config.toml`. e.g. `/set-model doer claude:claude-opus-4-7` |
| `/set-retry-budget <n>` | Same: live-edit + persist |
| `/set-max-parallel <n>` | Same |
| `/set-stacking [off\|within-milestone\|aggressive]` | Same |
| `/config` | Open full config in `$EDITOR` |
| `/help [<command>]` | Help modal |
| `/keybindings` | Show keymap |
| `/quit` | Exit (= `q`) |

Slash commands that change config (`/set-*`) write through to `.quikode/config.toml` and reload it. The orchestrator picks up changes on next-task scheduling (model swaps don't affect in-flight tasks; that's intentional — too risky mid-run).

## Keybindings

Standard textual conventions; vim-flavored where natural:

```
↑/↓  k/j           select task in tasks table
←/→  h/l           prev/next detail panel tab
PgUp/PgDn          scroll detail
Enter              drill into selected (= /show)
Tab                cycle detail panels (subtasks / agent calls / log / plan / diff)
/                  open command bar
?                  help (full keymap + commands)
q  / Ctrl-C        quit (with confirm if orchestrator is running)
                   ─ task actions (when a row is selected) ─
r                  /retry
a                  /abort
o                  /open-pr
d                  /export (dump)
t                  /tail (toggle tail mode)
v                  /view plan in detail
e                  /explain
m                  /mark-merged
                   ─ global ─
g g                jump to top of tasks
G                  jump to bottom
n                  next task in same state
N                  prev task in same state
.                  refresh / re-poll now
F                  toggle filter mode (then state name)
S                  toggle sort cycle (state → age → cost → retries → state)
:                  same as / (vim folks)
```

Conflict resolution: standard textual reserves `Ctrl-C`. We catch it for the "quit confirm if orchestrator running" prompt.

## Configurable knobs the TUI surfaces

Every config in `.quikode/config.toml` should be editable from a settings modal (`/config` opens the file in `$EDITOR`; the modal is for the high-traffic ones):

```
┌─ Settings (live) ─────────────────────────────────────────────────────────────┐
│                                                                                │
│  General                                                                       │
│    max_parallel               [ 3        ]  ← /set-max-parallel               │
│    base_branch                [ main     ]                                     │
│    triage_budget_per_phase    [ 3        ]  ← /set-retry-budget               │
│    stall_warn_seconds         [ 1800     ]                                     │
│                                                                                │
│  Models                                                                        │
│    planner            claude   claude-opus-4-7         ← /set-model planner   │
│    doer               opencode zai-coding-plan/glm-5.1                         │
│    checker            codex    gpt-5.3-codex                                   │
│    triage             claude   claude-opus-4-7                                 │
│    conflict_resolver  claude   claude-opus-4-7                                 │
│    intent_reviewer    claude   claude-haiku-4-5-20251001                      │
│                                                                                │
│  Subtasks (Phase 0)                                                            │
│    subtask_doer_timeout_s     [ 3600     ]                                     │
│    subtask_checker_timeout_s  [ 600      ]                                     │
│                                                                                │
│  Resources                                                                     │
│    cpu_per_task               [ 4        ]                                     │
│    mem_per_task_gb            [ 12       ]                                     │
│    host_reserved_cpu          [ 4        ]                                     │
│    host_reserved_mem_gb       [ 16       ]                                     │
│    max_parallel_auto          [ ✓ ]                                            │
│                                                                                │
│  Phase A · Conflicts                                                            │
│    auto_resolve               [ ✓ ]                                            │
│    max_resolve_attempts       [ 2        ]                                     │
│                                                                                │
│  Phase B · Intent                                                               │
│    check_on_dep_merge         [ ✓ ]                                            │
│    max_reviews_per_task       [ 5        ]                                     │
│    max_replans                [ 2        ]                                     │
│                                                                                │
│  Phase C · Stacking                                                             │
│    strategy                   [ within-milestone ▾ ]                           │
│    max_depth                  [ 4        ]                                     │
│                                                                                │
│ [Apply]   [Apply + Restart Orchestrator]   [Cancel]                            │
└────────────────────────────────────────────────────────────────────────────────┘
```

"Apply" updates `.quikode/config.toml` and reloads it. "Apply + Restart Orchestrator" stops the running orchestrator (graceful) and starts it back up under the new config — needed for any change that affects in-flight tasks (resource caps, max parallel, agent role assignments).

## Data sources & refresh strategy

The TUI is read-mostly. Three live signals:

1. **SQLite polling**, default 1s. Cheap because:
   - `Store` is WAL — readers don't block writers.
   - Every panel query is bounded (top-N tasks, top-N transitions, current container_stats).
   - Most panels can derive their content from a single `SELECT * FROM tasks` plus `state_log` for the activity feed.
2. **File-watch on per-task logs** (only the selected task's log when in tail mode). Use `watchfiles` (already a textual-friendly dep) or fall back to a 1s poll with `tail -F` semantics.
3. **Docker stats sampling** runs in the *orchestrator*, not the TUI. The TUI just reads the `container_stats` table. (This means stats refresh is bounded by `container_stats_sample_seconds` — default 30s. Configurable.)

No subscriptions, no event bus. The polling is synchronous from the user's perspective but happens inside textual's `set_interval` async loop.

## Implementation map (what files / what tools)

```
quikode/
├── tui/                                  ← new package
│   ├── __init__.py
│   ├── app.py                           ← textual.App subclass; layout + bindings
│   ├── widgets/
│   │   ├── tasks_table.py               ← Tasks panel
│   │   ├── activity_feed.py             ← Activity panel
│   │   ├── resources_panel.py           ← Resources panel
│   │   ├── detail_panel.py              ← Selected-task detail with Tab cycling
│   │   ├── command_bar.py               ← Slash-command input with autocomplete
│   │   ├── settings_modal.py            ← Config editor
│   │   ├── confirm_modal.py             ← Reusable "are you sure" prompt
│   │   └── ...
│   ├── controllers/                     ← actions called by widgets
│   │   ├── orchestrator_control.py      ← spawn/stop running quikode run process
│   │   ├── store_polls.py               ← canonical readers, cached per tick
│   │   ├── command_dispatch.py          ← parse slash → call CLI command directly OR
│   │   │                                  call a quikode/*.py function in-process
│   │   └── shell_actions.py             ← xdg-open, $EDITOR shell-out
│   └── styles/
│       └── quikode.tcss                 ← Textual CSS for panels
├── shells/                              ← rename existing cli.py here? optional
│   └── cli.py                           ← unchanged
└── core/                                ← optional rename to clarify
    └── ...
```

The TUI directly imports `quikode.cli` Typer commands? **No** — that couples shells. Instead, the TUI's `command_dispatch.py` calls the same underlying functions that `cli.py` calls (e.g. `Store(...)`, `Orchestrator(...)`, `worker.TaskWorker(...)`). For commands that have only-CLI shape (e.g. `quikode init` walks a directory tree and writes config), the TUI just shells out via `subprocess` to `quikode <subcommand>`. This keeps the TUI lightweight and the core shell-agnostic.

### Spawning the orchestrator

The orchestrator is a long-running thread. Two designs:

**A.** TUI launches its own orchestrator in a background thread inside the same process. Pro: zero IPC. Con: TUI exit kills the orchestrator (bad UX during long runs).

**B.** TUI spawns `quikode run` as a subprocess (daemonized). Pro: orchestrator survives TUI exits — you can re-attach. Con: needs PID file, log capture, status polling.

**Recommended: B.** Orchestrator is a managed subprocess. TUI tracks PID in `.quikode/orchestrator.pid`. On TUI start, if the PID file exists and is alive, "attach" to the running orchestrator. Otherwise show "no orchestrator running, /run to start." TUI exit leaves orchestrator running (a banner reminds the user).

This also means `quikode run` works standalone (current behavior) and the TUI is purely additive.

## Phasing — what ships when

### v1 (MVP — ships when polished, target: 2-3 days build)

- One window, one workspace.
- Header + tasks panel + activity feed + resources panel + selected-task detail (subtasks tab + agent calls tab + tail tab).
- Slash command bar with autocomplete (mirrors CLI).
- Keybindings: nav, retry, abort, open-pr, open-log, export.
- Settings modal for all config knobs.
- Orchestrator spawn-as-subprocess with PID file attach/detach.
- All slash commands that the existing CLI supports work; TUI-specific commands listed above.

**Explicit non-goals for v1:**
- Real-time agent stdout streaming (use `/tail` which polls the log file).
- Diff view with syntax highlighting (use `/open-worktree` to drop to shell).
- Multi-pane / multi-task simultaneous detail view.
- Cost projections / budgets ("you've used 60% of your daily $ budget").
- Mouse support (Textual has it for free; we don't design around it).

### v1.1 (cheap polish, ~1 day)

- Live agent stdout streaming for the selected task (replaces poll-based tail).
- Sparkline graphs for resource history.
- Notification bell on AWAITING_MERGE / BLOCKED transitions (existing `sound.py` integration).

### v2 (multi-workspace, ~1 week)

- Tabs across workspaces (each tab is its own SQLite + orchestrator subprocess).
- Workspace switcher: `Ctrl-1..9`, `/workspace [name]`, list workspaces in a dropdown.
- Cross-workspace cost rollup in a top header.
- Workspace-aware container labels (already done — `qk_workspace=<hash>`).

### v3 (collaboration, vague)

- Read-only "live link" mode where a remote user can view (not control) the TUI over SSH or web. Useful for pair review of an in-flight run.

## Settled design decisions

These were open questions in earlier drafts. Resolutions, with rationale:

1. **Panel height ratios are dynamic.** Tasks panel grows to ~60% when no task is selected; shrinks when one is highlighted so the detail panel can fill the bottom 35%. Activity and Resources stack to the right with fixed widths (40 cols each by default). This is a layout heuristic, not a config knob — if it grates after a week of use we revisit.

2. **`/stop` is graceful; `/force-quit` is the kill switch.** Two distinct slash commands, no flags. Rationale: keep the slash vocabulary verb-only. The whole appeal of slash commands is they read like a chat to a colleague — "stop", "force-quit", "abort R-001". Flags push that into terse-CLI-mode. We'll never add `--force` to a slash command; if it's worth a different behavior, it's worth its own verb.
   - `/stop` — sends graceful stop signal to orchestrator subprocess; 60s timeout; in-flight tasks reach a checkpoint and tear down their containers.
   - `/force-quit` — SIGKILL the orchestrator subprocess. Containers stranded for `/clean-containers`. Confirm modal: "containers will be stranded; you'll need /clean-containers after".

3. **Confirm-on-quit defaults to keep, with a parting status message.** When the user hits `q` while the orchestrator is running, show a confirm modal: `[k]eep running (default) / [s]top gracefully / [c]ancel`. On TUI exit with the orchestrator still running, print to stderr:

   ```
   quikode is still running in the background.
     orchestrator pid: 12345
     pid file:        .quikode/orchestrator.pid
     log:             .quikode/logs/orchestrator.log
     re-attach:       quikode tui
     stop:            quikode stop
   ```

4. **Dark theme, state-color map fixed:**
   - blue: provisioning / planning / replanning / final_checking
   - green: done / awaiting_merge / merged
   - yellow: triaging / triaging_subtask / rebasing / intent_reviewing
   - red: blocked / failed / conflict_resolving / aborted
   - cyan: doing_subtask / checking_subtask (active work — different from blue planning so the eye distinguishes "agent thinking" vs "agent doing")
   - dim: pending

5. **Settings modal validates via pydantic.** Every config field gets `Field(..., ge=..., le=..., description=...)` and where applicable a `@field_validator`. The settings modal *renders itself from the schema*: number fields with bounds become sliders or steppers, enums become dropdowns, strings with `description=` get inline helptext. Out-of-bounds values show a red inline error and disable Apply. This is an explicit refactor done as the first task of v1 work — see "Implementation prerequisites" below.

6. **Slash command discoverability is fuzzy autocomplete with Tab.** When user types `/`, a popover lists available commands with one-line descriptions (sourced from the same `description=` strings the CLI uses). Fuzzy-matches as they type. Tab autocompletes to the highlighted entry; Enter executes. **The command bar is also where future agentic chat will live** — typed text not starting with `/` is "free text" and reserved for a future supervisory agent (out of scope for v1, but the input surface should support it without restructuring). For v1, free text shows a hint: `(starts with / for commands; chat coming later)`.

## Future direction: supervisory agent in the command bar

(Out of scope for v1; documenting so the v1 design doesn't paint us into a corner.)

The command bar is intentionally one general-purpose text input rather than a button grid. This means we can later layer a supervisory chat agent on top: the user types "why is R-003 still blocked?" and a claude-code/codex/opencode instance with read access to `.quikode/quikode.db` and the per-task logs answers in the activity feed (or a dedicated chat panel). It can also *take* actions via the same slash dispatcher — `/retry R-003 --force` becomes the agent's tool-call result, surfaced to the user as a confirm modal.

This is the same pattern the user used during the bootstrap conversation (a Claude session driving the orchestrator over a CLI). Productizing it inside the TUI:

- v2.5: typed text → routes to supervisory agent; agent has read-only DB + log access; can answer questions and *propose* slash commands but user confirms.
- v3: agent can autonomously execute slash commands inside a sandbox of allowed verbs (e.g. always confirm `/abort`, never confirm `/sort`).
- The supervisory agent is itself configurable per-workspace (`config.supervisor.cli`, `.model`); defaults to claude-haiku-4-5 for cost.

For v1 nothing of this is built. But the input is general-text, not slash-prefix-required; and the slash-dispatch routes through `command_dispatch.py` which is shaped to also be the agent's tool surface in v2.5+.

## Implementation prerequisites (do these before TUI v1)

1. **Pydantic-ize Config.** Right now `Config` is a frozen dataclass-style pydantic model with bare types. To make it the source of truth for the settings modal, every field gets:
   - `Field(default, ge=..., le=..., description="<one line for the modal label>")` for numbers
   - `Field(default, description="...")` for strings/booleans
   - `@field_validator` for cross-field constraints (e.g. `host_reserved_cpu < cpus_per_task`)

2. **Audit existing pydantic models** (`AgentResult`, `IntentReviewOutcome`, `Subtask`, `Plan`) for missing `description=` and `Field` metadata. Even though they're not user-edited, descriptions help future developers and make `model_json_schema()` useful.

3. **Audit `subtask_schema.Subtask`** to ensure parsing is strict (`extra="forbid"`) and produces clear errors when a planner emits a malformed subtask.

These three audits pay off in v1 directly (settings modal "for free") and indirectly (better runtime validation, better error messages).

## Summary

- Mission control with one workspace, one window. Layout = header + tasks + activity + resources + selected-detail + command bar.
- Slash commands mirror the CLI plus TUI-specific (`/open-pr`, `/set-model`, `/sort`, `/filter`, `/workspace` later).
- Single source of truth = SQLite. TUI polls. No new state.
- Orchestrator runs as a subprocess managed by PID file — can survive TUI restarts.
- Settings modal exposes every config knob in the right shape (sliders, dropdowns, text inputs).
- Phasing: v1 MVP, v1.1 polish, v2 multi-workspace, v3 read-only sharing.

Build order for v1:
0. **Pydantic audit pass** — `Field()` + descriptions + validators on `Config`, `AgentResult`, `IntentReviewOutcome`, `Subtask`, `Plan`. Needed by the settings modal and improves runtime safety regardless. Tests assert `model_json_schema()` round-trips and validators reject expected-bad inputs.
1. Skeleton textual app + layout + dummy panels.
2. SQLite reader (cached per tick) + tasks panel.
3. Activity feed + resources panel.
4. Detail panel with Tab cycling.
5. Command bar + slash dispatch + fuzzy autocomplete (start with `/show`, `/retry`, `/abort`, `/open-pr`).
6. Orchestrator subprocess spawn + PID-file attach + parting status message on quit.
7. Settings modal — schema-driven from pydantic.
8. Polish: theme, keybindings, sound integration on transitions.

In-flight slash command additions captured during build:
- `/stop` graceful, `/force-quit` hard kill (separate verbs, no flags).
- Free text in the command bar is reserved for the future supervisory agent; v1 just shows a hint.
