"""Plan 52 migration: backfill `planning_cycle` + `planning_kind` on
pre-plan-52 subtask rows via the subtask-id naming heuristic.

Heuristic:
- `S-NN-*` / `Z-99-*` / `R-NN-*` → (1, "initial")
- `F-N-*` (where N is a digit) → (N+1, "fixup")
- `F-CI-*` → MAX(non-F-CI cycle on this task) + 1, kind="fixup_ci"
  (Plan 53 fix to plan 52's hardcoded 2 — F-CI rows come AFTER all
  pre-PR fixup cycles, so the cycle has to depend on the task's
  fixup history, not be a fixed constant).
- anything else → (1, "initial") fallback
"""

from __future__ import annotations

import sqlite3

from quikode.state_schema import (
    SCHEMA,
    _apply_plan52_migration,
    _infer_planning_provenance,
    _infer_planning_provenance_with_context,
)


def test_infer_planning_provenance_known_prefixes():
    assert _infer_planning_provenance("S-01-domain") == (1, "initial")
    assert _infer_planning_provenance("S-12-final") == (1, "initial")
    assert _infer_planning_provenance("Z-99-stabilize-spec-gate") == (1, "initial")
    assert _infer_planning_provenance("R-1-1-replan-foo") == (1, "initial")
    assert _infer_planning_provenance("F-1-1-fix") == (2, "fixup")
    assert _infer_planning_provenance("F-3-7-something") == (4, "fixup")
    # Plan 53: single-id helper retains the legacy fallback for callers
    # without per-task context. The migration's two-pass path uses the
    # MAX+1 helper instead.
    assert _infer_planning_provenance("F-CI-1-build-fix") == (2, "fixup_ci")
    # Unknown shape → safe default.
    assert _infer_planning_provenance("weird-id") == (1, "initial")


def test_infer_planning_provenance_with_context_fci_max_plus_one():
    """Plan 53: `_infer_planning_provenance_with_context` returns
    `(max_non_fci_cycle + 1, "fixup_ci")` for F-CI-* rows so the
    migration places F-CI cycles AFTER any pre-PR fixup cycles.
    Non-F-CI rows are unchanged from the single-id helper."""
    # F-CI on a task that had cycles 1..6 of non-F-CI rows → 7.
    assert _infer_planning_provenance_with_context("F-CI-1-fix", max_non_fci_cycle=6) == (7, "fixup_ci")
    # F-CI on a task with only initial cycle 1 → 2 (matches legacy).
    assert _infer_planning_provenance_with_context("F-CI-1-fix", max_non_fci_cycle=1) == (2, "fixup_ci")
    # F-CI on a task whose state suggests no non-F-CI rows yet → still 2
    # (we floor max_non_fci_cycle at 1 to preserve the pre-plan-53
    # fallback).
    assert _infer_planning_provenance_with_context("F-CI-1-fix", max_non_fci_cycle=0) == (2, "fixup_ci")
    # Non-F-CI rows fall through to the single-id helper.
    assert _infer_planning_provenance_with_context("F-3-7-something", max_non_fci_cycle=99) == (4, "fixup")


def test_apply_migration_backfills_via_heuristic(tmp_path):
    """Build a DB at the pre-plan-52 schema (no planning_cycle column),
    insert rows whose ids span the heuristic's branches, then run the
    plan-52 migration and assert each row carries the expected cycle/kind.
    """
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # Build a stripped schema WITHOUT plan-52 columns to simulate an
    # existing DB that needs the migration.
    legacy_schema = (
        SCHEMA.replace("    planning_cycle INTEGER NOT NULL DEFAULT 1,\n", "")
        .replace("    planning_kind TEXT NOT NULL DEFAULT 'initial',\n", "")
        .replace("    replan_cycle_marker TEXT,\n", "")
    )
    conn.executescript(legacy_schema)
    # Seed a task + subtasks of every relevant id shape.
    conn.execute(
        "INSERT INTO tasks (id, state, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("R-001", "blocked", 0.0, 0.0),
    )
    seed_ids = [
        "S-01-domain",
        "Z-99-stabilize-spec-gate",
        "F-1-1-rubric-fix",
        "F-2-3-standards-fix",
        "F-CI-1-build-fix",
        "weird-anomaly",
    ]
    for sid in seed_ids:
        conn.execute(
            "INSERT INTO subtasks "
            "(task_id, subtask_id, title, depends_on, files_to_touch, boundary, "
            " acceptance, notes, kind, state, retries, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            ("R-001", sid, sid, "[]", "[]", "", "[]", "", "spec", "pending", 0.0, 0.0),
        )
    # Run the plan-52 migration directly (bypasses the rest of
    # apply_migrations so the test is laser-focused).
    _apply_plan52_migration(conn)
    rows = conn.execute(
        "SELECT subtask_id, planning_cycle, planning_kind FROM subtasks ORDER BY id"
    ).fetchall()
    by_id = {r["subtask_id"]: r for r in rows}
    assert by_id["S-01-domain"]["planning_cycle"] == 1
    assert by_id["S-01-domain"]["planning_kind"] == "initial"
    assert by_id["Z-99-stabilize-spec-gate"]["planning_cycle"] == 1
    assert by_id["Z-99-stabilize-spec-gate"]["planning_kind"] == "initial"
    assert by_id["F-1-1-rubric-fix"]["planning_cycle"] == 2
    assert by_id["F-1-1-rubric-fix"]["planning_kind"] == "fixup"
    assert by_id["F-2-3-standards-fix"]["planning_cycle"] == 3
    assert by_id["F-2-3-standards-fix"]["planning_kind"] == "fixup"
    # Plan 53: F-CI rows take MAX(non-F-CI cycle on this task) + 1.
    # The seeded task has F-2-3 at cycle 3 → F-CI lands at cycle 4.
    assert by_id["F-CI-1-build-fix"]["planning_cycle"] == 4
    assert by_id["F-CI-1-build-fix"]["planning_kind"] == "fixup_ci"
    # Unknown id falls back to defaults.
    assert by_id["weird-anomaly"]["planning_cycle"] == 1
    assert by_id["weird-anomaly"]["planning_kind"] == "initial"
    # Also: the tasks table got the replan_cycle_marker column.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    assert "replan_cycle_marker" in cols
    conn.close()


