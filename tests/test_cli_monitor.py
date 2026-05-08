"""Plan 37: `qk monitor` CLI subcommand unit + smoke tests."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from quikode.cli import app
from quikode.cli_monitor import (
    NOTE_KEYWORDS,
    _load_cursor,
    _parse_since,
    _save_cursor,
    _should_emit,
)
from quikode.config_template import DEFAULT_CONFIG_TOML


def _row(**kw) -> sqlite3.Row:
    """Build a sqlite3.Row from a dict-like input via an in-memory query.
    Required because Row's fields are immutable + tied to a cursor."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    cur = con.execute(
        "SELECT :task_id AS task_id, :from_state AS from_state, "
        ":to_state AS to_state, :note AS note, :ts AS ts",
        {
            "task_id": kw.get("task_id", "R-0001"),
            "from_state": kw.get("from_state", "pending"),
            "to_state": kw.get("to_state", "doing"),
            "note": kw.get("note"),
            "ts": kw.get("ts", time.time()),
        },
    )
    return cur.fetchone()


# ---------- _should_emit truth table ----------


def test_should_emit_interesting_state_hits() -> None:
    row = _row(to_state="merged", note="")
    assert _should_emit(row, all_=False, task_filter=None, extra_keywords=()) is True


def test_should_emit_uninteresting_state_misses() -> None:
    row = _row(to_state="doing", note="ordinary progress")
    assert _should_emit(row, all_=False, task_filter=None, extra_keywords=()) is False


def test_should_emit_note_keyword_hits() -> None:
    row = _row(to_state="doing", note="audit_failed: rubric below threshold")
    assert _should_emit(row, all_=False, task_filter=None, extra_keywords=()) is True


def test_should_emit_attempt_six_hits() -> None:
    row = _row(to_state="doing", note="starting attempt 6 of subtask S-01")
    assert _should_emit(row, all_=False, task_filter=None, extra_keywords=()) is True


def test_should_emit_attempt_five_does_not_hit() -> None:
    """Soft cap is 6; attempt 5 must NOT trigger emission."""
    row = _row(to_state="doing", note="starting attempt 5 of subtask S-01")
    assert _should_emit(row, all_=False, task_filter=None, extra_keywords=()) is False


def test_should_emit_attempt_seven_hits() -> None:
    row = _row(to_state="doing", note="retry attempt 7 after triage")
    assert _should_emit(row, all_=False, task_filter=None, extra_keywords=()) is True


def test_should_emit_all_bypasses_filter() -> None:
    row = _row(to_state="doing", note="boring progress")
    assert _should_emit(row, all_=True, task_filter=None, extra_keywords=()) is True


def test_should_emit_task_filter_narrows() -> None:
    row = _row(task_id="R-0023", to_state="merged")
    assert _should_emit(row, all_=False, task_filter="R-0023", extra_keywords=()) is True
    assert _should_emit(row, all_=False, task_filter="R-9999", extra_keywords=()) is False


def test_should_emit_task_filter_blocks_all() -> None:
    """`--task` narrows even with `--all`."""
    row = _row(task_id="R-0023", to_state="doing", note="ordinary")
    assert _should_emit(row, all_=True, task_filter="R-9999", extra_keywords=()) is False


def test_should_emit_extra_keywords_extends() -> None:
    row = _row(to_state="doing", note="custom-marker fired")
    assert _should_emit(row, all_=False, task_filter=None, extra_keywords=()) is False
    assert _should_emit(row, all_=False, task_filter=None, extra_keywords=("custom-marker",)) is True


def test_should_emit_default_keywords_constants_present() -> None:
    """Sanity: the default NOTE_KEYWORDS list is non-empty + matches a known
    keyword. Catches accidental constant deletion in refactors."""
    assert "audit_failed" in NOTE_KEYWORDS
    assert "exhausted" in NOTE_KEYWORDS


# ---------- _parse_since ----------


def test_parse_since_none_is_now() -> None:
    before = time.time()
    got = _parse_since(None)
    after = time.time()
    assert before <= got <= after


