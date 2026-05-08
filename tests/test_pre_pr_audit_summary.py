"""Store helpers + TUI rendering for the pre-PR audit summary.

The Store side (begin_pre_pr_audit_cycle / update_pre_pr_audit_stage /
get_pre_pr_audit_summary) is exercised against a real sqlite DB. The
TUI side (_gauntlet_block) is exercised against synthesized
DetailSnapshots to validate the rendered markup carries the right
icons + status text.
"""

from __future__ import annotations

from quikode.state import State, Store
from quikode.tui.widgets.detail_panel import DetailSnapshot, _gauntlet_block, _state_long_description


def test_begin_cycle_seeds_four_queued_stages(tmp_path):
    store = Store(tmp_path / "q.db")
    store.upsert_pending("R-001")
    store.begin_pre_pr_audit_cycle("R-001", 1)
    summary = store.get_pre_pr_audit_summary("R-001")
    assert summary is not None
    assert summary["cycle"] == 1
    names = [s["name"] for s in summary["stages"]]
    assert names == ["local_ci", "rubric", "standards", "behavior"]
    # All seeded as queued (passed=None, summary="queued").
    assert all(s["passed"] is None for s in summary["stages"])
    assert all(s["summary"] == "queued" for s in summary["stages"])


def test_update_stage_sets_pass_status(tmp_path):
    store = Store(tmp_path / "q.db")
    store.upsert_pending("R-001")
    store.begin_pre_pr_audit_cycle("R-001", 1)
    store.update_pre_pr_audit_stage("R-001", cycle=1, stage_name="local_ci", passed=True, summary="rc=0")
    store.update_pre_pr_audit_stage(
        "R-001", cycle=1, stage_name="rubric", passed=False, summary="security=5 < 7"
    )
    summary = store.get_pre_pr_audit_summary("R-001")
    by_name = {s["name"]: s for s in summary["stages"]}
    assert by_name["local_ci"]["passed"] is True
    assert by_name["local_ci"]["summary"] == "rc=0"
    assert by_name["rubric"]["passed"] is False
    assert "security=5" in by_name["rubric"]["summary"]
    # Stages we didn't touch stay queued.
    assert by_name["standards"]["passed"] is None
    assert by_name["behavior"]["passed"] is None


def test_update_stage_lazy_seeds_when_no_cycle_open(tmp_path):
    """Defensive: caller forgot to call begin_pre_pr_audit_cycle but called
    update_pre_pr_audit_stage. The Store seeds lazily so the stage update
    still lands."""
    store = Store(tmp_path / "q.db")
    store.upsert_pending("R-001")
    store.update_pre_pr_audit_stage("R-001", cycle=2, stage_name="rubric", passed=True, summary="ok")
    summary = store.get_pre_pr_audit_summary("R-001")
    assert summary is not None
    assert summary["cycle"] == 2
    rubric = next(s for s in summary["stages"] if s["name"] == "rubric")
    assert rubric["passed"] is True


def test_new_cycle_clears_prior_results(tmp_path):
    """Fixup loop ran cycle 1, now we re-enter the pipeline at cycle 2.
    The prior cycle's stage outcomes shouldn't bleed into the cycle-2
    display."""
    store = Store(tmp_path / "q.db")
    store.upsert_pending("R-001")
    store.begin_pre_pr_audit_cycle("R-001", 1)
    store.update_pre_pr_audit_stage(
        "R-001", cycle=1, stage_name="local_ci", passed=False, summary="cargo error"
    )
    # New cycle.
    store.begin_pre_pr_audit_cycle("R-001", 2)
    summary = store.get_pre_pr_audit_summary("R-001")
    assert summary["cycle"] == 2
    # All stages reset to queued — the cycle-1 local_ci=False is gone.
    assert all(s["passed"] is None for s in summary["stages"])


# ----- TUI render helpers -----


def test_state_long_description_known():
    # Plan 38 PR-C: state long-descriptions describe the FSM phase
    # ("per-subtask checker phase"), NOT a synthesized agent-running
    # status ("running per-subtask checker"). Live agent in-flight vs
    # idle is rendered separately from `agent_calls` observed reality.
    assert _state_long_description("local_ci_checking") == "local CI gate (just ci)"
    assert _state_long_description("checking_subtask") == "per-subtask checker phase"
    assert _state_long_description("fixup_planning") == "planning fixup subtasks"
    # Self-explanatory states return None.
    assert _state_long_description("pending") is None
    assert _state_long_description("merged") is None


