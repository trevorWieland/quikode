from __future__ import annotations

import pytest

from quikode import fsm


def test_every_declared_transition_targets_expected_state():
    for (source, event), target in fsm.TRANSITIONS.items():
        assert fsm.target_for_event(source, event) is target


def test_invalid_transition_fails():
    with pytest.raises(fsm.InvalidTransition):
        fsm.target_for_event(fsm.State.PENDING, fsm.Event.MERGED)


def test_terminal_states_are_not_active():
    assert not (fsm.TERMINAL_STATES & fsm.ACTIVE_STATES)
    assert {
        fsm.State.MERGED,
        fsm.State.BLOCKED,
        fsm.State.FAILED,
        fsm.State.ABORTED,
    } == fsm.TERMINAL_STATES


def test_recovery_policy_covers_active_states():
    for state in fsm.ACTIVE_STATES:
        target, fields = fsm.recover_after_crash(state, has_pr=False)
        assert isinstance(target, fsm.State)
        assert isinstance(fields, dict)


def test_pr_aware_recovery_returns_pending_ci():
    assert fsm.recover_after_crash(fsm.State.ADDRESSING_FEEDBACK, has_pr=True)[0] is fsm.State.PENDING_CI


def test_mermaid_contains_states_and_events():
    diagram = fsm.mermaid()
    assert "stateDiagram-v2" in diagram
    for state in fsm.State:
        assert state.value in diagram
    for event in fsm.Event:
        if any(edge_event is event for _, edge_event in fsm.TRANSITIONS):
            assert event.value in diagram
