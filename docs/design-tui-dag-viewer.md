# TUI DAG viewer — design proposal

> **Status: IMPLEMENTED v1.** This document is kept as architectural reference for the design decisions.
> Current architecture lives in [`architecture.md`](architecture.md). Launch with `quikode tui` then press `g`.

A whole-project visualization. The mission-control dashboard tells you
"what's happening right now"; the DAG viewer tells you "where are we in
the journey." Different question, different surface.

## What problem this solves

Today the TUI's tasks panel shows ~5-15 rows: the tasks currently in
flight, awaiting merge, blocked, or pending. **It tells you nothing about
the 200+ nodes that aren't yet in the store** — the user has to jump to
`quikode briefing` or `quikode dag-stats` and reconstruct progress in
their head. Specifically missing today:

- Which milestone is the active work in?
- What % of M-0001 is merged vs. M-0002?
- Which behaviors does R-0001 unblock — and how many are downstream?
- If R-0001 lands cleanly, what's the next 5-10 tasks in dependency order?
- Where are the "tall" parts of the DAG (long single-file dep chains) vs.
  "wide" parts (parallel-friendly fan-out)?

The DAG viewer is a separate screen (push from the main TUI via `/dag`
or a key like `g`) that answers these.

## Proposed surfaces

### Mode 1: Milestone overview (default landing)

```
┌─ DAG · 2/233 merged (0.9%) ──────────────────────────── press / for command ─┐
│ M-0001 · Foundation                            ████████░░░░░░░░░  2/4   50% │
│ M-0002 · Identity & Access                     ░░░░░░░░░░░░░░░░░  0/18   0% │
│ M-0003 · Workspace bootstrap                   ░░░░░░░░░░░░░░░░░  0/12   0% │
│ M-0004 · Behavior catalog                      ░░░░░░░░░░░░░░░░░  0/22   0% │
│ M-0005 · Behavior proof                        ░░░░░░░░░░░░░░░░░  0/19   0% │
│ M-0006 · Roadmap automation                    ░░░░░░░░░░░░░░░░░  0/8    0% │
│ M-0007 · Architecture validators               ░░░░░░░░░░░░░░░░░  0/14   0% │
│ M-0008 · ...                                                                 │
│                                                                              │
│ Right now:                                                                   │
│   in flight: R-0001 (M-0001) · doing_subtask · 8m23s · S-02/8                │
│   awaiting:  none                                                            │
│   blocked:   none                                                            │
│   ready (DAG): R-0023 (M-0003)                                               │
│                                                                              │
│ ↑↓ pick milestone · Enter drill in · g toggle layout · q back to dashboard   │
└──────────────────────────────────────────────────────────────────────────────┘
```

Bar segments are colored by node state:
- green = merged
- bright cyan = in flight (provisioning/doing/checking/etc.)
- yellow = awaiting merge
- red = blocked / failed
- dim = pending or not yet seeded

### Mode 2: Single-milestone detail (Enter on a milestone)

