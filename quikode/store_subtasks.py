from __future__ import annotations

import json
import time
from typing import Any, cast

from quikode.state_types import ContainerStatsRow, SubtaskRow, SubtaskState


class StoreSubtaskMixin:
    def upsert_subtasks(self: Any, task_id: str, subtasks: list[dict]) -> None:
        """Replace any existing subtasks for this task with the given list."""
        now = time.time()
        with self.tx() as c:
            c.execute("DELETE FROM subtasks WHERE task_id = ?", (task_id,))
            for s in subtasks:
                c.execute(
                    "INSERT INTO subtasks "
                    "(task_id, subtask_id, title, depends_on, files_to_touch, boundary, "
                    " acceptance, notes, kind, state, retries, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (
                        task_id,
                        s["subtask_id"],
                        s.get("title", ""),
                        json.dumps(s.get("depends_on", [])),
                        json.dumps(s.get("files_to_touch", [])),
                        s.get("boundary", ""),
                        json.dumps(s.get("acceptance", [])),
                        s.get("notes", ""),
                        s.get("kind", "spec"),
                        SubtaskState.PENDING.value,
                        now,
                        now,
                    ),
                )

    def append_subtasks(self: Any, task_id: str, subtasks: list[dict]) -> None:
        """Append new subtasks to the existing set for `task_id` without deleting.

        Used by the v3 fixup-decomposition flow: when final-check or CI fails,
        the fixup planner emits a small Plan of additive slices that need to
        run after the original spec subtasks have already settled DONE.
        Skips rows whose `subtask_id` already exists for the task — the
        planner is responsible for unique IDs (e.g. `F-final-1-line-budget`)
        but we double-guard so a planner repeat doesn't error mid-round.
        """
        now = time.time()
        with self.tx() as c:
            existing = {
                r[0]
                for r in c.execute("SELECT subtask_id FROM subtasks WHERE task_id = ?", (task_id,)).fetchall()
            }
            for s in subtasks:
                if s["subtask_id"] in existing:
                    continue
                c.execute(
                    "INSERT INTO subtasks "
                    "(task_id, subtask_id, title, depends_on, files_to_touch, boundary, "
                    " acceptance, notes, kind, state, retries, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (
                        task_id,
                        s["subtask_id"],
                        s.get("title", ""),
                        json.dumps(s.get("depends_on", [])),
                        json.dumps(s.get("files_to_touch", [])),
                        s.get("boundary", ""),
                        json.dumps(s.get("acceptance", [])),
                        s.get("notes", ""),
                        s.get("kind", "spec"),
                        SubtaskState.PENDING.value,
                        now,
                        now,
                    ),
                )

    def list_subtasks(self: Any, task_id: str) -> list[SubtaskRow]:
        with self._tx_lock:
            rows = self.conn.execute(
                "SELECT * FROM subtasks WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        return [cast(SubtaskRow, dict(r)) for r in rows]

    def get_subtask(self: Any, task_id: str, subtask_id: str) -> SubtaskRow:
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT * FROM subtasks WHERE task_id = ? AND subtask_id = ?",
                (task_id, subtask_id),
            ).fetchone()
        return cast(SubtaskRow, dict(r)) if r else cast(SubtaskRow, None)

    def update_subtask(self: Any, task_id: str, subtask_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = [*list(fields.values()), time.time(), task_id, subtask_id]
        with self.tx() as c:
            c.execute(
                f"UPDATE subtasks SET {sets}, updated_at = ? WHERE task_id = ? AND subtask_id = ?",
                vals,
            )

    def reset_subtask_for_rewind(self: Any, task_id: str, subtask_id: str) -> None:
        """Reset a subtask to a fresh PENDING state for plan-27 rewind.

        Wipes everything that accrued during the subtask's run: retries,
        transient retries, flatline counter, triage notes, last error,
        commit sha, retry-reason history, accepted-files override,
        pre-commit failures, and progress-check counter. Leaves the
        immutable fields (id, title, depends_on, files_to_touch,
        boundary, acceptance, notes, kind, addresses_findings) intact
        so the worker re-runs the same subtask shape on resume.
        """
        with self.tx() as c:
            c.execute(
                "UPDATE subtasks SET state = ?, retries = 0, transient_retries = 0, "
                "flatline_count = 0, triage_notes = NULL, last_error = NULL, "
                "commit_sha = NULL, retry_reasons = NULL, accepted_files = NULL, "
                "pre_commit_failures = 0, progress_check_count = 0, "
                "updated_at = ? "
                "WHERE task_id = ? AND subtask_id = ?",
                ("pending", time.time(), task_id, subtask_id),
            )

    def record_container_stats(
        self: Any,
        task_id: str,
        container_name: str,
        cpu_pct: float | None,
        mem_bytes: int | None,
        mem_pct: float | None,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO container_stats (task_id, container_name, cpu_pct, mem_bytes, mem_pct, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, container_name, cpu_pct, mem_bytes, mem_pct, time.time()),
            )

    def task_total_cost_usd(self: Any, task_id: str) -> float:
        """Sum of `agent_calls.cost_usd` for a task. None when nothing
        has been logged yet (or every row has cost_usd NULL — providers
        that don't report cost). Used by `quikode briefing` to surface
        per-task spend."""
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT SUM(cost_usd) AS s FROM agent_calls WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if r is None:
            return cast(float, None)
        v = r["s"]
        return float(v) if v is not None else cast(float, None)

    def workspace_total_cost_usd(self: Any) -> float:
        """Sum of `agent_calls.cost_usd` across all tasks. None when
        nothing's been logged yet."""
        with self._tx_lock:
            r = self.conn.execute("SELECT SUM(cost_usd) AS s FROM agent_calls").fetchone()
        if r is None:
            return cast(float, None)
        v = r["s"]
        return float(v) if v is not None else cast(float, None)

    def task_max_rss(self: Any, task_id: str) -> int | None:
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT MAX(mem_bytes) AS m FROM container_stats WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return int(r["m"]) if r and r["m"] else None

    def mark_needs_intent_review(self: Any, task_ids: list[str], triggered_by: str) -> None:
        if not task_ids:
            return
        with self.tx() as c:
            for tid in task_ids:
                c.execute(
                    "UPDATE tasks SET needs_intent_review = 1, updated_at = ? WHERE id = ?",
                    (time.time(), tid),
                )

    def clear_intent_review_flag(self: Any, task_id: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET needs_intent_review = 0, last_intent_review_ts = ?, "
                "intent_review_count = COALESCE(intent_review_count, 0) + 1, updated_at = ? "
                "WHERE id = ?",
                (time.time(), time.time(), task_id),
            )

    def record_intent_review(
        self: Any,
        task_id: str,
        *,
        triggered_by_merge_of: str | None,
        main_sha_before: str | None,
        main_sha_after: str | None,
        verdict: str,
        explanation: str,
        affected_areas: str,
        raw_output: str,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO intent_reviews "
                "(task_id, triggered_by_merge_of, main_sha_before, main_sha_after, "
                " verdict, explanation, affected_areas, raw_output, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    triggered_by_merge_of,
                    main_sha_before,
                    main_sha_after,
                    verdict,
                    explanation,
                    affected_areas,
                    raw_output,
                    time.time(),
                ),
            )

    def latest_container_stats(self: Any, task_id: str) -> ContainerStatsRow | None:
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT * FROM container_stats WHERE task_id = ? ORDER BY ts DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return cast(ContainerStatsRow, dict(r)) if r else None

    def increment_subtask_retries(self: Any, task_id: str, subtask_id: str) -> int:
        with self.tx() as c:
            c.execute(
                "UPDATE subtasks SET retries = COALESCE(retries, 0) + 1, updated_at = ? "
                "WHERE task_id = ? AND subtask_id = ?",
                (time.time(), task_id, subtask_id),
            )
            r = c.execute(
                "SELECT retries FROM subtasks WHERE task_id = ? AND subtask_id = ?",
                (task_id, subtask_id),
            ).fetchone()
            return int(r["retries"]) if r else 0

    def get_parent_task_ids(self: Any, task_id: str) -> list[str]:
        """Read the JSON-array parent_task_ids for a task.

        Always returns a list (possibly empty)."""
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT parent_task_ids FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if r is None or not r["parent_task_ids"]:
            return []
        try:
            arr = json.loads(r["parent_task_ids"])
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(arr, list):
            return []
        return [str(x) for x in arr if x]

    def all_parents_merged(self: Any, task_id: str) -> bool:
        """Plan 31: True iff the task has parents AND all of them are MERGED.

        Returns False when the task has no parents (no parents = no rebase
        target distinction; caller decides). Used by the rebase worker to
        pick between staying-stacked-on-parent (parent_tip target) vs
        reattaching to main (target=main, retarget PR).
        """
        parent_ids = self.get_parent_task_ids(task_id)
        if not parent_ids:
            return False
        placeholders = ",".join("?" * len(parent_ids))
        with self._tx_lock:
            rows = self.conn.execute(
                f"SELECT state FROM tasks WHERE id IN ({placeholders})",
                tuple(parent_ids),
            ).fetchall()
        if len(rows) != len(parent_ids):
            # Some parents missing from the store — be conservative and
            # treat as "not all merged" so the rebase stays stacked. The
            # missing-parent case shouldn't happen on a healthy seeded DAG.
            return False
        return all(r["state"] == "merged" for r in rows)

    def get_parent_branches(self: Any, task_id: str) -> list[str]:
        """Read JSON-array parent_branches. Always returns a list."""
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT parent_branches FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if r is None or not r["parent_branches"]:
            return []
        try:
            arr = json.loads(r["parent_branches"])
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(arr, list):
            return []
        return [str(x) for x in arr if x]

    def set_parent_chain(
        self: Any,
        task_id: str,
        *,
        parent_task_ids: list[str],
        parent_branches: list[str] | None = None,
        parent_pr_branches: list[str] | None = None,
    ) -> None:
        """Stamp the multi-parent linkage on a task. Pass empty lists (or
        None) to clear all parent linkage."""
        ids_json = json.dumps(list(parent_task_ids))
        branches_json = json.dumps(list(parent_branches or []))
        pr_branches_json = json.dumps(list(parent_pr_branches or []))
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET "
                "  parent_task_ids = ?, parent_branches = ?, parent_pr_branches = ?, "
                "  updated_at = ? "
                "WHERE id = ?",
                (ids_json, branches_json, pr_branches_json, time.time(), task_id),
            )

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
        four stages are pre-seeded with `passed=None` (in-flight) so the
        operator sees a "queued" indicator before each stage actually runs.
        """
        seeded = {
            "cycle": cycle,
            "ts": time.time(),
            "stages": [
                {"name": "local_ci", "passed": None, "summary": "queued"},
                {"name": "rubric", "passed": None, "summary": "queued"},
                {"name": "standards", "passed": None, "summary": "queued"},
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

    def get_block_forensics(self: Any, task_id: str) -> dict | None:
        """Read the BLOCKED-forensics JSON dump for a task. None when no
        block has occurred (or the column is empty)."""
        with self._tx_lock:
            r = self.conn.execute("SELECT block_forensics FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if r is None or not r["block_forensics"]:
            return None
        try:
            data = json.loads(r["block_forensics"])
        except (json.JSONDecodeError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def set_block_forensics(self: Any, task_id: str, snapshot: dict) -> None:
        """Persist a forensics snapshot. Caller assembles the dict; this
        just stores it. Snapshot is JSON-serializable; non-serializable
        values are dropped via `default=str`."""
        try:
            blob = json.dumps(snapshot, default=str)[:200000]
        except (TypeError, ValueError):
            blob = json.dumps({"_serialization_error": True})
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET block_forensics = ?, updated_at = ? WHERE id = ?",
                (blob, time.time(), task_id),
            )
