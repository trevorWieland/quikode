"""`quikode briefing` surfaces v3 review-loop / rebase / blocked states.

Phase D extends the briefing groups so the user can wake up and see what
needs them: AWAITING_MERGE (just review/merge), ADDRESSING_FEEDBACK (auto-
working), REBASING_TO_MAIN (auto-working), BLOCKED (needs intervention,
with a `quikode unblock` hint).

Both human-readable and `--json` output are extended.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from quikode.cli import app
from quikode.config import DEFAULT_CONFIG_TOML
from quikode.state import State, Store


def _bootstrap(tmp_path):
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
                        "id": tid,
                        "kind": "behavior",
                        "milestone": "M-1",
                        "title": tid,
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
                    for tid in ("R-AM", "R-RR", "R-RB", "R-BL")
                ],
            }
        )
    )


def _seed_one_per_state(tmp_path):
    store = Store(tmp_path / ".quikode" / "quikode.db")
    # AWAITING_MERGE
    store.upsert_pending("R-AM")
    store.transition(
        "R-AM",
        State.PENDING_CI,
        pr_url="https://github.com/foo/bar/pull/1",
        pr_number=1,
        branch="quikode/r-am",
    )
    # ADDRESSING_FEEDBACK
    store.upsert_pending("R-RR")
    store.transition(
        "R-RR",
        State.ADDRESSING_FEEDBACK,
        pr_url="https://github.com/foo/bar/pull/2",
        pr_number=2,
        branch="quikode/r-rr",
        review_round=3,
    )
    # REBASING_TO_MAIN
    store.upsert_pending("R-RB")
    store.transition(
        "R-RB",
        State.REBASING_TO_MAIN,
        branch="quikode/r-rb",
    )
    # BLOCKED
    store.upsert_pending("R-BL")
    store.transition(
        "R-BL",
        State.BLOCKED,
        last_error="progress flatlined",
        branch="quikode/r-bl",
    )
    store.conn.close()


def test_briefing_human_includes_v3_state_sections(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    _seed_one_per_state(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(app, ["briefing"])
    assert result.exit_code == 0, result.output
    out = result.output
    # All four v3 sections render with their tasks.
    assert "Awaiting merge" in out
    assert "R-AM" in out
    assert "Responding to review" in out
    assert "R-RR" in out
    assert "round 3" in out
    assert "Rebasing onto main" in out
    assert "R-RB" in out
    assert "Blocked — needs intervention" in out
    assert "R-BL" in out
    # The unblock hint is present so the user knows the next step.
    assert "quikode unblock R-BL" in out


def test_briefing_json_groups_v3_states(tmp_path, monkeypatch):
    _bootstrap(tmp_path)
    _seed_one_per_state(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(app, ["briefing", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "awaiting_merge" in payload
    assert "addressing_feedback" in payload
    assert "rebasing_to_main" in payload
    assert "blocked_needs_intervention" in payload

    am_ids = [r["id"] for r in payload["awaiting_merge"]]
    rr_ids = [r["id"] for r in payload["addressing_feedback"]]
    rb_ids = [r["id"] for r in payload["rebasing_to_main"]]
    bl_ids = [r["id"] for r in payload["blocked_needs_intervention"]]
    assert am_ids == ["R-AM"]
    assert rr_ids == ["R-RR"]
    assert rb_ids == ["R-RB"]
    assert bl_ids == ["R-BL"]
    # review_round is preserved on the responding row.
    assert payload["addressing_feedback"][0].get("review_round") == 3


def test_briefing_with_no_v3_states_omits_sections(tmp_path, monkeypatch):
    """Empty state buckets shouldn't print empty headers."""
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(app, ["briefing"])
    assert result.exit_code == 0, result.output
    out = result.output
    # No tasks at all → none of the v3 section headers fire.
    assert "Responding to review" not in out
    assert "Rebasing onto main" not in out
    assert "Blocked — needs intervention" not in out
