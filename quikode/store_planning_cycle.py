"""Plan 52: planning-cycle queries on the subtasks table + pre-PR audit summary.

Lives in its own mixin so `store_subtasks.py` stays under the 600-line
architecture budget. Used by `qk replan-cycle` to target the most-recent
cycle's rows without touching earlier cycles' commits + retry counters.
The pre-PR audit summary methods sit here too because they're the
companion observability surface for the same audit-cycle data model.
"""

from __future__ import annotations

import json
import time
from typing import Any, cast

from quikode.state_types import SubtaskRow


class StorePlanningCycleMixin:
    def latest_planning_cycle(self: Any, task_id: str) -> tuple[int, str | None]:
        """Return (max_planning_cycle, planning_kind_of_max_cycle).

        `planning_kind` is the kind value of any row in the latest cycle
        (all rows in one cycle share one kind by construction). Returns
        (0, None) when the task has no subtasks at all — caller treats
        that as "no cycle to replan".
        """
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT MAX(planning_cycle) AS m FROM subtasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if r is None or r["m"] is None:
            return 0, None
        max_cycle = int(r["m"])
        with self._tx_lock:
            kr = self.conn.execute(
                "SELECT planning_kind FROM subtasks WHERE task_id = ? AND planning_cycle = ? "
                "ORDER BY id LIMIT 1",
                (task_id, max_cycle),
            ).fetchone()
        kind = kr["planning_kind"] if kr else None
        return max_cycle, kind

    def subtasks_in_cycle(self: Any, task_id: str, cycle: int) -> list[SubtaskRow]:
        """Rows belonging to a specific planning cycle, oldest first."""
        with self._tx_lock:
            rows = self.conn.execute(
                "SELECT * FROM subtasks WHERE task_id = ? AND planning_cycle = ? ORDER BY id",
                (task_id, cycle),
            ).fetchall()
        return [cast(SubtaskRow, dict(r)) for r in rows]

    def delete_subtasks_in_cycle(self: Any, task_id: str, cycle: int) -> int:
        """Drop every subtask row for the named cycle.

        Used by `qk replan-cycle` so the worker's natural fixup flow
        re-emits a clean cycle with the same ordinal (next emission
        increments from MAX = cycle - 1 back to cycle).
        """
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM subtasks WHERE task_id = ? AND planning_cycle = ?",
                (task_id, cycle),
            )
            return int(cur.rowcount or 0)

    def get_pre_pr_audit_summary(self: Any, task_id: str) -> dict[str, Any]:
        """Read the most recent pre-PR audit summary for a task.

        Shape:
          {"cycle": int, "stages": [{"name": str, "passed": bool|None,
                                     "summary": str}], "ts": float}
        `passed=None` means the stage hasn't run yet in the current
        cycle (or is currently in flight). The TUI uses that to render
        a "…" indicator distinct from pass/fail.
        """
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT pre_pr_audit_summary FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if r is None or not r["pre_pr_audit_summary"]:
            return cast(dict[str, Any], None)
        try:
            data = json.loads(r["pre_pr_audit_summary"])
        except (json.JSONDecodeError, TypeError):
            return cast(dict[str, Any], None)
        return cast(dict[str, Any], data) if isinstance(data, dict) else cast(dict[str, Any], None)

    def begin_pre_pr_audit_cycle(self: Any, task_id: str, cycle: int) -> None:
        """Reset the audit summary at the top of a new cycle so stale stage
        results from prior cycles don't bleed into the TUI display. The
        five stages are pre-seeded with `passed=None` (in-flight) so the
        operator sees a "queued" indicator before each stage actually runs.
        Plan 35 PR-B grew this from four stages to five — added the
        `architecture` stage between `standards` and `behavior`.
        """
        seeded = {
            "cycle": cycle,
            "ts": time.time(),
            "stages": [
                {"name": "local_ci", "passed": None, "summary": "queued"},
                {"name": "rubric", "passed": None, "summary": "queued"},
                {"name": "standards", "passed": None, "summary": "queued"},
                {"name": "architecture", "passed": None, "summary": "queued"},
                {"name": "behavior", "passed": None, "summary": "queued"},
            ],
        }
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET pre_pr_audit_summary = ?, updated_at = ? WHERE id = ?",
                (json.dumps(seeded), time.time(), task_id),
            )

    def update_pre_pr_audit_stage(
        self: Any,
        task_id: str,
        *,
        cycle: int,
        stage_name: str,
        passed: bool,
        summary: str,
    ) -> None:
        """Update one stage's outcome on the current cycle. Idempotent:
        re-calling with the same stage name overwrites. If the cycle on
        disk doesn't match the caller's cycle, no-op (defensive against
        a worker that re-entered the pipeline before clearing)."""
        existing = self.get_pre_pr_audit_summary(task_id)
        if existing is None or existing.get("cycle") != cycle:
            # Caller forgot to call begin_pre_pr_audit_cycle — seed lazily.
            self.begin_pre_pr_audit_cycle(task_id, cycle)
            existing = self.get_pre_pr_audit_summary(task_id)
            if existing is None:
                return
        stages = list(existing.get("stages") or [])
        replaced = False
        for s in stages:
            if s.get("name") == stage_name:
                s["passed"] = bool(passed)
                s["summary"] = str(summary)[:300]
                replaced = True
                break
        if not replaced:
            stages.append({"name": stage_name, "passed": bool(passed), "summary": str(summary)[:300]})
        existing["stages"] = stages
        existing["ts"] = time.time()
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET pre_pr_audit_summary = ?, updated_at = ? WHERE id = ?",
                (json.dumps(existing), time.time(), task_id),
            )
