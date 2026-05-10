"""Plan 53: pure-function unit tests for the stop-loss helpers in
`quikode.workers.subtask_stop_loss`. The helpers operate on
`(category, signature)` tuple lists captured from the retry table —
no TaskWorker harness needed to exercise them."""

from __future__ import annotations

from quikode.workers.subtask_stop_loss import (
    maybe_cannot_reproduce_stop_loss,
    maybe_same_signature_stop_loss,
    maybe_transport_stop_loss,
)


def test_cannot_reproduce_fires_at_k_two():
    sigs = [
        ("checker_fail", "verdict=FAIL,layer=cannot_reproduce"),
        ("checker_fail", "verdict=FAIL,layer=cannot_reproduce"),
    ]
    msg = maybe_cannot_reproduce_stop_loss(subtask_id="F-CI-1-fix", sigs=sigs, k=2)
    assert msg is not None
    assert "cannot_reproduce stop-loss" in msg
    assert "environmental drift" in msg
    assert "F-CI-1-fix" in msg


def test_cannot_reproduce_does_not_fire_when_layer_differs():
    """Two attempts with `layer=transport` (not cannot_reproduce) must
    NOT trip the cannot_reproduce stop-loss — the categories are
    distinct discriminators by design."""
    sigs = [
        ("checker_fail", "verdict=FAIL,layer=transport"),
        ("checker_fail", "verdict=FAIL,layer=transport"),
    ]
    assert maybe_cannot_reproduce_stop_loss(subtask_id="S-01", sigs=sigs, k=2) is None


def test_cannot_reproduce_does_not_fire_below_k():
    sigs = [("checker_fail", "verdict=FAIL,layer=cannot_reproduce")]
    assert maybe_cannot_reproduce_stop_loss(subtask_id="S-01", sigs=sigs, k=2) is None


def test_cannot_reproduce_does_not_fire_for_non_content_category():
    sigs = [
        ("network_timeout", ",layer=cannot_reproduce"),
        ("network_timeout", ",layer=cannot_reproduce"),
    ]
    assert maybe_cannot_reproduce_stop_loss(subtask_id="S-01", sigs=sigs, k=2) is None


def test_transport_fires_at_k_three():
    sigs = [
        ("checker_fail", "verdict=FAIL,layer=transport"),
        ("checker_fail", "verdict=FAIL,layer=transport"),
        ("checker_fail", "verdict=FAIL,layer=transport"),
    ]
    msg = maybe_transport_stop_loss(sigs=sigs, k=3)
    assert msg is not None
    assert "transport stop-loss" in msg


def test_same_signature_fires_at_n():
    sigs = [
        ("checker_fail", "verdict=FAIL,layer=local_ci"),
        ("checker_fail", "verdict=FAIL,layer=local_ci"),
        ("checker_fail", "verdict=FAIL,layer=local_ci"),
    ]
    msg = maybe_same_signature_stop_loss(sigs=sigs, n=3)
    assert msg is not None
    assert "same-signature stop-loss" in msg


def test_same_signature_does_not_fire_when_signatures_differ():
    sigs = [
        ("checker_fail", "verdict=FAIL,layer=local_ci"),
        ("checker_fail", "verdict=FAIL,layer=rubric"),
        ("checker_fail", "verdict=FAIL,layer=standards"),
    ]
    assert maybe_same_signature_stop_loss(sigs=sigs, n=3) is None
