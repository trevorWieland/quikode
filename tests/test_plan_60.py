"""Plan 60 fixes — five bug repairs landing in one commit:

* Fix 1 — fixup_ci subtasks route their objective gate through
  `cfg.local_ci_command` (was `subtask_check_command`).
* Fix 2 — Claude tier models declare quota fallback chains; the chain
  walker triggers on provider-unavailable signatures alongside the
  pre-existing quota signals.
* Fix 3 — fixup planner emits subtask ids carrying the cycle-of-origin
  as an `F-c<CYCLE>-` prefix.
* Fix 4 — TUI subtasks DataTable lives inside a `VerticalScroll` so
  rows beyond the initial viewport remain reachable.
* Fix 5 — `force_recover_to_pending_ci` clears `last_error` +
  `failure_reason` so post-recovery rows don't surface stale failures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from textual.containers import VerticalScroll
from textual.widgets import DataTable

from quikode import fsm_runtime
from quikode.agents.json_fallback import QuotaFallbackJsonAgent
from quikode.agents.json_protocol_types import JsonAgentTransport, RawTransportResult
from quikode.agents.transient_quota import (
    _is_provider_unavailable,
    _is_quota_exhausted,
)
from quikode.config import Config
from quikode.model_registry import get_model
from quikode.state import State
from quikode.subtask_schema import STABILIZATION_SUBTASK_ID, FixupPlan, Subtask
from quikode.tui.app import QuikodeTUI
from quikode.tui.widgets.detail_panel import (
    DetailPanel,
    DetailSnapshot,
    SubtaskRowSnapshot,
)
from quikode.worker import TaskWorker
from quikode.workers.pre_pr import _stamp_planning_cycle_prefix

# ---------- Fix 1: fixup_ci gate promotion ----------


def _stub_subtask(*, kind: str = "spec", subtask_id: str = "S-01") -> Subtask:
    return Subtask(
        id=subtask_id,
        title="x",
        depends_on=(),
        files_to_touch=("foo.rs",),
        boundary="",
        acceptance=("compiles",),
        notes="",
        kind=kind,
    )


def _z99_subtask() -> Subtask:
    return Subtask(
        id=STABILIZATION_SUBTASK_ID,
        title="Stabilize spec gate",
        depends_on=("S-01",),
        files_to_touch=(),
        boundary="",
        acceptance=("spec gate passes",),
        notes="",
    )


def _make_worker(cfg: Config) -> Any:
    w = TaskWorker.__new__(TaskWorker)
    w.cfg = cfg
    w.node = MagicMock()
    w.node.id = "R-0001"
    w.handle = MagicMock()
    w.handle.container_name = "qk-stub"
    w.log_path = None
    w.store = MagicMock()
    return w


def test_fixup_ci_subtask_uses_local_ci_command(tmp_path) -> None:
    """Plan 60 fix 1: kind='fixup-ci' subtasks now run the full
    `local_ci_command` for the objective gate (was the lightweight
    `subtask_check_command`). The doer's local diagnosis stays in sync
    with what GitHub CI actually runs."""
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        subtask_check_command="just check",
        subtask_check_timeout_s=17,
        local_ci_command="just ci",
        local_ci_timeout_s=4321,
    )
    w = _make_worker(cfg)
    subtask = _stub_subtask(kind="fixup-ci", subtask_id="F-c8-2-3-rust-bdd-fix")
    with patch("quikode.worker.exec_in", return_value=(0, "all checks pass\n", "")) as exec_in:
        out = w._run_subtask_check_command(subtask)
    assert out is None
    args, kwargs = exec_in.call_args
    assert args[1] == ["bash", "-lc", "cd /workspace && just ci"]
    assert kwargs["timeout"] == 4321


def test_fixup_ci_subtask_kind_underscore_form_also_routes_to_local_ci(tmp_path) -> None:
    """Plan 60 fix 1: both kind-label forms (the `fixup_ci`
    planning-cycle enum + the runtime `fixup-ci` Subtask value) route
    through the same `_is_fixup_ci_subtask` helper."""
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        subtask_check_command="just check",
        local_ci_command="just ci",
        local_ci_timeout_s=999,
    )
    w = _make_worker(cfg)
    subtask = _stub_subtask(kind="fixup_ci", subtask_id="F-c5-1-1-cleanup")
    with patch("quikode.worker.exec_in", return_value=(0, "", "")) as exec_in:
        out = w._run_subtask_check_command(subtask)
    assert out is None
    args, kwargs = exec_in.call_args
    assert args[1] == ["bash", "-lc", "cd /workspace && just ci"]
    assert kwargs["timeout"] == 999


def test_regular_fixup_subtask_still_uses_subtask_check_command(tmp_path) -> None:
    """Plan 60 fix 1 must NOT promote regular `fixup` / `fixup-final` /
    `fixup-review` subtasks — only the CI-specific variants. Their
    gate stays on the lightweight `subtask_check_command`."""
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        subtask_check_command="just check",
        subtask_check_timeout_s=17,
        local_ci_command="just ci",
        local_ci_timeout_s=999,
    )
    w = _make_worker(cfg)
    subtask = _stub_subtask(kind="fixup", subtask_id="F-c2-1-1-rubric")
    with patch("quikode.worker.exec_in", return_value=(0, "", "")) as exec_in:
        out = w._run_subtask_check_command(subtask)
    assert out is None
    args, kwargs = exec_in.call_args
    assert args[1] == ["bash", "-lc", "cd /workspace && just check"]
    assert kwargs["timeout"] == 17


def test_z99_stabilization_subtask_still_uses_local_ci_command(tmp_path) -> None:
    """Plan 60 fix 1 preserves the Z-99 behavior (was the only path
    using `local_ci_command` before this plan)."""
    cfg = Config(
        repo_path=tmp_path,
        dag_path=tmp_path / "dag.json",
        subtask_check_command="just check",
        local_ci_command="just ci",
        local_ci_timeout_s=2222,
    )
    w = _make_worker(cfg)
    with patch("quikode.worker.exec_in", return_value=(0, "", "")) as exec_in:
        out = w._run_subtask_check_command(_z99_subtask())
    assert out is None
    args, kwargs = exec_in.call_args
    assert args[1] == ["bash", "-lc", "cd /workspace && just ci"]
    assert kwargs["timeout"] == 2222


# ---------- Fix 2: Claude fallback chains + provider-unavailable ----------


def test_claude_opus_has_sonnet_then_gpt55_fallbacks() -> None:
    """Plan 60 fix 2: claude-opus-4-7 falls back to claude-sonnet-4-6
    first (same tier, handles provider-capacity outages), then gpt-5.5
    as the cross-provider floor."""
    spec = get_model("claude-opus-4-7")
    assert spec.quota_fallbacks == ("claude-sonnet-4-6", "gpt-5.5")


def test_claude_sonnet_has_gpt55_then_codex_fallbacks() -> None:
    """Plan 60 fix 2: claude-sonnet-4-6 falls back to gpt-5.5 first
    (strongest non-Claude option for the analytical roles Sonnet
    runs), then gpt-5.3-codex as the writes-files-aware floor."""
    spec = get_model("claude-sonnet-4-6")
    assert spec.quota_fallbacks == ("gpt-5.5", "gpt-5.3-codex")


def test_is_provider_unavailable_matches_claude_auth_signatures() -> None:
    """Plan 60 fix 2: the Claude CLI's auth-failure stderr in the
    2026-05-11 overnight outage was rc=1 + 'Invalid API key' /
    'authentication failed' / 'session expired' / 401/403 patterns —
    none of which `_is_quota_exhausted` matched. The new detector
    treats them as chain-walk triggers."""
    assert _is_provider_unavailable(1, "", "Invalid API key (provided key has been revoked)") is True
    assert _is_provider_unavailable(1, "", "authentication failed: bad creds") is True
    assert _is_provider_unavailable(1, "", "authentication error: expired") is True
    assert _is_provider_unavailable(1, "", "session expired; please /login") is True
    assert _is_provider_unavailable(1, "", "HTTP 401 Unauthorized") is True
    assert _is_provider_unavailable(1, "", "401: Unauthorized request") is True
    assert _is_provider_unavailable(1, "", "HTTP 403 Forbidden") is True
    assert _is_provider_unavailable(1, "", "Please run /login to re-authenticate") is True
    # rc=0 always returns False (success can't be a chain trigger).
    assert _is_provider_unavailable(0, "", "Invalid API key in docs") is False
    # No match → False.
    assert _is_provider_unavailable(1, "", "some other failure") is False
    # Quota patterns are NOT a provider-unavailable match (they belong
    # to the existing `_is_quota_exhausted` detector).
    assert _is_provider_unavailable(1, "", "HTTP 429 rate limit exceeded") is False


def test_is_quota_exhausted_does_not_double_count_auth_signals() -> None:
    """Plan 60 fix 2 keeps the two detectors disjoint so the
    `_should_walk_chain` OR remains principled (quota vs provider-
    auth are different failure modes even though they share the
    cascade path)."""
    # Quota signature → quota detector True, provider-unavailable False.
    assert _is_quota_exhausted(1, "", "HTTP 429: rate limit exceeded") is True
    assert _is_provider_unavailable(1, "", "HTTP 429: rate limit exceeded") is False
    # Auth signature → provider-unavailable True, quota False.
    assert _is_provider_unavailable(1, "", "Invalid API key") is True
    assert _is_quota_exhausted(1, "", "Invalid API key") is False


class _StubTransport(JsonAgentTransport):
    """Minimal `invoke_raw`-only stub for chain-walker tests. The real
    `JsonAgentTransport` has `invoke` + `invoke_raw`; the wrapper's
    `invoke_raw` is the only path exercised here."""

    name = "stub"

    def __init__(self, name: str, schema_enforcement: str, responses: list[RawTransportResult]):
        self.name = name
        self.schema_enforcement = schema_enforcement
        self._responses = list(responses)
        self.invocations: list[str] = []

    def invoke(self, prompt, *, output_schema, handle, log_path, timeout):  # pragma: no cover
        raise NotImplementedError("test uses invoke_raw only")

    def invoke_raw(self, prompt, *, handle, log_path, timeout) -> RawTransportResult:
        self.invocations.append(prompt)
        return self._responses.pop(0)


def test_quota_fallback_chain_walks_on_claude_auth_failure(tmp_path) -> None:
    """Plan 60 fix 2: a rc=1 Claude-shaped auth failure must trigger
    the chain walker. Today's overnight outage emitted exactly this
    signature and the old quota-only check left every call wedged."""
    primary = _StubTransport(
        name="claude-opus",
        schema_enforcement="cli_native",
        responses=[
            RawTransportResult(
                raw_text=None,
                structured=None,
                rc=1,
                transient=False,
                duration_s=3.0,
                stderr_excerpt="Invalid API key · Please run /login",
            )
        ],
    )
    fallback = _StubTransport(
        name="gpt-5.5",
        schema_enforcement="cli_native",
        responses=[
            RawTransportResult(
                raw_text='{"verdict":"pass"}',
                structured=None,
                rc=0,
                transient=False,
                duration_s=2.0,
            )
        ],
    )
    transport = QuotaFallbackJsonAgent(primary=primary, fallbacks=(fallback,))
    log_path = tmp_path / "agent.log"
    result = transport.invoke_raw("hi", handle=object(), log_path=log_path, timeout=60)
    assert result.rc == 0
    assert len(primary.invocations) == 1
    assert len(fallback.invocations) == 1
    assert "quota fallback: claude-opus -> gpt-5.5" in log_path.read_text()


def test_quota_fallback_chain_still_walks_on_quota_exhaustion(tmp_path) -> None:
    """Regression guard: Plan 60 fix 2's broadened trigger must keep
    the existing quota path working."""
    primary = _StubTransport(
        name="claude-opus",
        schema_enforcement="cli_native",
        responses=[
            RawTransportResult(
                raw_text=None,
                structured=None,
                rc=1,
                transient=False,
                duration_s=1.0,
                stderr_excerpt="HTTP 429: rate limit exceeded",
            )
        ],
    )
    fallback = _StubTransport(
        name="claude-sonnet",
        schema_enforcement="cli_native",
        responses=[
            RawTransportResult(
                raw_text='{"ok":true}',
                structured=None,
                rc=0,
                transient=False,
                duration_s=1.0,
            )
        ],
    )
    transport = QuotaFallbackJsonAgent(primary=primary, fallbacks=(fallback,))
    result = transport.invoke_raw("hi", handle=object(), log_path=tmp_path / "agent.log", timeout=60)
    assert result.rc == 0
    assert len(fallback.invocations) == 1


# ---------- Fix 3: planning_cycle prefix on fixup subtask IDs ----------


def test_stamp_planning_cycle_prefix_rewrites_ids_and_deps() -> None:
    """Plan 60 fix 3: `_stamp_planning_cycle_prefix` rewrites every
    `F-N-...` id in the plan to `F-c<CYCLE>-N-...` AND rewrites
    `depends_on` references to siblings so the within-plan dependency
    graph stays consistent. (FixupPlan's runtime validator requires
    depends_on entries to refer to siblings within the same plan, so
    this test only exercises sibling rewrites.)"""
    plan = FixupPlan(
        summary="fix the BDD scenarios",
        findings_addressed=("rubric:r1",),
        subtasks=(
            Subtask(
                id="F-2-3-rust-bdd-fix",
                title="rust BDD",
                depends_on=(),
                files_to_touch=("crates/x/src/lib.rs",),
                boundary="",
                acceptance=("BDD passes",),
                notes="",
                kind="fixup-ci",
            ),
            Subtask(
                id="F-2-4-extend",
                title="extend tests",
                depends_on=("F-2-3-rust-bdd-fix",),
                files_to_touch=("crates/x/tests/bdd.rs",),
                boundary="",
                acceptance=("scenarios added",),
                notes="",
                kind="fixup-ci",
            ),
        ),
    )
    stamped = _stamp_planning_cycle_prefix(plan, cycle=8)
    assert stamped.subtasks[0].id == "F-c8-2-3-rust-bdd-fix"
    assert stamped.subtasks[1].id == "F-c8-2-4-extend"
    # Sibling fixup depends_on rewritten to the new prefixed id.
    assert stamped.subtasks[1].depends_on == ("F-c8-2-3-rust-bdd-fix",)
    # Original plan untouched (immutable copies).
    assert plan.subtasks[0].id == "F-2-3-rust-bdd-fix"


def test_stamp_planning_cycle_prefix_idempotent_on_already_prefixed_ids() -> None:
    """Plan 60 fix 3: an id already carrying a different `F-c<N>-`
    prefix gets rebound to the current cycle so the cycle-of-origin
    invariant holds even on planner-output echoes."""
    plan = FixupPlan(
        summary="x",
        findings_addressed=(),
        subtasks=(
            Subtask(
                id="F-c3-1-1-fix",  # planner echoed an old cycle prefix
                title="x",
                depends_on=(),
                files_to_touch=(),
                boundary="",
                acceptance=("ok",),
                notes="",
                kind="fixup",
            ),
        ),
    )
    stamped = _stamp_planning_cycle_prefix(plan, cycle=5)
    # The "1-1-fix" suffix is preserved; only the cycle changes.
    assert stamped.subtasks[0].id == "F-c5-1-1-fix"


# ---------- Fix 4: TUI subtasks DataTable scroll ----------


def test_subtasks_table_lives_inside_vertical_scroll_container() -> None:
    """Plan 60 fix 4: the DataTable composing the subtasks tab is now
    wrapped in a `VerticalScroll` so its host container handles
    overflow and tasks with 40+ rows remain reachable."""
    src = Path("quikode/tui/widgets/detail_panel.py").read_text()
    # The wrapper id used in the new compose() shape.
    assert 'id="subtasks-scroll"' in src
    # The DataTable still has its own id (used by the per-row render
    # and the test below).
    assert 'id="subtasks-table"' in src
    # CSS sized the wrapper to claim the pane's available vertical
    # space (`#subtasks-scroll { height: 1fr }`) and let the table grow
    # to its natural row content (`#subtasks-table { height: auto }`).
    tcss = Path("quikode/tui/styles/quikode.tcss").read_text()
    assert "#subtasks-scroll" in tcss
    assert "height: auto" in tcss


@pytest.mark.asyncio
async def test_detail_panel_renders_50_subtasks_with_reachable_last_row(tmp_path) -> None:
    """Plan 60 fix 4: a task with 50 subtasks renders all rows into the
    DataTable; with the VerticalScroll wrapper the cursor can be moved
    to the last row (row 49) without the table clipping it. Prior
    behavior was that the table got clipped by the surrounding TabPane
    height and the last ~20 rows were unreachable."""
    app = QuikodeTUI(workspace=tmp_path)
    async with app.run_test() as pilot:
        panel = app.query_one("#detail-panel", DetailPanel)
        rows = [
            SubtaskRowSnapshot(
                subtask_id=f"S-{i:02d}-slice",
                title=f"slice {i}",
                state="pending" if i > 0 else "doing",
                retries=0,
            )
            for i in range(50)
        ]
        snap = DetailSnapshot(
            task_id="R-9999",
            title="big task",
            subtasks=rows,
            active_subtask_idx=0,
            task_state="doing_subtask",
        )
        panel.render_snapshot(snap)
        await pilot.pause()
        table = panel.query_one("#subtasks-table", DataTable)
        assert table.row_count == 50
        # Move the cursor to the last row — with the VerticalScroll
        # wrapper the move succeeds and the cursor coordinate updates.
        table.move_cursor(row=49, column=0, animate=False)
        await pilot.pause()
        assert table.cursor_coordinate.row == 49
        # The wrapping VerticalScroll is present.
        scroll = panel.query_one("#subtasks-scroll", VerticalScroll)
        assert scroll is not None


# ---------- Fix 5: force_recover_to_pending_ci clears stale errors ----------


def test_force_recover_to_pending_ci_clears_last_error_and_failure_reason() -> None:
    """Plan 60 fix 5: the supervisor escape hatch must clear
    `last_error` + `failure_reason` so the post-recovery row isn't
    showing a stale audit-stage error message after the operator/
    orchestrator has explicitly forgiven the stall."""
    store = MagicMock()

    captured: dict[str, Any] = {}

    def fake_transition(task_id: str, state: State, *, note: str | None = None, **fields: Any) -> None:
        captured["task_id"] = task_id
        captured["state"] = state
        captured["note"] = note
        captured["fields"] = fields

    store.transition.side_effect = fake_transition
    with patch.object(fsm_runtime, "current_state", return_value=State.AUDIT_BEHAVIOR):
        result = fsm_runtime.force_recover_to_pending_ci(store, "R-0042", note="stalled 35min")
    assert result is State.PENDING_CI
    assert captured["state"] is State.PENDING_CI
    assert captured["fields"]["last_error"] is None
    assert captured["fields"]["failure_reason"] is None


def test_force_recover_to_pending_ci_respects_explicit_field_overrides() -> None:
    """Plan 60 fix 5: callers can still override `last_error` /
    `failure_reason` explicitly — the cleanup is a `setdefault`, not a
    blanket overwrite."""
    store = MagicMock()
    captured: dict[str, Any] = {}

    def fake_transition(task_id: str, state: State, *, note: str | None = None, **fields: Any) -> None:
        captured["fields"] = fields

    store.transition.side_effect = fake_transition
    with patch.object(fsm_runtime, "current_state", return_value=State.AUDIT_RUBRIC):
        fsm_runtime.force_recover_to_pending_ci(store, "R-0007", note="x", last_error="explicit override")
    assert captured["fields"]["last_error"] == "explicit override"
    # failure_reason wasn't passed by the caller → defaulted to None.
    assert captured["fields"]["failure_reason"] is None
