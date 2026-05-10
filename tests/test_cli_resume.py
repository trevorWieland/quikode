"""Plan 48: `qk resume <task>` clears retry state on blocked subtasks.

The user's mental model: a manual resume means "give this a fresh
budget" — the operator has explicitly disregarded the prior block, so
the resumed attempt should not inherit the stop-loss history that just
fired.

Pinned contract:
- A blocked subtask's `retry_reasons`, `retries`, `transient_retries`,
  `flatline_count`, and `progress_check_count` are zeroed on resume.
- A done subtask's audit trail (retries / retry_reasons) is preserved.
- A pending subtask (unblocked-by-association) has nothing to clear and
  is left untouched.
"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from quikode.cli import app
from quikode.config_template import DEFAULT_CONFIG_TOML
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


def _seed_blocked_task(tmp_path):
    """Create a BLOCKED R-001 with one DONE subtask, one BLOCKED subtask
    carrying populated retry_reasons + counters, and one PENDING subtask
    held by association. Plan_text is non-empty so resume accepts the
    task."""
    store = Store(tmp_path / ".quikode" / "quikode.db")
    store.upsert_pending("R-001")
    store.transition("R-001", State.PROVISIONING)
    store.transition("R-001", State.PLANNING)
    store.transition("R-001", State.DOING_SUBTASK)
    store.transition("R-001", State.CHECKING_SUBTASK)
    store.transition("R-001", State.TRIAGING_SUBTASK)
    store.transition("R-001", State.BLOCKED, last_error="same-signature stop-loss")
    store.set_field("R-001", plan_text='{"node_id":"R-001","subtasks":[]}')
    store.upsert_subtasks(
        "R-001",
        [
            {"subtask_id": "S-01", "title": "first", "acceptance": ["a"]},
            {"subtask_id": "S-02", "title": "second", "acceptance": ["b"]},
            {"subtask_id": "S-03", "title": "third", "acceptance": ["c"]},
        ],
    )
    store.update_subtask("R-001", "S-01", state="done", retries=2)
    store.update_subtask(
        "R-001",
        "S-02",
        state="blocked",
        retries=5,
        transient_retries=1,
        flatline_count=2,
        progress_check_count=4,
        last_error="same-signature stop-loss: 5 identical FAILs",
        retry_reasons=json.dumps(
            [
                {
                    "attempt": i,
                    "category": "checker_fail",
                    "signature": "verdict=FAIL,layer=local_ci",
                    "transient": False,
                }
                for i in range(1, 6)
            ]
        ),
    )
    store.update_subtask(
        "R-001",
        "S-03",
        state="pending",
        last_error="upstream subtask S-02 blocked",
    )
    store.conn.close()


def test_resume_clears_retry_state_on_blocked_subtask(tmp_path, monkeypatch):
    """The blocked subtask's retry_reasons + counters are cleared so the
    resumed attempt gets a fresh stop-loss budget. The done subtask's
    audit trail is preserved untouched."""
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    _seed_blocked_task(tmp_path)

    result = CliRunner().invoke(app, ["resume", "R-001"])
    assert result.exit_code == 0, result.output
    assert "to redo" in result.output

    store = Store(tmp_path / ".quikode" / "quikode.db")

    # S-02 was blocked → retry state must be cleared.
    s2 = store.get_subtask("R-001", "S-02")
    assert s2["state"] == "pending"
    assert s2["retries"] == 0
    assert s2["transient_retries"] == 0
    assert s2["flatline_count"] == 0
    assert s2["progress_check_count"] == 0
    assert s2["retry_reasons"] is None

    # S-01 was done → retries history preserved verbatim.
    s1 = store.get_subtask("R-001", "S-01")
    assert s1["state"] == "done"
    assert s1["retries"] == 2

    # S-03 was pending (held by association) → no retry history existed,
    # and it was re-pended (state stays pending).
    s3 = store.get_subtask("R-001", "S-03")
    assert s3["state"] == "pending"

    store.conn.close()


def test_resume_summary_line_unchanged_by_clearing(tmp_path, monkeypatch):
    """The (N done · M to redo) summary line counts subtask states, not
    retry-state mutations; the silent layer-clear must not affect it."""
    _bootstrap(tmp_path)
    monkeypatch.chdir(tmp_path)
    _seed_blocked_task(tmp_path)

    result = CliRunner().invoke(app, ["resume", "R-001"])
    assert result.exit_code == 0, result.output
    # 1 done, 2 to redo (1 blocked + 1 pending-held).
    assert "1 done" in result.output
    assert "2 to redo" in result.output
