"""Per-subtask retry-storm stop-loss helpers.

The subtask loop runs three independent stop-loss checks against the
last K non-transient retry signatures stored on the row:

* Plan 53 cannot_reproduce stop-loss (K=2 default) — fires when the
  doer keeps producing empty diffs on `kind="fixup_ci"` work while the
  local objective gate and scoped witnesses are green. The signal is
  environmental drift: GitHub CI is failing on something the local
  container cannot reproduce, and no amount of retry on the same
  recipe will close the gap.

* Plan 51 transport stop-loss (K=3 default) — fires when the doer
  keeps producing empty diffs on non-fixup_ci work. The signal is a
  broken doer transport (model is rc=0 but emitting nothing); the
  right operator action is a model swap.

* Plan 23 same-signature stop-loss (K=5 default) — fires when the last
  N non-transient retries all share the same `(category, signature)`
  tuple. Catches deadlocks where the failure layer + signature stay
  pinned and the progress agent rates them 'progressing'.

Lives in its own module so `subtasks.py` stays under the 600-line
architecture budget. Each helper takes the bare inputs it needs (the
last-K signatures + the relevant cfg knob) so unit tests can exercise
them without the full TaskWorker harness.
"""

from __future__ import annotations


def maybe_cannot_reproduce_stop_loss(*, subtask_id: str, sigs: list[tuple[str, str]], k: int) -> str | None:
    """Plan 53: BLOCK after K consecutive non-transient retries whose
    retry signature carries `,layer=cannot_reproduce`. Returns the
    block message naming environmental drift explicitly so the
    operator doesn't have to dig through retry history."""
    if k < 2 or len(sigs) < k:
        return None
    for cat, sig in sigs:
        if cat not in ("checker_fail", "doer_output_invalid"):
            return None
        if ",layer=cannot_reproduce" not in sig:
            return None
    return (
        f"cannot_reproduce stop-loss: last {k} non-transient retries on "
        f"{subtask_id} produced empty diffs while the local objective "
        "gate and scoped witnesses were green (layer=cannot_reproduce). "
        "GitHub CI is failing on environmental drift the local "
        "container cannot reproduce — likely cached intermediate "
        "artifacts, pinned-version divergence between local and CI "
        "runner, or a missing checked-in generated file. Operator "
        "action: investigate the environment delta; do not retry "
        "this recipe."
    )


def maybe_transport_stop_loss(*, sigs: list[tuple[str, str]], k: int) -> str | None:
    """Plan 51: BLOCK after K consecutive non-transient retries whose
    retry signature carries `,layer=transport` AND whose category is a
    content-failure category (`checker_fail` or `doer_output_invalid`
    — the two categories the empty-diff synthesized outcome routes
    through `retry_classify`).

    Block message is operator-clear about the recommended action: swap
    `subtask_doer_model` to a known-reliable model and resume.
    """
    if k < 2 or len(sigs) < k:
        return None
    for cat, sig in sigs:
        if cat not in ("checker_fail", "doer_output_invalid"):
            return None
        if ",layer=transport" not in sig:
            return None
    return (
        f"transport stop-loss: last {k} non-transient retries had empty "
        f"diffs (layer=transport). The doer model is not producing work "
        f"product. Check `cfg.subtask_doer_model` and the underlying "
        f"transport (litellm bridge, codex profile, provider quota). "
        f"Operator action: swap subtask_doer_model to a known-reliable "
        f"model and resume."
    )


def maybe_same_signature_stop_loss(*, sigs: list[tuple[str, str]], n: int) -> str | None:
    """Plan 23: BLOCK when the last N non-transient retries all share
    the same `(category, signature)` tuple. Independent of the
    progress-check verdict — catches deadlocks where each attempt
    produces different-but-equally-invalid output that the
    progress-check agent rates 'progressing'."""
    if n < 2 or len(sigs) < n:
        return None
    first = sigs[0]
    if not all(s == first for s in sigs):
        return None
    cat, sig = first
    sig_short = sig[:120]
    return (
        f"same-signature stop-loss: last {n} non-transient retries all "
        f"share category={cat!r} signature={sig_short!r}"
    )


__all__ = [
    "maybe_cannot_reproduce_stop_loss",
    "maybe_same_signature_stop_loss",
    "maybe_transport_stop_loss",
]
