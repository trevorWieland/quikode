"""Subtask-id → (planning_cycle, planning_kind) parsing.

Extracted from `state_schema.py` per Plan 60 fix 3 so the schema module
stays under the 600-line architecture budget.

Heuristic only — used for backfill of pre-plan-52 rows and for any read
path that doesn't have the persisted `planning_cycle` column handy. New
emissions populate the cycle/kind columns explicitly at the planner call
site so the CLI's cycle-targeting stays exact and doesn't depend on
re-parsing ids.

Recognised shapes:

* `S-NN-*` / `Z-99-*` / `R-N-*` → `(1, "initial")`
* `F-c<CYCLE>-...`              → `(max(cycle, 2), "fixup")`   ← plan 60
* `F-<N>-...` (numeric)         → `(max(N + 1, 2), "fixup")`
* `F-CI-*`                      → `(2, "fixup_ci")` (single-id fallback;
                                   the migration's two-pass path uses
                                   `infer_planning_provenance_with_context`
                                   to compute MAX(non-F-CI) + 1)
* anything else                 → `(1, "initial")`
"""

from __future__ import annotations

import re

# Plan 60 fix 3: NEW fixup subtask ids carry the cycle-of-origin
# explicitly as `F-c<CYCLE>-...`. The cycle is authoritative — no `+1`
# offset needed because the prefix is stamped against the actual
# emission cycle. Both `fixup` and `fixup_ci` planners share the
# prefix; we read the planning_kind from the persisted column rather
# than re-deriving it from the id, but the heuristic falls back to
# `fixup` for backfill ergonomics.
_FIXUP_CYCLE_PREFIXED_RE = re.compile(r"^F-c(\d+)-")
_FIXUP_NUMERIC_RE = re.compile(r"^F-(\d+)-")
_FIXUP_CI_RE = re.compile(r"^F-CI-")
_INITIAL_PREFIX_RE = re.compile(r"^(S-\d+|Z-99|R-\d+)-?")


def infer_planning_provenance(subtask_id: str) -> tuple[int, str]:
    """Map a subtask id to (planning_cycle, planning_kind)."""
    return _resolve_fixup_provenance(subtask_id) or (1, "initial")


def infer_planning_provenance_with_context(subtask_id: str, *, max_non_fci_cycle: int) -> tuple[int, str]:
    """Plan 53 two-pass migration helper. Identical to
    `infer_planning_provenance` for non-F-CI rows; F-CI rows return
    `(max(max_non_fci_cycle, 1) + 1, "fixup_ci")` so they sit one cycle
    past the highest non-F-CI cycle the task has."""
    if _FIXUP_CI_RE.match(subtask_id):
        cycle = max(max_non_fci_cycle, 1) + 1
        return (cycle, "fixup_ci")
    return infer_planning_provenance(subtask_id)


def is_fixup_ci_id(subtask_id: str) -> bool:
    """True when the id is the pre-plan-60 `F-CI-*` shape. New
    emissions use the `F-c<CYCLE>-` prefix and disambiguate `fixup` vs
    `fixup_ci` via the persisted `planning_kind` column rather than the
    id."""
    return bool(_FIXUP_CI_RE.match(subtask_id))


def _resolve_fixup_provenance(subtask_id: str) -> tuple[int, str] | None:
    """Shared lookup for the fixup-id shapes. Returns None for
    initial-cycle prefixes so the caller can apply the (1, "initial")
    default in one place. Splitting the cycle-prefixed / numeric / F-CI
    / initial branches into one helper keeps the public entry points
    under the project's branch-count lint cap."""
    if _INITIAL_PREFIX_RE.match(subtask_id):
        return None
    cm = _FIXUP_CYCLE_PREFIXED_RE.match(subtask_id)
    if cm:
        return _safe_fixup_cycle_tuple(cm.group(1), kind="fixup", offset=0)
    m = _FIXUP_NUMERIC_RE.match(subtask_id)
    if m:
        return _safe_fixup_cycle_tuple(m.group(1), kind="fixup", offset=1)
    if _FIXUP_CI_RE.match(subtask_id):
        return (2, "fixup_ci")
    return None


def _safe_fixup_cycle_tuple(raw_cycle: str, *, kind: str, offset: int) -> tuple[int, str] | None:
    """Parse a numeric cycle group from a fixup-id regex match. Returns
    None on ValueError so the caller can fall through to the initial
    default; otherwise returns `(max(cycle + offset, 2), kind)`."""
    try:
        cycle = int(raw_cycle) + offset
    except ValueError:
        return None
    return (max(cycle, 2), kind)


__all__ = [
    "infer_planning_provenance",
    "infer_planning_provenance_with_context",
    "is_fixup_ci_id",
]
