from __future__ import annotations

import logging
import time
from typing import Any, cast

from quikode.fsm import Event, InvalidTransition, target_for_event
from quikode.state_types import Phase, PrReviewTrigger, State, TaskRow

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

    def enter_phase(
        self: Any,
        task_id: str,
        phase: Phase,
        *,
        cycle_in_phase: int = 1,
        pr_review_trigger: PrReviewTrigger = PrReviewTrigger.NONE,
        note: str | None = None,
    ) -> None:
        """Plan 58: phase-transition writer.

        Sets `phase`, `cycle_in_phase`, `pr_review_trigger` atomically and
        records a synthetic state_log entry (same row state, note carrying
        the phase change) so historical context is preserved alongside the
        FSM trail.
        """
        now = time.time()
        with self.tx() as c:
            r = c.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
            current_state = r["state"] if r else None
            c.execute(
                "UPDATE tasks SET phase = ?, cycle_in_phase = ?, "
                "pr_review_trigger = ?, updated_at = ? WHERE id = ?",
                (phase.value, cycle_in_phase, pr_review_trigger.value, now, task_id),
            )
            c.execute(
                "INSERT INTO state_log (task_id, from_state, to_state, note, ts) VALUES (?, ?, ?, ?, ?)",
                (
                    task_id,
                    current_state,
                    current_state,
                    note or f"phase→{phase.value} cycle={cycle_in_phase} trigger={pr_review_trigger.value}",
                    now,
                ),
            )

    def increment_cycle_in_phase(
        self: Any,
        task_id: str,
        *,
        pr_review_trigger: PrReviewTrigger | None = None,
        note: str | None = None,
    ) -> int:
        """Plan 58: bump `cycle_in_phase` by 1; optionally stamp a new
        `pr_review_trigger` (PR-review fixup trigger source). Returns the
        new cycle value."""
        now = time.time()
        with self.tx() as c:
            r = c.execute(
                "SELECT state, phase, cycle_in_phase, pr_review_trigger FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if r is None:
                raise InvalidTransition(f"task does not exist: {task_id}")
            new_cycle = int(r["cycle_in_phase"] or 0) + 1
            new_trigger = pr_review_trigger.value if pr_review_trigger is not None else r["pr_review_trigger"]
            c.execute(
                "UPDATE tasks SET cycle_in_phase = ?, pr_review_trigger = ?, updated_at = ? WHERE id = ?",
                (new_cycle, new_trigger, now, task_id),
            )
            c.execute(
                "INSERT INTO state_log (task_id, from_state, to_state, note, ts) VALUES (?, ?, ?, ?, ?)",
                (
                    task_id,
                    r["state"],
                    r["state"],
                    note or f"cycle_in_phase→{new_cycle} trigger={new_trigger}",
                    now,
                ),
            )
        return new_cycle

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
        now = time.time()
        with self.tx() as c:
            c.execute(
                "INSERT INTO agent_calls "
                "(task_id, phase, cli, model, rc, duration_s, tokens_used, "
                " tokens_input, tokens_output, tokens_cached_read, tokens_cached_creation, "
                " cost_usd, subtask_id, started_at, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    now,
                    now,
                ),
            )

    def record_agent_call_started(
        self: Any,
        task_id: str,
        *,
        phase: str,
        cli: str,
        model: str | None,
        subtask_id: str | None = None,
    ) -> int:
        """Plan 38 PR-C: insert a start-marker row before invoking the agent.

        Returns the new row's `id`; the caller passes it to
        `record_agent_call_finished` once the agent returns. While `rc`
        and `duration_s` are NULL the TUI's "agent in-flight" detector
        treats this row as live work. Crash-safe: a worker that exits
        before calling `_finished` leaves the row as "stuck in-flight"
        — operators (or the daemon supervisor) can spot the staleness
        from `started_at` age.
        """
        now = time.time()
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO agent_calls "
                "(task_id, phase, cli, model, subtask_id, started_at, ts, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'running')",
                (task_id, phase, cli, model, subtask_id, now, now),
            )
            row_id = cur.lastrowid
        if row_id is None:  # pragma: no cover — sqlite3 always returns an id
            raise RuntimeError("INSERT INTO agent_calls returned no lastrowid")
        return int(row_id)

    def record_agent_call_finished(
        self: Any,
        call_id: int,
        *,
        rc: int,
        duration_s: float,
        tokens_used: int | None = None,
        tokens_input: int | None = None,
        tokens_output: int | None = None,
        tokens_cached_read: int | None = None,
        tokens_cached_creation: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        """Plan 38 PR-C: complete the start-marker row inserted by
        `record_agent_call_started`. Updates rc/duration_s/tokens/cost
        and bumps `ts` to the finish moment so chronological ordering
        by ts still reflects call completion."""
        now = time.time()
        with self.tx() as c:
            c.execute(
                "UPDATE agent_calls SET "
                "  rc = ?, duration_s = ?, tokens_used = ?, "
                "  tokens_input = ?, tokens_output = ?, "
                "  tokens_cached_read = ?, tokens_cached_creation = ?, "
                "  cost_usd = ?, ts = ? "
                "WHERE id = ?",
                (
                    rc,
                    duration_s,
                    tokens_used,
                    tokens_input,
                    tokens_output,
                    tokens_cached_read,
                    tokens_cached_creation,
                    cost_usd,
                    now,
                    call_id,
                ),
            )

    def update_agent_call_status(self: Any, call_id: int, status: str) -> None:
        """Plan 59 fix B: set fine-grained in-flight status on the agent_call.

        Used by `_run_with_retry` to mark a call as `backoff_auth` /
        `backoff_container` while the transport is sleeping between
        retries, and back to `running` when the retry actually fires.

        The column is constrained by convention (no CHECK at the SQL
        level) — accepted values are `running`, `backoff_auth`,
        `backoff_container`. Unknown values pass through; the TUI
        renders them verbatim so a future status surfaces immediately
        rather than being silently dropped.
        """
        with self.tx() as c:
            c.execute("UPDATE agent_calls SET status = ? WHERE id = ?", (status, call_id))

    def agent_in_flight_status(
        self: Any, task_id: str, *, now: float | None = None
    ) -> tuple[str, str | None, float | None, int | None, str | None]:
        """Plan 38 PR-C / plan 59 fix B: 'agent in-flight' status for the
        TUI / briefing.

        Reads the LATEST `agent_calls` row for `task_id` and returns
        the 5-tuple `(status_class, phase, age_s, last_rc, sub_status)`:

        * `status_class` ∈ {`running`, `idle`, `never`} — the high-level
          state class (matches plan 38 PR-C semantics).
        * `phase` — the agent phase (`subtask_doer`, `subtask_checker`,
          …) or `None` when no row exists.
        * `age_s` — seconds since `started_at` for `running`, or since
          `ts` for `idle`. `None` when no row exists.
        * `last_rc` — set on `idle` so operators see a recent rc=124
          timeout. `None` on `running` and `never`.
        * `sub_status` — plan 59 fine-grained status for `running`
          rows: `running` (subprocess executing), `backoff_auth` (60s
          auth-refresh sleep), `backoff_container` (rare container
          recovery sleep). `None` on `idle` / `never`.

        The TUI / detail panel renders "subtask_doer backoff_auth 45s"
        when `sub_status != "running"` so the operator sees the worker
        is actively waiting on auth refresh, not silently stalled.
        """
        wall = time.time() if now is None else now
        row = self.conn.execute(
            "SELECT phase, rc, started_at, ts, status FROM agent_calls "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            return ("never", None, None, None, None)
        phase = str(row["phase"]) if row["phase"] is not None else None
        if row["rc"] is None:
            started = float(row["started_at"]) if row["started_at"] is not None else float(row["ts"])
            sub_status = str(row["status"]) if row["status"] is not None else "running"
            return ("running", phase, max(0.0, wall - started), None, sub_status)
        finished = float(row["ts"])
        return ("idle", phase, max(0.0, wall - finished), int(row["rc"]), None)