def test_parse_since_empty_is_now() -> None:
    before = time.time()
    got = _parse_since("")
    after = time.time()
    assert before <= got <= after


@pytest.mark.parametrize(
    ("expr", "secs"),
    [("30s", 30), ("5m", 300), ("1h", 3600), ("2d", 172_800)],
)
def test_parse_since_units(expr: str, secs: int) -> None:
    now = time.time()
    got = _parse_since(expr)
    # Expect ~now-secs, allow 1s slack for test execution.
    assert now - secs - 1 <= got <= now - secs + 1


def test_parse_since_malformed_raises() -> None:
    with pytest.raises(typer.BadParameter):
        _parse_since("garbage")


# ---------- cursor round-trip ----------


def test_cursor_round_trip(tmp_path: Path) -> None:
    assert _load_cursor(tmp_path) is None
    _save_cursor(tmp_path, 1234567.5)
    assert _load_cursor(tmp_path) == pytest.approx(1234567.5)


def test_cursor_corrupt_returns_none(tmp_path: Path) -> None:
    (tmp_path / "qk-monitor.lastts").write_text("not-a-float\n")
    assert _load_cursor(tmp_path) is None


# ---------- end-to-end smoke: --once against a temp DB ----------


def _bootstrap_workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace with .quikode/config.toml + dag.json + db."""
    qkdir = tmp_path / ".quikode"
    qkdir.mkdir()
    (qkdir / "config.toml").write_text(
        DEFAULT_CONFIG_TOML.format(repo_path=str(tmp_path), dag_path=str(tmp_path / "dag.json"))
    )
    (tmp_path / "dag.json").write_text(
        json.dumps(
            {
                "schema": "test",
                "milestones": [{"id": "M-1", "title": "x", "goal": "x", "status": "planned"}],
                "nodes": [],
            }
        )
    )
    return qkdir


def _seed_state_log(db_path: Path, rows: list[dict]) -> None:
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE state_log (task_id TEXT, from_state TEXT, to_state TEXT, note TEXT, ts REAL)")
    con.executemany(
        "INSERT INTO state_log (task_id, from_state, to_state, note, ts) "
        "VALUES (:task_id, :from_state, :to_state, :note, :ts)",
        rows,
    )
    con.commit()
    con.close()


def test_monitor_once_emits_two_rows(tmp_path: Path, monkeypatch) -> None:
    qkdir = _bootstrap_workspace(tmp_path)
    db = qkdir / "quikode.db"
    now = time.time()
    _seed_state_log(
        db,
        [
            {
                "task_id": "R-0001",
                "from_state": "pending",
                "to_state": "merged",  # interesting
                "note": "first transition",
                "ts": now - 100,
            },
            {
                "task_id": "R-0002",
                "from_state": "doing",
                "to_state": "blocked",  # interesting
                "note": "second transition",
                "ts": now - 50,
            },
        ],
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["monitor", "--once", "--since", "1h"])
    assert result.exit_code == 0, result.output
    # Two interesting transitions seeded -> two stdout lines.
    out_lines = [line for line in result.output.splitlines() if "->" in line]
    assert len(out_lines) == 2, result.output
    assert "R-0001" in result.output
    assert "R-0002" in result.output


def test_monitor_once_json_format(tmp_path: Path, monkeypatch) -> None:
    qkdir = _bootstrap_workspace(tmp_path)
    db = qkdir / "quikode.db"
    now = time.time()
    _seed_state_log(
        db,
        [
            {
                "task_id": "R-0001",
                "from_state": "pending",
                "to_state": "merged",
                "note": None,
                "ts": now - 30,
            },
        ],
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["monitor", "--once", "--since", "1h", "--format", "json"])
    assert result.exit_code == 0, result.output
    json_lines = [line for line in result.output.splitlines() if line.startswith("{")]
    assert len(json_lines) == 1, result.output
    payload = json.loads(json_lines[0])
    assert payload["task_id"] == "R-0001"
    assert payload["to_state"] == "merged"