```
┌─ M-0001 · Foundation · 2/4 merged ───────────────────────────────────────────┐
│                                                                              │
│   F-0001  ✓ merged    Foundation Spec — Minimum Buildable Tanren             │
│   F-0002  ✓ merged    Foundation Correction (HTTP MCP, BDD, validators)      │
│   R-0001  ▶ doing     Create an account and sign in    [3 deps · 47 unblocks]│
│   R-0023  ⋯ pending   Bootstrap Tanren assets ...      [4 deps · 12 unblocks]│
│                                                                              │
│   Behaviors completed by this milestone:                                     │
│     B-0001 (sign-in)   B-0002 (sign-out)   B-0003 (acct-recovery)            │
│     B-0023 (workspace-init)   ... 14 more                                    │
│                                                                              │
│   This milestone unlocks:                                                    │
│     M-0002 (Identity & Access) — 18 nodes ready when M-0001 fully merged     │
│     M-0003 (Workspace bootstrap) — 12 nodes ready                            │
│                                                                              │
│ ↑↓ pick task · Enter drill in (or jump back to dashboard for that task) ·    │
│ g graph · b backward · q back                                                │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Mode 3: Graph view (`g` toggle from any mode)

ASCII-art DAG layout. For 233 nodes this won't fit on one screen — but a
*scrollable* DAG, with the current cursor task as anchor and a couple of
hops in each direction visible, is genuinely useful for "what's next?"
intuition.

```
┌─ Graph view · anchor R-0001 · ↑↓←→ pan · zoom +/- ───────────────────────────┐
│                                                                              │
│   F-0001 ─────┬───── F-0002 ─────┬─── R-0001 ▶───┬─── R-0002 ─── R-0006      │
│               │                  │               ├─── R-0003                  │
│               │                  │               ├─── R-0004                  │
│               │                  │               └─── R-0005 ─── R-0007       │
│               │                  └─── R-0023 ─── R-0024                       │
│               │                                                              │
│               └──── F-0003 (M-0001 follow-up) — ⋯ pending                    │
│                                                                              │
│ Legend: ✓ merged  ▶ in flight  ⏸ awaiting  ✗ blocked  ⋯ pending  + unseeded  │
└──────────────────────────────────────────────────────────────────────────────┘
```

The graph layer plug:
- We already have `quikode dag` rendering trees in CLI; reuse that logic.
- Layout: a topological-rank-based Sugiyama-style algorithm. Each node
  gets a rank = longest path from a root + a column index that minimizes
  edge crossings. Cheap to compute (~O(n+e)).
- For 233 nodes the full graph is ~80 cols wide × 30 ranks tall — fits a
  modern terminal with horizontal pan.
- "Critical path" highlighting: starting from any node, color the deepest
  unmerged dependency chain in cyan. Tells you "to land R-0050, here's
  the longest pole."

## What goes in v1 vs v2

**Reordered after user feedback (2026-05-02)**: the actual DAG is
node-level. Milestones are loose collections of R-* nodes — useful as a
secondary lens but not the primary semantics. The graph view jumps to v1.

### v1 — node-level graph + headline stats (~3-4 days)

This is what ships first. The user's words: "what is the state of the
project at large, like with all 200+ nodes in the DAG, some form of color
coded node and edge graph viewer, with maybe some high level stats like
remaining depth, estimated total token costs based on rolling averages,
estimates on time to finish based on parallel settings and DAG shape."

Components:

1. **Color-coded node + edge ASCII graph**
   - Sugiyama-style layout: rank = longest dep path from a root, column
     index minimizes edge crossings.
   - For 233 nodes, expected canvas: ~30 ranks tall × 20-30 columns wide.
     Doesn't fit one screen; pan with `←→↑↓`, jump-to-anchor with `/<id>`
     in the command bar.
   - Node glyph carries state color: `✓` green merged · `▶` cyan in flight
     · `⏸` yellow awaiting · `✗` red blocked · `⋯` dim pending.
   - Edges are plain ASCII (`─`, `│`, `┬`, `┴`, `┼`, `└`, `┘`, `├`, `┤`).
     Edges to merged nodes render dim; edges into in-flight or ready
     nodes render in cyan; edges into blocked nodes render red.
   - Critical-path mode (`c` to toggle): from the cursor, highlight the
     deepest unmerged dep chain. Tells you "to land R-0050, this is the
     longest pole."

2. **Headline stats panel** (right-hand sidebar, ~30 cols wide):
   ```
   Project depth:   12 ranks   (max longest path)
   Remaining depth: 11 ranks   (from any unmerged node)
   Nodes:           2 / 233 merged · 0 in-flight · 1 ready
   Behaviors:       0 / 73 proven  (B-XXXX with passing scenarios)

   Cost so far:     $4.23      (claude · sum across agent_calls)
   Avg per R-*:     $1.06      (rolling, last 5 merged)
   Projected total: ~$245      ← (231 unmerged × avg)

   Time per R-*:    ~38 min    (rolling, last 5)
   ETA serial:      6d 4h
   ETA @ N=3:       2d 1h      (parallel-aware ceiling)
   ETA @ N=5:       1d 6h
   ```
   Stats refresh every 10s (cheaper than the 1s dashboard tick).

3. **Filter modes** — type `/` to enter command mode within the DAG
   screen:
   - `/filter blocked` — show only blocked + their unblocked-by descendants
   - `/filter ready` — only nodes whose deps are all merged
   - `/filter milestone M-0001` — nodes in one milestone
   - `/anchor R-0050` — center the view on a specific node
   - `/clear` — back to full graph
   - These mirror the dashboard's slash conventions.

4. **Keybindings inside the DAG screen**:
   - `↑↓←→` or `hjkl` pan
   - `+`/`-` zoom (compact / loose layout — affects whether sibling nodes
     pack tightly or spread)
   - `Enter` on cursor node — pop the DAG screen and select that task in
     the dashboard
   - `c` toggle critical-path overlay
   - `m` toggle milestone-grouping overlay (boxes around nodes by
     milestone — secondary lens)
   - `q`/`Esc` back to dashboard

### v1.1 — milestone overlay + behavior rollup (~1 day)

Once the graph works, layer in the milestone view as an *overlay*, not
the primary surface:

- `m` keybinding draws a soft-bordered box around all nodes in each
  milestone, with the milestone id + progress count in the corner.
- A condensed milestone-progress sidebar replaces the headline stats
  when toggled (`s` to swap), giving the rolled-up view from the
  original Mode 1 design.
- Behavior coverage row in the headline stats (B-XXXX completion
  count from `completes_behaviors` × scenario presence in
  `tests/bdd/features/`).

### v2 — interactive editing + polish

- Mouse support: click a node to anchor.
- Inline cost-by-task tooltip (hover or `i` to inspect).
- Fork view: "if I started R-0050, which other R-* could I parallelize
  with right now?" (set difference of ready nodes touching disjoint
  crate groups).
- Save filter as a named view (`/save-filter blocked-paths`).

## Implementation sketch (v1)

Layered build — graph rendering is the harder piece, so isolate it in a
pure-Python module that's testable without textual.

```
quikode/tui/dag_view/
├── __init__.py
├── layout.py         # ranks(dag), columns(dag), edge_routing(...) — no UI
├── render.py         # ascii_canvas(layout, state_map, filter) → str (or list[Strip])
├── stats.py          # depth, rolling_avg_cost, parallel_eta — pure functions
└── screen.py         # DAGScreen (textual) — composes everything
```

### `layout.py` (pure)
- `ranks(dag) -> dict[node_id, int]` — longest path from any root.
- `columns(dag, ranks) -> dict[node_id, int]` — Coffman-Graham-ish, with
  median-heuristic crossing reduction. ~150 LOC.
- `edges(dag, ranks, columns) -> list[Edge]` where `Edge` carries source,
  target, intermediate cells. Long edges get split through dummy
  same-rank cells so the renderer doesn't draw through nodes.

### `render.py` (pure)
- `ascii_canvas(layout, states, *, filter, anchor, viewport) -> Canvas`
  where `Canvas` is a 2D grid of cells, each carrying a glyph + style.
- `Canvas.to_strips(...)` produces textual `Strip`s with markup applied
  per cell. Renderer is a textual `Widget` that consumes strips.
- Edge characters chosen by adjacency (corners, T-joins) — standard
  graphviz-style ASCII rendering.

### `stats.py` (pure)
```python
@dataclass(frozen=True)
class HeadlineStats:
    project_depth: int
    remaining_depth: int
    merged: int
    in_flight: int
    ready: int
    cost_so_far_usd: float | None
    avg_cost_per_node_usd: float | None
    projected_total_usd: float | None
    avg_runtime_per_node_s: float | None
    eta_serial_s: float | None
    eta_parallel_s: dict[int, float]  # {2: ..., 3: ..., 5: ...}

