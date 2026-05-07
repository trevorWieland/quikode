from __future__ import annotations

import logging
import time
from typing import Any, cast

from quikode.fsm import Event, InvalidTransition, target_for_event
from quikode.state_types import State, TaskRow

log = logging.getLogger("quikode.state")


class StoreTaskMixin:
    def upsert_pending(self: Any, task_id: str) -> None:
        now = time.time()
        with self.tx() as c:
            r = c.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if r is None:
                c.execute(
                    "INSERT INTO tasks (id, state, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (task_id, State.PENDING.value, now, now),
                )
                c.execute(
                    "INSERT INTO state_log (task_id, from_state, to_state, ts) VALUES (?, NULL, ?, ?)",
                    (task_id, State.PENDING.value, now),
                )

    def transition(self: Any, task_id: str, new_state: State, note: str | None = None, **fields: Any) -> None:
        now = time.time()
        with self.tx() as c:
            r = c.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
            from_state = r["state"] if r else None
            sets = ["state = ?", "updated_at = ?"]
            vals: list[Any] = [new_state.value, now]
            for k, v in fields.items():
                sets.append(f"{k} = ?")
                vals.append(v)
            vals.append(task_id)
            c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals)
            c.execute(
                "INSERT INTO state_log (task_id, from_state, to_state, note, ts) VALUES (?, ?, ?, ?, ?)",
                (task_id, from_state, new_state.value, note, now),
            )
        # v3.6 BLOCKED-as-bug: every BLOCKED transition triggers a forensics
        # snapshot. Best-effort — a failure here must not crash the worker
        # (the BLOCK itself is what the operator needs first; the dump is
        # diagnostic, not load-bearing). We avoid re-capturing if the
        # `from_state` was already BLOCKED (defensive: re-blocking shouldn't
        # overwrite the original snapshot's framing).
        if new_state is State.BLOCKED and from_state != State.BLOCKED.value:
            try:
                self.capture_block_forensics(task_id)
            except Exception as e:
                log.warning("capture_block_forensics(%s) raised: %s — continuing", task_id, e)

    def apply_event(
        self: Any, task_id: str, event: Event | str, note: str | None = None, **fields: Any
    ) -> State:
        """Apply an FSM event to a task and persist the computed target state."""
        row = self.get(task_id)
        if row is None:
            raise InvalidTransition(f"task does not exist: {task_id}")
        target = State(target_for_event(row["state"], event).value)
        self.transition(task_id, target, note=note or str(event), **fields)
        return target

    def seed_merged_node(
        self: Any,
        task_id: str,
        *,
        source: str,
        evidence: str,
        seeded_at: float | None = None,
    ) -> None:
        """Insert deterministic fresh-workspace evidence that a DAG node is already merged."""

        now = seeded_at or time.time()
        with self.tx() as c:
            row = c.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO tasks "
                    "(id, state, seed_source, seed_evidence, seeded_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (task_id, State.MERGED.value, source, evidence, now, now, now),
                )
                from_state = None
            elif row["state"] == State.PENDING.value:
                c.execute(
                    "UPDATE tasks SET state = ?, seed_source = ?, seed_evidence = ?, "
                    "seeded_at = ?, updated_at = ? WHERE id = ?",
                    (State.MERGED.value, source, evidence, now, now, task_id),
                )
                from_state = State.PENDING.value
            elif row["state"] == State.MERGED.value:
                return
            else:
                raise ValueError(f"cannot seed {task_id}: task already exists in state {row['state']!r}")
            c.execute(
                "INSERT INTO state_log (task_id, from_state, to_state, note, ts) VALUES (?, ?, ?, ?, ?)",
                (task_id, from_state, State.MERGED.value, f"seed-from-base:{source}", now),
            )

    def get(self: Any, task_id: str) -> TaskRow:
        # _tx_lock serializes ALL connection access (reads + writes), not
        # just transactions. With check_same_thread=False the sqlite3
        # module accepts concurrent calls from multiple threads, but a
        # second thread starting an `execute` while the first is mid-fetch
        # raises `InterfaceError: bad parameter or other API misuse`.
        # Wrapping reads in the same lock as writes prevents that race.
        with self._tx_lock:
            r = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return cast(TaskRow, dict(r)) if r else cast(TaskRow, None)

    def all_tasks(self: Any) -> list[TaskRow]:
        with self._tx_lock:
            return [
                cast(TaskRow, dict(r))
                for r in self.conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
            ]

    def in_state(self: Any, *states: State) -> list[TaskRow]:
        if not states:
            return []
        q = ",".join("?" * len(states))
        with self._tx_lock:
            return [
                cast(TaskRow, dict(r))
                for r in self.conn.execute(
                    f"SELECT * FROM tasks WHERE state IN ({q}) ORDER BY id",
                    tuple(s.value for s in states),
                ).fetchall()
            ]

    def last_entered_state_ts(self: Any, task_id: str, state: State) -> float | None:
        """Most recent ts at which `task_id` transitioned INTO `state`, or None.

        Reads `state_log`. Used by the stacking-readiness gate to compute "how
        long has this parent been quietly in MERGE_READY?" — a parent that
        flapped through ADDRESSING_FEEDBACK and back gets a fresh ts and
        falls back below the quiet threshold until it stabilizes.
        """
        with self._tx_lock:
            r = self.conn.execute(
                "SELECT MAX(ts) FROM state_log WHERE task_id = ? AND to_state = ?",
                (task_id, state.value),
            ).fetchone()
        if r is None:
            return None
        ts = r[0]
        return float(ts) if ts is not None else None

    def subtask_progress(self: Any, task_id: str) -> tuple[int, int]:
        """Return (done, total) subtask counts for `task_id`.

        Used by the resume-boost in `score_candidate`: a task with most
        subtasks already DONE that returned to PENDING (orphan recovery,
        explicit resume) should outrank a fresh PENDING root with no work.
        """
        with self._tx_lock:
            row = self.conn.execute(
                "SELECT "
                "  SUM(CASE WHEN state='done' THEN 1 ELSE 0 END) AS done, "
                "  COUNT(*) AS total "
                "FROM subtasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return (0, 0)
        return (int(row["done"] or 0), int(row["total"] or 0))

    def completed_ids(self: Any) -> set[str]:
        with self._tx_lock:
            return {
                r["id"]
                for r in self.conn.execute(
                    "SELECT id FROM tasks WHERE state = ?", (State.MERGED.value,)
                ).fetchall()
            }

    def active_ids(self: Any) -> set[str]:
        with self._tx_lock:
            return {
                r["id"]
                for r in self.conn.execute(
                    "SELECT id FROM tasks WHERE state NOT IN (?, ?, ?, ?, ?, ?)",
                    (
                        State.PENDING.value,
                        State.MERGED.value,
                        State.BLOCKED.value,
                        State.FAILED.value,
                        State.ABORTED.value,
                        State.PENDING_CI.value,  # tasks waiting on merge block dependents until merged
                    ),
                ).fetchall()
            }

    def record_agent_call(
        self: Any,
        task_id: str,
        *,
        phase: str,
        cli: str,
        model: str | None,
        rc: int,
        duration_s: float,
        tokens_used: int | None,
        subtask_id: str | None = None,
        tokens_input: int | None = None,
        tokens_output: int | None = None,
        tokens_cached_read: int | None = None,
        tokens_cached_creation: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO agent_calls "
                "(task_id, phase, cli, model, rc, duration_s, tokens_used, "
                " tokens_input, tokens_output, tokens_cached_read, tokens_cached_creation, "
                " cost_usd, subtask_id, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    phase,
                    cli,
                    model,
                    rc,
                    duration_s,
                    tokens_used,
                    tokens_input,
                    tokens_output,
                    tokens_cached_read,
                    tokens_cached_creation,
                    cost_usd,
                    subtask_id,
                    time.time(),
                ),
            )
