"""Plan 37: `qk monitor` — tail high-signal state transitions.

Promoted verbatim from `/tmp/qk-monitor.py`. Polls the SQLite `state_log`
table directly (read-only) and emits one stdout line per transition matching
the built-in interesting-states / note-keywords / soft-cap-attempts filter.
Robust to log rotation, ANSI escapes, pipe buffering, and WSL filesystem
quirks because it never touches the daemon log.

Read-only by design: opens `quikode.db` with `mode=ro` (URI form) and never
goes through `Store` (which opens RW + runs migrations on construction —
incompatible with watching a daemon-owned DB from a second process).
"""

from __future__ import annotations

import json as _json
import re
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .cli_context import app, load_config, typer

# ---------- filter constants (lifted verbatim from /tmp/qk-monitor.py) ----------

INTERESTING_STATES: frozenset[str] = frozenset(
    {
        "pr_opening",
        "pending_ci",
        "awaiting_review",
        "merge_ready",
        "merged",
        "blocked",
        "failed",
        "triaging_feedback",
        "addressing_feedback",
        "rebasing_to_main",
    }
)

NOTE_KEYWORDS: tuple[str, ...] = (
    "blocked:",
    "exhausted",
    "InvalidTransition",
    "stop-loss",
    "pre-pr cycle 3",
    "pre-pr cycle 4",
    "pre-pr cycle 5",
    "audit_failed",
    "local_ci_failed",
    "fixup_exhausted",
)

_ATTEMPT_RE = re.compile(r"attempt (\d+)")
SOFT_CAP_ATTEMPT = 6

# `qk` duration grammar: <int><unit> where unit ∈ {s,m,h,d}.
_SINCE_RE = re.compile(r"^\s*(?P<n>\d+)\s*(?P<unit>[smhd])\s*$")
_UNIT_SECS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_since(s: str | None) -> float:
    """Translate a `--since` value to a unix timestamp.

    Empty/None → now (no replay). Accepts the same suffix grammar `qk`
    uses elsewhere: `30s`, `5m`, `1h`, `2d`. Raises `typer.BadParameter`
    on malformed input.
    """
    now = time.time()
    if not s:
        return now
    m = _SINCE_RE.match(s)
    if not m:
        raise typer.BadParameter(f"--since: expected '<n><s|m|h|d>' (e.g. 5m, 1h), got {s!r}")
    return now - int(m.group("n")) * _UNIT_SECS[m.group("unit")]


def _cursor_path(state_dir: Path) -> Path:
    return state_dir / "qk-monitor.lastts"


def _load_cursor(state_dir: Path) -> float | None:
    try:
        return float(_cursor_path(state_dir).read_text().strip())
    except (OSError, ValueError):
        return None


def _save_cursor(state_dir: Path, ts: float) -> None:
    try:
        _cursor_path(state_dir).write_text(f"{ts:.6f}\n")
    except OSError:
        pass


def _should_emit(
    row: sqlite3.Row,
    *,
    all_: bool,
    task_filter: str | None,
    extra_keywords: tuple[str, ...],
) -> bool:
    """Filter predicate. `--all` short-circuits all filters except `--task`.

    Otherwise emit when: `to_state` is interesting, OR `note` contains any
    keyword in `NOTE_KEYWORDS + extra_keywords`, OR `note` mentions
    `attempt N` with `N >= SOFT_CAP_ATTEMPT`.
    """
    if task_filter and row["task_id"] != task_filter:
        return False
    if all_:
        return True
    to_state = row["to_state"]
    note = row["note"] or ""
    if to_state in INTERESTING_STATES:
        return True
    keywords = NOTE_KEYWORDS + extra_keywords
    if any(k in note for k in keywords):
        return True
    am = _ATTEMPT_RE.search(note)
    return bool(am and int(am.group(1)) >= SOFT_CAP_ATTEMPT)


def _format_text(row: sqlite3.Row) -> str:
    when = datetime.fromtimestamp(row["ts"], tz=UTC).astimezone().strftime("%H:%M:%S")
    fr = row["from_state"] or "-"
    to = row["to_state"]
    note = (row["note"] or "")[:120].replace("\n", " ")
    return f"{when} {row['task_id']:8s} {fr} -> {to}  {note}"


