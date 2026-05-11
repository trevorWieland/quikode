"""Heartbeat, container sampling, and stalled future recovery mixin."""

from __future__ import annotations

import sys
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from quikode import fsm_runtime
from quikode.state import State


class _RunnerGlobals:
    def __getattr__(self: Any, name: str) -> Any:
        return getattr(sys.modules["quikode.orchestration.runner"], name)


_rt = _RunnerGlobals()


class SupervisionMixin:
    def _sample_container_stats(self: Any) -> None:
        """Periodic snapshot of cpu/mem usage for every active task's dev container.
        Records to `container_stats` table — used by `quikode resources` and
        `quikode briefing` to show live + max-RSS."""
        for row in self.store.in_state(
            *[
                State.PROVISIONING,
                State.PLANNING,
                State.DOING_SUBTASK,
                State.CHECKING_SUBTASK,
                State.TRIAGING_SUBTASK,
                State.COMMITTING,
                State.PUSHING,
                State.PR_OPENING,
            ]
        ):
            cid = row.get("container_id")
            if not cid:
                continue
            # We don't have the handle on the orchestrator side, but the
            # container name follows a stable pattern: qk-<slug>-<hex>-dev.
            # We can infer it by matching the running containers.
            for c in _rt.docker_env.list_quikode_containers():
                if c["name"].endswith("-dev") and c["id"].startswith(cid[:12]):
                    stats = _rt.docker_env.sample_container_stats(c["name"])
                    if stats:
                        self.store.record_container_stats(
                            row["id"],
                            c["name"],
                            stats.get("cpu_pct"),
                            stats.get("mem_bytes"),
                            stats.get("mem_pct"),
                        )
                    break

    def _check_stalls(
        self: Any,
        warned: dict[str, float],
        futures: dict[str, Future] | None = None,
        review_response_futures: set[str] | None = None,
    ) -> None:
        """Warn once per stall-window when a DOING task's worktree has been quiet,
        AND auto-recover review-response futures that are silently stalled.

        Only DOING is expected to produce file edits — planner/checker/triage
        phases are read-only. We only warn during DOING.

        v3 follow-up to the 2026-05-04 R-0002 / R-0015 pool-slot leaks:
        when a `addressing_feedback` task has logged ZERO agent_call
        activity for `cfg.stall_warn_seconds` (default 1800s = 30min),
        the future is almost certainly leaked (silently crashed before
        the first agent invocation). We force-cancel + reset the task to
        PENDING_CI so the watcher's next tick re-dispatches against
        a fresh pool slot. Without this, R-0002-style stalls persist
        indefinitely (every minute of stall = $0 progress + 1 reserved
        pool slot starving real work).
        """
        threshold = self.cfg.stall_warn_seconds
        if threshold <= 0:
            return
        now = _rt.time.time()
        for row in self.store.in_state(State.DOING_SUBTASK):
            wt = row.get("worktree_path")
            if not wt:
                continue
            mt = _rt._worktree_mtime(Path(wt))
            if mt is None:
                continue
            quiet = now - mt
            if quiet < threshold:
                warned.pop(row["id"], None)
                continue
            last_warned = warned.get(row["id"], 0)
            if now - last_warned < threshold:
                continue  # already warned in this window
            _rt.log.warning(
                "task %s appears stalled: doer worktree quiet for %d min (no file edits since %s)",
                row["id"],
                int(quiet // 60),
                _rt.time.strftime("%H:%M:%S", _rt.time.localtime(mt)),
            )
            warned[row["id"]] = now

        # Stalled review-response detector. Plan 58: ADDRESSING_FEEDBACK
        # retired — a stalled review response now sits in one of the
        # audit-stage states (most likely AUDIT_LOCAL_CI since that's the
        # entry point for both CI_FIXUP_START and REVIEW_FIXUP_START).
        if futures is None or review_response_futures is None:
            return
        review_stuck_states = {
            State.AUDIT_LOCAL_CI.value,
            State.AUDIT_RUBRIC.value,
            State.AUDIT_STANDARDS.value,
            State.AUDIT_ARCHITECTURE.value,
            State.AUDIT_BEHAVIOR.value,
            State.FIXUP_PLANNING.value,
        }
        for tid in list(review_response_futures):
            row = self.store.get(tid)
            if row is None:
                continue
            if row["state"] not in review_stuck_states:
                continue  # not stalled — task moved on
            # Most-recent agent_call timestamp for this task.
            last_call = self.store.conn.execute(
                "SELECT MAX(ts) FROM agent_calls WHERE task_id = ?",
                (tid,),
            ).fetchone()
            last_ts = float(last_call[0]) if last_call and last_call[0] else 0.0
            # last_ts may be from a PRIOR review-response cycle. Compare
            # against when this task last entered the audit gauntlet.
            entered = self.store.conn.execute(
                "SELECT MAX(ts) FROM state_log WHERE task_id = ? AND to_state = ?",
                (tid, State.AUDIT_LOCAL_CI.value),
            ).fetchone()
            entered_ts = float(entered[0]) if entered and entered[0] else 0.0
            # Effective "silence start" = max(entered, last agent_call). If
            # silent since either, that's our window.
            silence_start = max(entered_ts, last_ts)
            silence = now - silence_start
            if silence < threshold:
                continue
            _rt.log.error(
                "task %s: review-response stalled %d min — no agent_call since %s; "
                "resetting to PENDING_CI for re-dispatch",
                tid,
                int(silence // 60),
                _rt.time.strftime("%H:%M:%S", _rt.time.localtime(silence_start)),
            )
            # Cancel the leaked Future (best-effort — may already be in a
            # broken state, can't .cancel() if running, but discard from
            # tracking sets so the slot frees on the next reap pass).
            fut = futures.get(tid)
            if fut is not None:
                fut.cancel()
                # Best-effort: if cancel fails because the future is "running"
                # (the silent-leak case), the future is wedged — drop it from
                # `futures` directly so the slot frees. The Future object will
                # eventually be garbage-collected; the worker thread it points
                # to is presumably already dead.
                futures.pop(tid, None)
            review_response_futures.discard(tid)
            # Plan 58: force-recover via the dedicated fsm_runtime escape
            # hatch. No FSM event maps "audit-stage → PENDING_CI" cleanly;
            # the helper records the supervisor rationale + uses the
            # raw transition path under an architecture-guard allowlist.
            fsm_runtime.force_recover_to_pending_ci(
                self.store,
                tid,
                note=(
                    f"orchestrator force-recovery: audit-stage stalled "
                    f"{int(silence // 60)}min, slot freed for re-dispatch"
                ),
            )

    def _write_heartbeat(self: Any, in_flight: int, audit_futures: int) -> None:
        """Write a small JSON liveness blob to `state_dir/orchestrator.heartbeat`.

        Plan 58: post-PR fixup work no longer collapses into
        ADDRESSING_FEEDBACK; we count tasks across the audit-stage states
        + FIXUP_PLANNING for the heartbeat. `audit_futures` is the
        review-response futures count (kept as a backward-compatible
        field name in the JSON for now).
        """
        try:
            self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
            pending_ci = len(self.store.in_state(State.PENDING_CI))
            responding = len(
                self.store.in_state(
                    State.AUDIT_LOCAL_CI,
                    State.AUDIT_RUBRIC,
                    State.AUDIT_STANDARDS,
                    State.AUDIT_ARCHITECTURE,
                    State.AUDIT_BEHAVIOR,
                    State.FIXUP_PLANNING,
                )
            )
            payload = {
                "ts": _rt.time.time(),
                "in_flight": in_flight,
                "pending_ci": pending_ci,
                # Plan 58: keep the pre-plan-58 key for any operator scripts
                # that grep the heartbeat; semantics now = "tasks in any
                # audit-stage or fixup-planning state".
                "addressing_feedback": responding,
                "addressing_feedback_futures": audit_futures,
                "audit_in_flight": responding,
                "audit_futures": audit_futures,
                "max_parallel": int(self.cfg.max_parallel),
            }
            (self.cfg.state_dir / "orchestrator.heartbeat").write_text(_rt.json.dumps(payload))
        except OSError as e:
            _rt.log.debug("heartbeat write failed: %s", e)