def compute_headline_stats(dag, store, *, max_parallel_choices=(1, 3, 5)) -> HeadlineStats: ...
```
Rolling averages use the last 5 merged R-* nodes (configurable). Projected
total = unmerged_count × avg_cost. Parallel ETA uses a critical-path
calculation: ceil(remaining work / N) where work counts in
node-runtime-seconds, bounded below by the longest unmerged chain.

### `screen.py` (textual)
- `DAGScreen` is a textual `ModalScreen` pushed via `g` from the main
  dashboard or `/dag`.
- Subscribes to the same store-poll signal as the dashboard, but at a
  longer interval (10s instead of 1s — DAG state changes slowly).
- Layout: graph widget (left, fr), stats sidebar (right, ~30 cols).
  Same docked command bar at the bottom as the main dashboard so slash
  commands feel consistent.

### Tests
- `test_dag_layout.py` — small fixtures (5-node, 10-node DAGs), assert
  rank/column assignments are correct, no edge crosses through a node,
  edge counts match dep counts.
- `test_dag_stats.py` — fake store with hardcoded merged nodes + agent
  calls, assert rolling-avg cost is correct, parallel ETA math.
- `test_dag_screen.py` — Pilot smoke test: open screen, assert glyphs
  for known states render in the expected colors, `Enter` drills back.

## Open questions

1. **What renders the progress bar?** Textual has a built-in `ProgressBar`
   widget (smooth, color-aware) but it's per-row. For 14 milestones I'd
   prefer a static text-based bar with `█` and `░` so the layout is dense.
   I'd default to text and revisit if it looks crusty.
2. **Behavior overlay?** Behaviors (B-XXXX) are a parallel concept to
   tasks. A milestone "completes" a set of behaviors via its R-* nodes.
   Should the milestone overview show behavior coverage % too, or is
   that a separate "behavior coverage" mode? My instinct: separate. Don't
   conflate them in v1.
3. **DAG viewer for *runs*, not just current state?** The `state_log`
   table has every transition; we could replay. But this is a "history"
   feature, separate concern. Defer.

## Why this is worth doing

The TUI today optimizes for "what's in flight RIGHT NOW." That's the
high-frequency view. But every few hours / days the user wants to step
back: "how am I doing on the whole project?" Dropping to `quikode briefing`
+ `dag-stats --by milestone` works, but breaks the cockpit metaphor.
The DAG viewer keeps the metaphor — same window, different lens — and
lets the user pivot between "do" and "where am I" without context switch.

Also, once R-0001 lands, the next decision is "which task to start next."
The right answer depends on:
- Which milestone needs progress most?
- Which downstream nodes have the largest unblock fan-out?
- Are any tasks ready that are particularly cheap (small scope, well-trodden)?

The DAG viewer surfaces all three.