def _format_json(row: sqlite3.Row) -> str:
    return _json.dumps(
        {
            "ts": row["ts"],
            "task_id": row["task_id"],
            "from_state": row["from_state"],
            "to_state": row["to_state"],
            "note": row["note"],
        }
    )


def _fetch_rows(db: Path, last_ts: float) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        con.row_factory = sqlite3.Row
        return con.execute(
            "SELECT task_id, from_state, to_state, note, ts FROM state_log WHERE ts > ? ORDER BY ts",
            (last_ts,),
        ).fetchall()
    finally:
        con.close()


def _emit_matching(
    rows: list[sqlite3.Row],
    *,
    all_: bool,
    task_filter: str | None,
    extra_keywords: tuple[str, ...],
    fmt: str,
) -> float | None:
    last_ts: float | None = None
    formatter = _format_json if fmt == "json" else _format_text
    for row in rows:
        if _should_emit(row, all_=all_, task_filter=task_filter, extra_keywords=extra_keywords):
            print(formatter(row), flush=True)
        last_ts = float(row["ts"]) if last_ts is None else max(last_ts, float(row["ts"]))
    return last_ts


@app.command()
def monitor(
    since: str | None = typer.Option(
        None, "--since", help="Replay window: 30s, 5m, 1h, 2d. Default: now (no replay)."
    ),
    task: str | None = typer.Option(None, "--task", help="Only emit transitions for this task id."),
    all_: bool = typer.Option(False, "--all", help="Bypass the interesting-states / keywords filter."),
    keywords: str | None = typer.Option(
        None, "--keywords", help="Comma-separated extra note substrings to match."
    ),
    interval: float = typer.Option(5.0, "--interval", help="Poll interval in seconds."),
    output_format: str = typer.Option(
        "text", "--format", help="Output format: 'text' (default) or 'json' (one object per line)."
    ),
    once: bool = typer.Option(False, "--once", help="Emit one snapshot since --since and exit."),
) -> None:
    """Tail high-signal state transitions from the SQLite `state_log`.

    Read-only; works whether or not the daemon is running. Use `--since 1h`
    to replay, `--task R-NNNN` to narrow, `--all` for unfiltered, `--once`
    for a snapshot, `--format json` for tooling.
    """
    cfg = load_config()
    db = cfg.state_dir / "quikode.db"
    if not db.exists():
        print(f"[monitor] db not found: {db}", file=sys.stderr, flush=True)
        raise typer.Exit(1)
    if output_format not in ("text", "json"):
        raise typer.BadParameter(f"--format: expected 'text' or 'json', got {output_format!r}")
    if not (cfg.state_dir / "orchestrator.pid").exists():
        print(
            "[monitor] daemon not running — tailing state_log only",
            file=sys.stderr,
            flush=True,
        )
    extra_keywords = tuple(k.strip() for k in (keywords or "").split(",") if k.strip())
    persist_cursor = not (once or task or all_)
    cursor = _load_cursor(cfg.state_dir) if persist_cursor else None
    last_ts = cursor if cursor is not None else _parse_since(since)
    try:
        if once:
            rows = _fetch_rows(db, last_ts)
            _emit_matching(
                rows,
                all_=all_,
                task_filter=task,
                extra_keywords=extra_keywords,
                fmt=output_format,
            )
            return
        while True:
            try:
                rows = _fetch_rows(db, last_ts)
            except sqlite3.Error as e:
                print(f"[monitor] sqlite error: {e}", file=sys.stderr, flush=True)
                time.sleep(interval)
                continue
            new_last = _emit_matching(
                rows,
                all_=all_,
                task_filter=task,
                extra_keywords=extra_keywords,
                fmt=output_format,
            )
            if new_last is not None:
                last_ts = new_last
                if persist_cursor:
                    _save_cursor(cfg.state_dir, last_ts)
            time.sleep(interval)
    except KeyboardInterrupt:
        raise typer.Exit(130) from None


__all__ = [
    "INTERESTING_STATES",
    "NOTE_KEYWORDS",
    "SOFT_CAP_ATTEMPT",
    "_load_cursor",
    "_parse_since",
    "_save_cursor",
    "_should_emit",
    "monitor",
]
