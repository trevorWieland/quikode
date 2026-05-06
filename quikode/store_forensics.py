from __future__ import annotations

import json
import time
from typing import Any, cast

from quikode.state_types import ProgressCheckRow


class StoreForensicsMixin:
    def capture_block_forensics(self: Any, task_id: str) -> dict:
        """Build + persist a comprehensive BLOCKED-forensics snapshot.

        Designed for the operator's "what should the system have done
        differently?" question — not just "what failed." Captures:

          - retry-reason histogram across all subtasks
          - last 5 distinct checker outputs (deduped on first 80 chars)
          - last 5 triage notes
          - last 3 progress-check verdicts
          - peak container RSS observed
          - last 20 state-log transitions
          - subtasks state distribution

        Returns the snapshot dict (caller can also read via
        `get_block_forensics`).
        """
        snapshot: dict = {"task_id": task_id, "captured_at": time.time()}

        # retry_reasons aggregate
        with self._tx_lock:
            sub_rows = self.conn.execute(
                "SELECT subtask_id, retries, transient_retries, flatline_count, "
                "pre_commit_failures, retry_reasons FROM subtasks WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        retry_summary: dict[str, int] = {}
        per_subtask_retries: list[dict] = []
        for sr in sub_rows:
            d = dict(sr)
            try:
                rr = json.loads(d.get("retry_reasons") or "[]")
            except (json.JSONDecodeError, TypeError):
                rr = []
            cats = {}
            for entry in rr:
                cat = entry.get("category", "other")
                cats[cat] = cats.get(cat, 0) + 1
                retry_summary[cat] = retry_summary.get(cat, 0) + 1
            per_subtask_retries.append(
                {
                    "subtask_id": d.get("subtask_id"),
                    "retries": d.get("retries") or 0,
                    "transient_retries": d.get("transient_retries") or 0,
                    "flatline_count": d.get("flatline_count") or 0,
                    "pre_commit_failures": d.get("pre_commit_failures") or 0,
                    "retry_categories": cats,
                    "recent_retry_examples": rr[-3:],
                }
            )
        snapshot["retry_categories_total"] = retry_summary
        snapshot["per_subtask"] = per_subtask_retries

        # Last 5 distinct checker outputs
        with self._tx_lock:
            arts = self.conn.execute(
                "SELECT kind, content FROM artifacts WHERE task_id = ? "
                "AND kind LIKE 'subtask_checker:%' ORDER BY id DESC LIMIT 20",
                (task_id,),
            ).fetchall()
        seen_starts: set[str] = set()
        last_checker_outputs: list[dict] = []
        for art in arts:
            content = (art["content"] or "")[:1500]
            head = content[:80]
            if head in seen_starts:
                continue
            seen_starts.add(head)
            last_checker_outputs.append({"kind": art["kind"], "excerpt": content})
            if len(last_checker_outputs) >= 5:
                break
        snapshot["last_checker_outputs"] = last_checker_outputs

        # Last 5 triage notes
        with self._tx_lock:
            tarts = self.conn.execute(
                "SELECT kind, content FROM artifacts WHERE task_id = ? "
                "AND kind LIKE 'subtask_triage:%' ORDER BY id DESC LIMIT 5",
                (task_id,),
            ).fetchall()
        snapshot["last_triage_notes"] = [
            {"kind": t["kind"], "excerpt": (t["content"] or "")[:1500]} for t in tarts
        ]

        # Last 3 progress-check verdicts
        with self._tx_lock:
            pc = self.conn.execute(
                "SELECT subtask_id, verdict, rationale, ts FROM progress_checks "
                "WHERE task_id = ? ORDER BY ts DESC LIMIT 3",
                (task_id,),
            ).fetchall()
        snapshot["last_progress_checks"] = [
            {
                "subtask_id": p["subtask_id"],
                "verdict": p["verdict"],
                "rationale": (p["rationale"] or "")[:500],
                "ts": p["ts"],
            }
            for p in pc
        ]

        # Peak RSS
        with self._tx_lock:
            rss = self.conn.execute(
                "SELECT MAX(mem_bytes) FROM container_stats WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        snapshot["peak_mem_bytes"] = int(rss[0]) if rss and rss[0] else None

        # Last 20 state transitions
        with self._tx_lock:
            sl = self.conn.execute(
                "SELECT from_state, to_state, ts, note FROM state_log "
                "WHERE task_id = ? ORDER BY ts DESC LIMIT 20",
                (task_id,),
            ).fetchall()
        snapshot["recent_state_log"] = [
            {
                "from_state": s["from_state"],
                "to_state": s["to_state"],
                "ts": s["ts"],
                "note": (s["note"] or "")[:200],
            }
            for s in sl
        ]

        # Subtask state distribution
        with self._tx_lock:
            sd = self.conn.execute(
                "SELECT state, COUNT(*) AS n FROM subtasks WHERE task_id = ? GROUP BY state",
                (task_id,),
            ).fetchall()
        snapshot["subtask_states"] = {row["state"]: int(row["n"]) for row in sd}

        self.set_block_forensics(task_id, snapshot)
        return snapshot

    def get_last_observed_branch_tip_sha(self: Any, task_id: str) -> str | None:
        """Read the cascade-on-push tracker: the most recent remote-branch tip
        sha we observed for this task. None when never seen / column absent."""
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT last_observed_branch_tip_sha FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if r is None:
            return None
        v = r["last_observed_branch_tip_sha"]
        return str(v) if v else None

    def set_last_observed_branch_tip_sha(self: Any, task_id: str, sha: str) -> None:
        """Stamp the most-recent remote-branch tip sha for cascade detection."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET last_observed_branch_tip_sha = ?, updated_at = ? WHERE id = ?",
                (sha, time.time(), task_id),
            )

    def set_parent_merge_base(self: Any, task_id: str, *, branch: str | None, sha: str | None) -> None:
        """Record the synthetic merge-base branch + sha used for multi-parent
        stacking. Either argument may be None to clear."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET parent_merge_base_branch = ?, parent_merge_base_sha = ?, "
                "updated_at = ? WHERE id = ?",
                (branch, sha, time.time(), task_id),
            )

    def append_retry_reason(
        self: Any,
        task_id: str,
        subtask_id: str,
        *,
        attempt: int,
        category: str,
        signature: str,
        transient: bool = False,
    ) -> None:
        """Record one retry's cause + fingerprint on the subtask row.

        `retry_reasons` is a JSON array of objects:
          [{"attempt": 3, "ts": 1777938xxx.xx,
            "category": "checker_fail", "signature": "verdict=FAIL",
            "transient": false}, ...]

        Bounded at the latest 50 entries — pathological retry storms
        (R-0019 F-1-1 saw 25+ in one stretch) shouldn't blow the column up
        unboundedly. The histogram in `quikode show` only needs counts; the
        signatures are kept for the most-recent entries to surface examples.
        """
        with self.tx() as c:
            r = c.execute(
                "SELECT retry_reasons FROM subtasks WHERE task_id = ? AND subtask_id = ?",
                (task_id, subtask_id),
            ).fetchone()
            try:
                existing = json.loads(r["retry_reasons"]) if r and r["retry_reasons"] else []
            except (json.JSONDecodeError, TypeError):
                existing = []
            if not isinstance(existing, list):
                existing = []
            existing.append(
                {
                    "attempt": int(attempt),
                    "ts": time.time(),
                    "category": str(category),
                    "signature": str(signature)[:200],
                    "transient": bool(transient),
                }
            )
            # Keep tail; counts are preserved by retry_reason_histogram.
            if len(existing) > 50:
                existing = existing[-50:]
            c.execute(
                "UPDATE subtasks SET retry_reasons = ?, updated_at = ? WHERE task_id = ? AND subtask_id = ?",
                (json.dumps(existing), time.time(), task_id, subtask_id),
            )

    def retry_reasons(self: Any, task_id: str, subtask_id: str) -> list[dict]:
        """Read back the retry_reasons JSON array. Empty list when missing/malformed."""
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT retry_reasons FROM subtasks WHERE task_id = ? AND subtask_id = ?",
                (task_id, subtask_id),
            ).fetchone()
        if r is None or r["retry_reasons"] is None:
            return []
        try:
            data = json.loads(r["retry_reasons"])
        except (json.JSONDecodeError, TypeError):
            return []
        return (
            [cast(dict[str, Any], item) for item in data if isinstance(item, dict)]
            if isinstance(data, list)
            else []
        )

    def increment_subtask_pre_commit_failures(self: Any, task_id: str, subtask_id: str) -> int:
        """Bump the pre-commit-failure counter for a subtask. Distinct from
        `retries` so the operator can tell hook-gate rejections apart from
        real verdict-FAILs in the briefing."""
        with self.tx() as c:
            c.execute(
                "UPDATE subtasks SET pre_commit_failures = COALESCE(pre_commit_failures, 0) + 1, "
                "updated_at = ? WHERE task_id = ? AND subtask_id = ?",
                (time.time(), task_id, subtask_id),
            )
            r = c.execute(
                "SELECT pre_commit_failures FROM subtasks WHERE task_id = ? AND subtask_id = ?",
                (task_id, subtask_id),
            ).fetchone()
            return int(r["pre_commit_failures"]) if r else 0

    def increment_subtask_flatline_count(self: Any, task_id: str, subtask_id: str) -> int:
        """Bump consecutive-flatline counter. Reset to 0 by
        `reset_subtask_flatline_count` on any non-flatline progress verdict."""
        with self.tx() as c:
            c.execute(
                "UPDATE subtasks SET flatline_count = COALESCE(flatline_count, 0) + 1, "
                "progress_check_count = COALESCE(progress_check_count, 0) + 1, "
                "updated_at = ? WHERE task_id = ? AND subtask_id = ?",
                (time.time(), task_id, subtask_id),
            )
            r = c.execute(
                "SELECT flatline_count FROM subtasks WHERE task_id = ? AND subtask_id = ?",
                (task_id, subtask_id),
            ).fetchone()
            return int(r["flatline_count"]) if r else 0

    def reset_subtask_flatline_count(self: Any, task_id: str, subtask_id: str) -> None:
        """Zero out the consecutive-flatline counter (still bumps total
        progress_check_count so the operator can see how often the agent
        ran)."""
        with self.tx() as c:
            c.execute(
                "UPDATE subtasks SET flatline_count = 0, "
                "progress_check_count = COALESCE(progress_check_count, 0) + 1, "
                "updated_at = ? WHERE task_id = ? AND subtask_id = ?",
                (time.time(), task_id, subtask_id),
            )

    def increment_subtask_transient_retries(self: Any, task_id: str, subtask_id: str) -> int:
        """Bump the transient-retry counter for a subtask. Used for
        container/network/push-network glitches that don't burn the real
        retry budget."""
        with self.tx() as c:
            c.execute(
                "UPDATE subtasks SET transient_retries = COALESCE(transient_retries, 0) + 1, "
                "updated_at = ? WHERE task_id = ? AND subtask_id = ?",
                (time.time(), task_id, subtask_id),
            )
            r = c.execute(
                "SELECT transient_retries FROM subtasks WHERE task_id = ? AND subtask_id = ?",
                (task_id, subtask_id),
            ).fetchone()
            return int(r["transient_retries"]) if r else 0

    def record_progress_check(
        self: Any,
        task_id: str,
        subtask_id: str,
        *,
        attempts_at_check: int,
        verdict: str,
        rationale: str | None,
    ) -> None:
        """Audit row for one progress-check agent invocation.

        Inserted every time the v3 progress-check agent fires (or fails to
        fire — `uncertain` rows from agent-transient errors land here too).
        Used by `quikode show` / TUI to show why a subtask was eventually
        blocked on flatline grounds, and by tests to verify cadence.
        """
        with self.tx() as c:
            c.execute(
                "INSERT INTO progress_checks "
                "(task_id, subtask_id, ts, attempts_at_check, verdict, rationale) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, subtask_id, time.time(), attempts_at_check, verdict, rationale),
            )

    def get_recent_progress_checks(
        self: Any, task_id: str, subtask_id: str, *, limit: int = 10
    ) -> list[ProgressCheckRow]:
        """Return the most-recent progress-check audit rows for a subtask
        (newest first)."""
        with self._tx_lock:
            rows = self.conn.execute(
                "SELECT * FROM progress_checks WHERE task_id = ? AND subtask_id = ? ORDER BY ts DESC LIMIT ?",
                (task_id, subtask_id, limit),
            ).fetchall()
        return [cast(ProgressCheckRow, dict(r)) for r in rows]

    def recent_subtask_checker_outputs(
        self: Any, task_id: str, subtask_id: str, *, limit: int = 5
    ) -> list[str]:
        """Return the last N checker artifact bodies for a given subtask,
        oldest first. Used by the v3 progress-check agent to derive
        per-attempt root-cause history.

        We look at the artifact stream rather than agent_calls because the
        latter doesn't store agent stdout. The artifact `kind` for a subtask
        checker is `subtask_checker:<subtask_id>`.
        """
        with self._tx_lock:
            rows = self.conn.execute(
                "SELECT content FROM artifacts WHERE task_id = ? AND kind = ? ORDER BY ts DESC LIMIT ?",
                (task_id, f"subtask_checker:{subtask_id}", limit),
            ).fetchall()
        # Reverse so caller sees oldest-first (matches "attempt 1 ... attempt N").
        return [str(r["content"] or "") for r in reversed(rows)]

    def add_artifact(self: Any, task_id: str, kind: str, content: str, is_path: bool = False) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO artifacts (task_id, kind, content, is_path, ts) VALUES (?, ?, ?, ?, ?)",
                (task_id, kind, content, 1 if is_path else 0, time.time()),
            )

    def increment(self: Any, task_id: str, field: str) -> int:
        with self.tx() as c:
            c.execute(
                f"UPDATE tasks SET {field} = COALESCE({field}, 0) + 1, updated_at = ? WHERE id = ?",
                (time.time(), task_id),
            )
            r = c.execute(f"SELECT {field} FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return int(r[field]) if r else 0

    def reset_field(self: Any, task_id: str, field: str, value: Any = 0) -> None:
        with self.tx() as c:
            c.execute(
                f"UPDATE tasks SET {field} = ?, updated_at = ? WHERE id = ?", (value, time.time(), task_id)
            )

    def set_field(self: Any, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = [*list(fields.values()), time.time(), task_id]
        with self.tx() as c:
            c.execute(f"UPDATE tasks SET {sets}, updated_at = ? WHERE id = ?", vals)
