"""`quikode show` surfaces progress-check verdicts so operators see FLATLINED
warnings without dropping into sqlite. Closes the gap noted in
`Store.record_progress_check`'s docstring."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from quikode.cli import app
from quikode.config import DEFAULT_CONFIG_TOML
from quikode.state import State, Store


def _bootstrap(tmp_path) -> None:
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
                "nodes": [
                    {
                        "id": "R-001",
                        "kind": "behavior",
                        "milestone": "M-1",
                        "title": "x",
                        "scope": "x",
                        "depends_on": [],
                        "completes_behaviors": [],
                        "supports_behaviors": [],
                        "boundary_with_neighbors": "",
                        "expected_evidence": [],
                        "playbook": [],
                        "rationale": "",
                        "risks": [],
                    }
                ],
            }
        )
    )


def test_show_renders_progress_check_section(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / ".quikode" / "quikode.db"
    store = Store(db)
    store.upsert_pending("R-001")
    store.transition("R-001", State.DOING_SUBTASK)
    store.upsert_subtasks("R-001", [{"subtask_id": "S-01", "title": "x", "acceptance": ["a"]}])
    store.record_progress_check(
        "R-001", "S-01", attempts_at_check=4, verdict="progressing", rationale="adding tests"
    )
    store.record_progress_check(
        "R-001", "S-01", attempts_at_check=7, verdict="flatlined", rationale="same FAIL three times"
    )
    store.conn.close()

    result = CliRunner().invoke(app, ["show", "R-001"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "progress checks" in out
    assert "S-01" in out
    # Latest verdict surfaced
    assert "flatlined" in out
    # Tally accumulates both verdicts
    assert "progressing=1" in out
    assert "flatlined=1" in out
    # Rationale surfaced
    assert "same FAIL three times" in out


def test_show_omits_progress_check_section_when_no_rows(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / ".quikode" / "quikode.db"
    store = Store(db)
    store.upsert_pending("R-001")
    store.conn.close()

    result = CliRunner().invoke(app, ["show", "R-001"])
    assert result.exit_code == 0, result.output
    assert "progress checks" not in result.output


def test_show_renders_per_subtask_cost(tmp_path, monkeypatch):
    """`quikode show` should aggregate agent_calls by subtask_id and show
    call count, duration, and cost beside each subtask line."""
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / ".quikode" / "quikode.db"
    store = Store(db)
    store.upsert_pending("R-001")
    store.upsert_subtasks(
        "R-001",
        [
            {"subtask_id": "S-01", "title": "x", "acceptance": ["a"]},
            {"subtask_id": "S-02", "title": "y", "acceptance": ["b"]},
        ],
    )
    store.record_agent_call(
        "R-001",
        phase="subtask_doer",
        cli="opencode",
        model="glm-5.1",
        rc=0,
        duration_s=420.0,
        tokens_used=None,
        cost_usd=1.50,
        subtask_id="S-01",
    )
    store.record_agent_call(
        "R-001",
        phase="subtask_checker",
        cli="codex",
        model="gpt-5.3-codex",
        rc=0,
        duration_s=60.0,
        tokens_used=1234,
        cost_usd=0.10,
        subtask_id="S-01",
    )
    # No calls yet for S-02 — should not crash and should not show stats.
    store.conn.close()

    result = CliRunner().invoke(app, ["show", "R-001"])
    assert result.exit_code == 0, result.output
    out = result.output
    # S-01: aggregated 2 calls, ~8m, $1.60
    assert "2 calls" in out
    assert "$1.60" in out
    # S-02: no stats shown (no calls yet)
    assert "S-02" in out


def test_show_categorizes_review_threads_by_resolution_source(tmp_path, monkeypatch):
    """`quikode show` distinguishes three review-thread states:
    - addressed: is_resolved=1 + has commit_sha (we pushed a fix)
    - auto-resolved-upstream: is_resolved=1 but commit_sha=NULL
      (e.g. CodeQL re-scan cleared the alert without a quikode commit)
    - unresolved: is_resolved=0 (still needs work)

    Without categorization, operators couldn't tell why a thread shows
    addressed_in_commit_sha=NULL — was it a quikode bug or upstream
    auto-resolution? The new section makes this explicit."""
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / ".quikode" / "quikode.db"
    store = Store(db)
    store.upsert_pending("R-001")
    # Three threads, one of each kind.
    store.upsert_review_thread(
        "R-001",
        thread_id="PRRT_addressed",
        is_resolved=1,
        last_comment_ts=1000.0,
        last_comment_author="codex-bot",
        last_comment_is_bot=1,
    )
    store.mark_thread_addressed("R-001", "PRRT_addressed", "abc1234567")
    store.upsert_review_thread(
        "R-001",
        thread_id="PRRT_upstream",
        is_resolved=1,
        last_comment_ts=2000.0,
        last_comment_author="github-advanced-security",
        last_comment_is_bot=0,
    )
    # No mark_thread_addressed → upstream auto-resolved.
    store.upsert_review_thread(
        "R-001",
        thread_id="PRRT_unresolved",
        is_resolved=0,
        last_comment_ts=3000.0,
        last_comment_author="alice",
        last_comment_is_bot=0,
    )
    store.conn.close()

    result = CliRunner().invoke(app, ["show", "R-001"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "review threads" in out
    assert "addressed=1" in out
    assert "auto-resolved-upstream=1" in out
    assert "unresolved=1" in out
    # Unresolved details listed.
    assert "PRRT_unresolved" in out


def test_show_truncates_long_rationale(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    db = tmp_path / ".quikode" / "quikode.db"
    store = Store(db)
    store.upsert_pending("R-001")
    store.upsert_subtasks("R-001", [{"subtask_id": "S-01", "title": "x", "acceptance": ["a"]}])
    long_rationale = "abcdef " * 50  # 350 chars
    store.record_progress_check(
        "R-001", "S-01", attempts_at_check=4, verdict="uncertain", rationale=long_rationale
    )
    store.conn.close()

    result = CliRunner().invoke(app, ["show", "R-001"])
    assert result.exit_code == 0, result.output
    assert "…" in result.output
