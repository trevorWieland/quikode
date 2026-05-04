"""Item 7: ccusage cost surfacing in `quikode briefing`.

`agent_calls.cost_usd` per row → per-task totals + workspace totals
should appear in the briefing output. Verifies the dollar-formatted
strings render where expected.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from quikode.cli import app
from quikode.dag import DAG
from quikode.state import State, Store


def _write_dag(tmp_path: Path) -> DAG:
    raw = {
        "schema": "test",
        "milestones": [{"id": "M-1", "title": "x", "goal": "x", "status": "planned"}],
        "nodes": [
            {
                "id": nid,
                "kind": "behavior",
                "milestone": "M-1",
                "title": nid,
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
            for nid in ("T-001", "T-002")
        ],
    }
    p = tmp_path / "dag.json"
    p.write_text(json.dumps(raw))
    return DAG.load(p)


def _init_workspace(tmp_path: Path) -> tuple[Path, Store]:
    """Create a minimal .quikode/config.toml + db so `quikode briefing` runs."""
    cfg_dir = tmp_path / ".quikode"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    dag = _write_dag(tmp_path)
    cfg_path = cfg_dir / "config.toml"
    cfg_path.write_text(
        f'repo_path = "{tmp_path}"\n'
        f'dag_path = "{dag.path if hasattr(dag, "path") else tmp_path / "dag.json"}"\n'
    )
    db = Store(cfg_dir / "quikode.db")
    return cfg_path, db


def _record_cost(db: Store, task_id: str, cli: str, cost: float) -> None:
    db.record_agent_call(
        task_id,
        phase="planner",
        cli=cli,
        model="some-model",
        rc=0,
        duration_s=10.0,
        tokens_used=1000,
        tokens_input=900,
        tokens_output=100,
        tokens_cached_read=0,
        tokens_cached_creation=0,
        cost_usd=cost,
    )


def test_task_total_cost_helper(tmp_path):
    _, db = _init_workspace(tmp_path)
    db.upsert_pending("T-001")
    _record_cost(db, "T-001", "claude", 0.42)
    _record_cost(db, "T-001", "codex", 0.18)
    _record_cost(db, "T-002", "claude", 1.50)
    assert abs(db.task_total_cost_usd("T-001") - 0.60) < 0.001
    assert abs(db.task_total_cost_usd("T-002") - 1.50) < 0.001
    assert db.task_total_cost_usd("T-XXX") is None
    db.conn.close()


def test_workspace_total_cost_helper(tmp_path):
    _, db = _init_workspace(tmp_path)
    db.upsert_pending("T-001")
    _record_cost(db, "T-001", "claude", 0.50)
    _record_cost(db, "T-002", "codex", 0.30)
    assert abs(db.workspace_total_cost_usd() - 0.80) < 0.001
    db.conn.close()


def test_workspace_total_cost_none_when_empty(tmp_path):
    _, db = _init_workspace(tmp_path)
    assert db.workspace_total_cost_usd() is None
    db.conn.close()


def test_briefing_renders_per_task_and_total_cost(tmp_path, monkeypatch):
    """End-to-end: drive `quikode briefing` and check the rendered text."""
    _cfg_path, db = _init_workspace(tmp_path)
    db.upsert_pending("T-001")
    db.upsert_pending("T-002")
    db.transition("T-001", State.AWAITING_MERGE, branch="quikode/t-001-aaa", pr_number=1)
    db.transition("T-002", State.MERGED, branch="quikode/t-002-bbb", pr_number=2)
    _record_cost(db, "T-001", "claude", 0.42)
    _record_cost(db, "T-002", "codex", 1.05)
    db.conn.close()

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["briefing"])
    assert result.exit_code == 0, result.output
    out = result.output
    # Per-task awaiting-merge cost
    assert "$0.42" in out
    # Recent merges block
    assert "Recent merges" in out
    assert "$1.05" in out
    # Workspace total
    assert "total: $1.47" in out


def test_briefing_zero_cost_omits_dollars(tmp_path, monkeypatch):
    """When cost_usd is NULL on every call, no dollar amount renders."""
    _cfg_path, db = _init_workspace(tmp_path)
    db.upsert_pending("T-001")
    db.transition("T-001", State.AWAITING_MERGE, branch="quikode/t-001-aaa", pr_number=1)
    db.record_agent_call(
        "T-001",
        phase="planner",
        cli="claude",
        model="x",
        rc=0,
        duration_s=10.0,
        tokens_used=100,
        # no cost_usd
    )
    db.conn.close()

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["briefing"])
    assert result.exit_code == 0, result.output
    # No dollar sign anywhere
    assert "$" not in result.output