def test_gauntlet_block_returns_none_when_no_summary():
    snap = DetailSnapshot(task_id="R-001")
    assert _gauntlet_block(snap) is None


def test_gauntlet_block_renders_pass_fail_queued_icons():
    snap = DetailSnapshot(
        task_id="R-001",
        # plan 26: gauntlet only renders when current state is in the
        # pipeline-relevant set; pre_pr_auditing is the canonical "render"
        # state.
        task_state="pre_pr_auditing",
        pre_pr_audit_cycle=2,
        pre_pr_audit_stages=[
            {"name": "local_ci", "passed": True, "summary": "rc=0"},
            {"name": "rubric", "passed": False, "summary": "security=5 < 7"},
            {"name": "standards", "passed": None, "summary": "queued"},
            {"name": "behavior", "passed": None, "summary": ""},
        ],
    )
    block = _gauntlet_block(snap)
    assert block is not None
    assert "cycle 2" in block
    # local_ci: pass icon + status
    assert "✓" in block
    assert "passed" in block
    # rubric: fail icon
    assert "✗" in block
    assert "failed" in block
    # standards: queued icon (·) since summary == "queued"
    # behavior: running indicator (…) since summary is empty (in-flight)
    assert "queued" in block
    assert "running" in block


def test_gauntlet_block_handles_missing_stage_gracefully():
    """A summary with only 2 stages (older row layout, partial cycle) should
    still render without raising — missing stages are simply skipped."""
    snap = DetailSnapshot(
        task_id="R-001",
        task_state="pre_pr_auditing",  # plan 26: pipeline-relevant state
        pre_pr_audit_cycle=1,
        pre_pr_audit_stages=[
            {"name": "local_ci", "passed": True, "summary": "rc=0"},
            {"name": "rubric", "passed": True, "summary": "all categories ≥ 7"},
        ],
    )
    block = _gauntlet_block(snap)
    assert block is not None
    assert "local_ci" not in block  # we render the human label, not the raw name
    assert "local CI gate" in block
    assert "rubric audit" in block


def test_gauntlet_block_hides_when_state_is_not_pipeline_relevant():
    """Plan 26: when the task is back in spec/fixup-subtask work after a
    prior cycle ran, the persisted audit summary represents history, not
    current state. The gauntlet panel should hide rather than mislead the
    operator with stale data."""
    stages = [
        {"name": "local_ci", "passed": False, "summary": "rc=1"},
        {"name": "rubric", "passed": False, "summary": "security<7"},
        {"name": "standards", "passed": None, "summary": "queued"},
        {"name": "behavior", "passed": None, "summary": "queued"},
    ]
    # Subtask phase states — should hide.
    for hide_state in (
        "doing_subtask",
        "checking_subtask",
        "triaging_subtask",
        "planning",
        "pending",
        "provisioning",
    ):
        snap = DetailSnapshot(
            task_id="R-001",
            task_state=hide_state,
            pre_pr_audit_cycle=1,
            pre_pr_audit_stages=stages,
        )
        assert _gauntlet_block(snap) is None, f"expected None for state {hide_state!r}"

    # Pipeline / terminal states — should render.
    for show_state in (
        "pre_pr_auditing",
        "local_ci_checking",
        "fixup_planning",
        "pending_ci",
        "merged",
        "blocked",
        "failed",
    ):
        snap = DetailSnapshot(
            task_id="R-001",
            task_state=show_state,
            pre_pr_audit_cycle=1,
            pre_pr_audit_stages=stages,
        )
        assert _gauntlet_block(snap) is not None, f"expected block for state {show_state!r}"


def test_block_transition_does_not_clobber_audit_summary(tmp_path):
    """The audit summary persists through a BLOCK; the operator wants to see
    'last cycle had standards=fail' in `quikode unblock`."""
    store = Store(tmp_path / "q.db")
    store.upsert_pending("R-001")
    store.begin_pre_pr_audit_cycle("R-001", 1)
    store.update_pre_pr_audit_stage(
        "R-001", cycle=1, stage_name="standards", passed=False, summary="3 high-severity"
    )
    store.transition("R-001", State.BLOCKED, last_error="audit cycles exhausted")
    summary = store.get_pre_pr_audit_summary("R-001")
    assert summary is not None
    assert any(s["name"] == "standards" and s["passed"] is False for s in summary["stages"])
