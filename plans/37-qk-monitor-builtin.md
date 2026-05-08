# Plan 37 — `qk monitor` built-in CLI subcommand

## Diagnosis

The operator's state-log monitor lives at `/tmp/qk-monitor.py` — ~110 LoC of
stdlib-only Python that polls `<state_dir>/quikode.db`'s `state_log` table for
rows since `last_seen` and emits one line per "interesting" transition. It was
introduced because `tail -F daemon.log | grep` is flaky (log rotation, Rich's
ANSI escapes, pipe buffering, WSL filesystem quirks). orientation.md §6
currently points readers at `/tmp/qk-monitor.py` from the live session as the
reference implementation.

The friction: every operator (and the manager agent in Claude Code's Monitor
tool) has to copy or rewrite the script before they can use it. The filter
list (`INTERESTING_STATES`, `NOTE_KEYWORDS`, soft-cap `attempt N` regex) has
already drifted once in-session — there's nowhere to durably encode it.

## Decision

Promote the script verbatim into a first-class `qk monitor` subcommand, no
behavior change at the operator-default. Add minimal flags to cover the cases
the ad-hoc script hardcoded by hand (task filter, replay window, custom
keywords, JSON output, one-shot snapshot). Retire the `/tmp` script by
rewriting orientation §6 and the `qk briefing` Hints footer. No daemon
integration — pure read-only CLI.

## Design

**New module** `quikode/cli_monitor.py` (~150 LoC) registered via the existing
Typer pattern (`from .cli_context import app, …`; `@app.command()`). Wired
into `cli.py`'s `_command_modules` tuple alongside the rest. Uses `load_config()`
+ `cfg.state_dir / "quikode.db"` for path resolution — same as every other
read-only command (`status`, `show`, `tail`, `briefing`).

**DB access:** direct `sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)`
each poll, with `con.close()` between polls. **Not** through `Store`, which
opens read-write and runs `apply_migrations()` on construction — incompatible
with watching a daemon-owned DB from a second process. The query is the same
shape as `cli_briefing_dev.py:268`:

```python
con.execute(
    "SELECT task_id, from_state, to_state, note, ts FROM state_log "
    "WHERE ts > ? ORDER BY ts",
    (last_ts,),
).fetchall()
```

**Filter constants** lifted from `/tmp/qk-monitor.py` verbatim into module
constants (`INTERESTING_STATES`, `NOTE_KEYWORDS`, `_ATTEMPT_RE`,
`SOFT_CAP_ATTEMPT = 6`). Encapsulated in a single
`_should_emit(row, filters) -> bool` predicate so the filter logic becomes
unit-testable in isolation.

**CLI shape:**

```
qk monitor                                  # default: tail forever, default filters, 5s interval
qk monitor --once                           # snapshot since --since, exit 0
qk monitor --since 1h                       # replay last hour, then continue tailing
qk monitor --task R-0023                    # only this task's transitions
qk monitor --all                            # no filter (every state_log row)
qk monitor --keywords "blocked:,exhausted"  # extend NOTE_KEYWORDS at runtime
qk monitor --interval 2                     # poll cadence override
qk monitor --format json                    # one JSON object per line (same fields)
```

`--since` accepts the same suffix grammar `qk` uses elsewhere (`30s`, `5m`,
`1h`, `2d`); empty/absent = "now" (= no replay, only new rows). The
`last_seen` cursor lives at `<state_dir>/qk-monitor.lastts` (per-workspace,
not `/tmp`, because workspaces can be parallel). Cursor is **only** persisted
when not in `--once` / `--task` / `--all` modes — those are ad-hoc views and
shouldn't move the long-running tailer's cursor.

**stdout discipline:** every emit goes through `print(..., flush=True)` to
satisfy Claude Code's Monitor tool's line-buffered expectation. SIGINT
returns 130 cleanly (Ctrl-C from the operator's terminal).

**Daemon-down behavior:** the DB exists independent of the daemon, so
`qk monitor` works fine when the daemon isn't running. On startup, print one
informational line to stderr (`[monitor] daemon not running — tailing
state_log only`) when no `orchestrator.pid` is present and continue. This is
the case where the operator most wants to watch transitions catch up after
restart.

## File list

- `quikode/cli_monitor.py` — new module (~150 LoC). Single `@app.command()`
  `monitor()` entrypoint; `_should_emit()` predicate; `_parse_since()` /
  `_save_cursor()` / `_load_cursor()` helpers.
- `quikode/cli.py` — add `cli_monitor` to the imports tuple and
  `_command_modules` (~2 LoC).
- `tests/test_cli_monitor.py` — new file. Cover `_should_emit` truth table:
  interesting state hits, note keyword hits, `attempt 6+` regex hits, empty
  filter on `--all`, `--task` filter narrows correctly,
  drift-from-default-keywords replaceable. Optional end-to-end:
  spawn `qk monitor --once --since 0s` against a temp DB seeded with two
  rows; assert one stdout line per row.
- `orientation.md` (§6, around L194) — replace the "State-log monitor pattern"
  paragraph (text below).
- `quikode/cli_briefing_dev.py:362-366` — add one Hints line:
  `quikode monitor          — tail high-signal state transitions`.

## Orientation §6 replacement text

> **State-log monitor pattern.** When you need a long-running watch for state
> transitions of interest, run **`qk monitor`** — it polls the SQLite
> `state_log` table directly (not the daemon log) and emits one stdout line
> per transition matching the built-in interesting-states / note-keywords /
> soft-cap-attempts filter. Robust to log rotation, ANSI escapes, pipe
> buffering, and WSL filesystem quirks because it never touches the log.
> Useful flags: `--since 1h` to replay, `--task R-NNNN` to narrow,
> `--all` for unfiltered, `--once` for a snapshot, `--format json` for
> tooling. Works whether or not the daemon is running.

## PR sizing

Single PR, ~200 LoC code + ~80 LoC tests + 6-line orientation edit + 1-line
briefing-hint edit. No schema change, no FSM change, no prompt change, no
config knobs added. Stdlib-only (`sqlite3`, `re`, `time`, `pathlib`,
`argparse`-via-Typer); no new dependency.

## Deploy

No `qk retry`, no daemon restart needed beyond the standard reinstall. After
landing:

1. `bash scripts/reinstall.sh --skip-tests`.
2. Operators using `python3 /tmp/qk-monitor.py` switch their Monitor-tool
   command to `qk monitor`. The `/tmp` script can be deleted but isn't
   required to be — they coexist.
3. orientation.md update lands in the same PR so the next manager-agent
   session reads the new pattern.

## Validation

- `ruff check` + `ruff format --check` + `ty check` + `pytest tests/ -q` all
  green.
- `_should_emit` truth-table tests cover each filter predicate independently
  and combined.
- Manual smoke: `qk monitor --once --since 1d` against the live tanren
  workspace produces output identical-up-to-ordering to `python3
  /tmp/qk-monitor.py` run for the same window. (Captured in PR description.)

## Confidence

**High.** The script already exists and works; this is a verbatim promotion
into the existing Typer dispatch with light flag scaffolding. The filter
logic is small and isolated behind a single predicate. The DB access pattern
matches what `cli_briefing_dev.py` and `cli_show_export.py` already do. No
shared state or contract surface is touched. Risk is bounded to the new
module file plus a 2-line edit in `cli.py` and a 1-line edit in
`cli_briefing_dev.py`.