def test_fci_backfill_uses_max_non_fci_cycle_plus_one(tmp_path):
    """Plan 53: F-CI-* rows backfill to MAX(non-F-CI cycle) + 1, not the
    hardcoded 2 plan 52 originally used. Construct a task with five F-N
    rounds (cycles 2..6) and confirm an F-CI row on the same task lands
    at cycle 7."""
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    legacy_schema = (
        SCHEMA.replace("    planning_cycle INTEGER NOT NULL DEFAULT 1,\n", "")
        .replace("    planning_kind TEXT NOT NULL DEFAULT 'initial',\n", "")
        .replace("    replan_cycle_marker TEXT,\n", "")
    )
    conn.executescript(legacy_schema)
    conn.execute(
        "INSERT INTO tasks (id, state, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("R-007", "blocked", 0.0, 0.0),
    )
    seed_ids = [
        "S-01-domain",
        "F-1-1-rubric-fix",
        "F-2-1-standards-fix",
        "F-3-1-architecture-fix",
        "F-4-1-rubric-fix-2",
        "F-5-1-standards-fix-2",
        "F-CI-1-build-fix",
    ]
    for sid in seed_ids:
        conn.execute(
            "INSERT INTO subtasks "
            "(task_id, subtask_id, title, depends_on, files_to_touch, boundary, "
            " acceptance, notes, kind, state, retries, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            ("R-007", sid, sid, "[]", "[]", "", "[]", "", "spec", "pending", 0.0, 0.0),
        )
    _apply_plan52_migration(conn)
    rows = conn.execute(
        "SELECT subtask_id, planning_cycle, planning_kind FROM subtasks ORDER BY id"
    ).fetchall()
    by_id = {r["subtask_id"]: r for r in rows}
    # F-5-1 is the highest non-F-CI cycle → 6.
    assert by_id["F-5-1-standards-fix-2"]["planning_cycle"] == 6
    # F-CI lands at MAX(6) + 1 = 7.
    assert by_id["F-CI-1-build-fix"]["planning_cycle"] == 7
    assert by_id["F-CI-1-build-fix"]["planning_kind"] == "fixup_ci"
    conn.close()


def test_apply_migration_is_idempotent(tmp_path):
    """Re-running the migration on an already-migrated DB is a no-op."""
    db_path = tmp_path / "migrated.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO tasks (id, state, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("R-001", "blocked", 0.0, 0.0),
    )
    conn.execute(
        "INSERT INTO subtasks "
        "(task_id, subtask_id, title, depends_on, files_to_touch, boundary, "
        " acceptance, notes, kind, state, retries, "
        " planning_cycle, planning_kind, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
        (
            "R-001",
            "F-1-1-fix",
            "x",
            "[]",
            "[]",
            "",
            "[]",
            "",
            "spec",
            "pending",
            7,  # explicitly set to a non-default value
            "fixup",
            0.0,
            0.0,
        ),
    )
    _apply_plan52_migration(conn)
    _apply_plan52_migration(conn)
    r = conn.execute(
        "SELECT planning_cycle, planning_kind FROM subtasks WHERE subtask_id = ?",
        ("F-1-1-fix",),
    ).fetchone()
    # Migration must NOT clobber an existing non-default value (re-runs
    # only touch rows still at the (1, "initial") default).
    assert r["planning_cycle"] == 7
    assert r["planning_kind"] == "fixup"
    conn.close()
