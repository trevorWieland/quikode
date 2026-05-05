"""Per-tick SQLite reader. Centralizes all polling so widgets stay pure.

One `poll()` call returns snapshots for every panel. The TUI's `set_interval`
calls this on every tick (default 1s). Connection is opened once and cached;
WAL mode means we don't block the orchestrator's writers.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from quikode.config import Config, load_config
from quikode.dag import DAG
from quikode.state import State

from ..widgets.activity_feed import ActivityEntry
from ..widgets.detail_panel import DetailSnapshot, SubtaskRowSnapshot
from ..widgets.header import HeaderSnapshot
from ..widgets.resources_panel import ContainerSample, ResourcesSnapshot
from ..widgets.tasks_table import TaskRowSnapshot

# State -> short label that fits the table column without truncation.
_SHORT_STATE = {
    # v3.6 disambiguation: each "checking" variant gets a distinct label so
    # operators can tell at a glance which gate the task is currently in.
    # The base column has finite width — short forms below; the detail
    # panel + briefing render the long forms.
    "doing_subtask": "subtask_do",
    "checking_subtask": "subtask_check",
    "triaging_subtask": "subtask_triage",
    "final_checking": "final_check",
    "checking": "spec_check",  # v0.1 monolithic flow whole-spec checker
    "conflict_resolving": "conflict_res",
    "intent_reviewing": "intent_rev",
    "triaging_feedback": "triaging_fb",
    "addressing_feedback": "addressing_fb",
    "pending_ci": "pending_ci",
    "awaiting_review": "awaiting_rev",
    "merge_ready": "merge_ready",
    "local_ci_checking": "local_ci",
    "pre_pr_auditing": "pre_pr_audit",
    "pre_pr_triaging": "pre_pr_triage",
}

# Long-form descriptions used by the detail-panel phase line + briefing.
# The state column in tables is too narrow for these.
_LONG_STATE_DESCRIPTION = {
    "checking": "whole-spec checker (v0.1 legacy)",
    "checking_subtask": "per-subtask checker",
    "triaging_subtask": "per-subtask triage (root-causing FAIL)",
    "final_checking": "final whole-spec checker",
    "local_ci_checking": "local CI gate (just ci)",
    "pre_pr_auditing": "pre-PR audit gauntlet",
    "pre_pr_triaging": "merging audit findings → fixup planner",
    "triaging_feedback": "Python triage of review threads",
    "addressing_feedback": "fixup planner + per-subtask doer",
    "conflict_resolving": "spawned conflict-resolver agent",
    "intent_reviewing": "checking spec-compatibility after dep merge",
    "rebasing": "rebasing onto current main",
    "rebasing_to_main": "rebasing onto main (parent merged)",
    "fixup_planning": "planning fixup subtasks",
    "pending_ci": "PR open · CI running",
    "awaiting_review": "CI green · awaiting human/bot review",
    "merge_ready": "ready to merge",
    "doing_subtask": "running per-subtask doer",
    "doing": "running whole-spec doer (v0.1 legacy)",
    "triaging": "whole-spec triage (v0.1 legacy)",
    "replanning": "replanning after intent review",
}

_NON_TERMINAL_AGGREGATES = {
    "in_flight": {
        State.PROVISIONING.value,
        State.PLANNING.value,
        State.DOING_SUBTASK.value,
        State.CHECKING_SUBTASK.value,
        State.TRIAGING_SUBTASK.value,
        State.COMMITTING.value,
        State.PUSHING.value,
        State.PR_OPENING.value,
        State.POLLING_CI.value,
        State.REBASING.value,
        State.CONFLICT_RESOLVING.value,
        State.INTENT_REVIEWING.value,
        State.REPLANNING.value,
        State.TRIAGING_FEEDBACK.value,
        State.ADDRESSING_FEEDBACK.value,
    },
    "awaiting": {
        State.PENDING_CI.value,
        State.AWAITING_REVIEW.value,
        State.MERGE_READY.value,
    },
    "blocked": {State.BLOCKED.value, State.FAILED.value},
    "merged": {State.MERGED.value},
}


@dataclass
class PollSnapshot:
    """Bundle of per-tick snapshots, one per panel."""

    header: HeaderSnapshot
    tasks: list[TaskRowSnapshot]
    activity: list[ActivityEntry]
    resources: ResourcesSnapshot
    detail: DetailSnapshot
    error: str | None = None


class StorePoller:
    """Long-lived per-TUI poller. Cheap construction; expensive bit is the SQLite open."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self._cfg: Config | None = None
        self._conn: sqlite3.Connection | None = None
        self._dag: DAG | None = None
        self._dag_mtime: float | None = None
        self._last_error: str | None = None

    def _ensure_open(self) -> bool:
        """Try to open config + sqlite. Returns True if both succeed."""
        if self._conn is not None and self._cfg is not None:
            return True
        try:
            self._cfg = load_config(self.workspace)
        except FileNotFoundError as e:
            self._last_error = str(e)
            return False
        db_path = self._cfg.state_dir / "quikode.db"
        if not db_path.exists():
            self._last_error = f"no SQLite at {db_path} — run `quikode run` first"
            return False
        # Read-only connection; we never write from the TUI directly.
        try:
            self._conn = sqlite3.connect(
                f"file:{db_path}?mode=ro",
                uri=True,
                timeout=5,
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
        except sqlite3.Error as e:
            self._last_error = f"sqlite open failed: {e}"
            self._conn = None
            return False
        self._last_error = None
        return True

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _load_dag_cached(self) -> DAG | None:
        """Load the DAG, caching against its mtime so we re-parse only on edits."""
        if self._cfg is None:
            return None
        path = self._cfg.dag_path
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return self._dag
        if self._dag is None or self._dag_mtime != mtime:
            try:
                self._dag = DAG.load(path)
                self._dag_mtime = mtime
            except (OSError, ValueError):
                return self._dag
        return self._dag

    def poll(self, selected_task_id: str | None = None) -> PollSnapshot:
        if not self._ensure_open():
            return _empty_snapshot(self.workspace, error=self._last_error)
        assert self._cfg is not None and self._conn is not None
        cfg = self._cfg
        c = self._conn

        # --- tasks
        # Pull every task for header counts; filter terminal states out of the
        # main panel rows below. MERGED/ABORTED are static — they don't belong
        # in a "live work" view, the header's `merged: N/total` covers them.
        rows = c.execute(
            "SELECT id, state, branch, pr_number, pr_url, worktree_path, "
            "do_check_retries, ci_triage_retries, review_triage_retries, "
            "needs_intent_review, parent_task_id, last_error, "
            "review_round, intervention_request, "
            "pre_pr_audit_summary, "
            "updated_at, created_at "
            "FROM tasks ORDER BY "
            "  CASE state "
            "    WHEN 'blocked' THEN 0 WHEN 'failed' THEN 0 "
            "    WHEN 'merge_ready' THEN 1 "
            "    WHEN 'awaiting_review' THEN 1 "
            "    WHEN 'pending_ci' THEN 1 "
            "    WHEN 'merged' THEN 4 "
            "    WHEN 'aborted' THEN 4 "
            "    WHEN 'pending' THEN 3 "
            "    ELSE 2 END, id"
        ).fetchall()
        # States hidden from the primary tasks table — they're static or
        # bulk and crowd out live work. PENDING especially: a fresh tanren
        # workspace seeds 230+ pending rows that drown the in-flight set.
        # All hidden buckets remain counted in the header (merged / pending
        # implicit via `total_in_scope - merged - in_flight - awaiting -
        # blocked`), so the operator still sees the totals.
        _PANEL_HIDDEN = {State.MERGED.value, State.ABORTED.value, State.PENDING.value}

        # last state-log entry per task (for in-state-for)
        last_log_per_task: dict[str, float] = {}
        for r in c.execute("SELECT task_id, MAX(ts) AS ts FROM state_log GROUP BY task_id").fetchall():
            last_log_per_task[r["task_id"]] = float(r["ts"])

        # Most recent `→ pending` transition per task — this is the start of the
        # *current attempt*. `quikode retry` re-pends a task, so this resets
        # naturally on retry rather than carrying lifecycle from prior runs.
        attempt_start_per_task: dict[str, float] = {}
        for r in c.execute(
            "SELECT task_id, MAX(ts) AS ts FROM state_log WHERE to_state = 'pending' GROUP BY task_id"
        ).fetchall():
            attempt_start_per_task[r["task_id"]] = float(r["ts"])

        now = time.time()
        task_rows: list[TaskRowSnapshot] = []
        counts = {"in_flight": 0, "awaiting": 0, "blocked": 0, "merged": 0}
        for r in rows:
            state = r["state"]
            for bucket, members in _NON_TERMINAL_AGGREGATES.items():
                if state in members:
                    counts[bucket] += 1
            if state in _PANEL_HIDDEN:
                continue  # static; counted in header, omitted from main panel
            in_state = _humanize_seconds(now - last_log_per_task.get(r["id"], r["created_at"]))
            # Runtime = wall-clock since the most recent attempt began (last
            # → pending transition). Resets on `quikode retry`, so a fresh
            # restart shows a fresh runtime, not yesterday's accumulated hours.
            runtime = _humanize_seconds(now - attempt_start_per_task.get(r["id"], r["created_at"]))
            retries = "{}/{}/{}".format(
                r["do_check_retries"] or 0,
                r["ci_triage_retries"] or 0,
                r["review_triage_retries"] or 0,
            )
            dag = self._load_dag_cached()
            node = dag.nodes.get(r["id"]) if dag else None
            task_rows.append(
                TaskRowSnapshot(
                    task_id=r["id"],
                    title=node.title if node else "",
                    milestone=node.milestone if node else "",
                    state=_SHORT_STATE.get(state, state),
                    in_state_for=in_state,
                    runtime=runtime,
                    retries=retries,
                    branch_or_pr=_branch_or_pr(r),
                )
            )

        # --- activity feed: last 20 transitions
        activity: list[ActivityEntry] = []
        for r in c.execute(
            "SELECT task_id, from_state, to_state, note, ts FROM state_log ORDER BY ts DESC LIMIT 30"
        ).fetchall():
            ts_str = _dt.datetime.fromtimestamp(r["ts"], tz=ZoneInfo("UTC")).astimezone().strftime("%H:%M:%S")
            from_s = r["from_state"] or "(start)"
            to_s = r["to_state"]
            activity.append(
                ActivityEntry(
                    timestamp=ts_str,
                    task_id=r["task_id"],
                    transition=f"{from_s} → {to_s}",
                    note=r["note"] or "",
                )
            )

        # --- resources: latest container_stats per task
        latest_stats: dict[str, sqlite3.Row] = {}
        for r in c.execute(
            "SELECT cs1.task_id, cs1.cpu_pct, cs1.mem_bytes "
            "FROM container_stats cs1 "
            "INNER JOIN ("
            "  SELECT task_id, MAX(ts) AS mts FROM container_stats GROUP BY task_id"
            ") cs2 ON cs1.task_id = cs2.task_id AND cs1.ts = cs2.mts"
        ).fetchall():
            latest_stats[r["task_id"]] = r
        # Only show stats for tasks currently in an in-flight state.
        in_flight_ids = {r["id"] for r in rows if r["state"] in _NON_TERMINAL_AGGREGATES["in_flight"]}
        containers: list[ContainerSample] = []
        for tid, r in latest_stats.items():
            if tid not in in_flight_ids:
                continue
            cpu = float(r["cpu_pct"]) if r["cpu_pct"] is not None else None
            rss = float(r["mem_bytes"]) / 1024**3 if r["mem_bytes"] is not None else None
            containers.append(ContainerSample(task_id=tid, cpu_pct=cpu, rss_gb=rss))
        containers.sort(key=lambda x: x.task_id)
        host = _read_host_caps()

        resources = ResourcesSnapshot(
            host_cpus=host[0],
            host_mem_gb=host[1],
            cpu_per_task=cfg.cpu_per_task,
            mem_per_task_gb=cfg.mem_per_task_gb,
            max_parallel=cfg.max_parallel,
            containers=containers,
        )

        # --- header
        # total_in_scope is the DAG node count, not the seeded-task count —
        # so the merged% reflects DAG progress, not just what's in SQLite.
        dag = self._load_dag_cached()
        if dag is not None:
            total_tasks = len(dag.nodes)
            seeded_ids = {r["id"] for r in rows}
            merged_ids = {r["id"] for r in rows if r["state"] == State.MERGED.value}
            dag_ready_unseeded = sum(
                1
                for nid, n in dag.nodes.items()
                if nid not in seeded_ids and all(d in merged_ids for d in n.depends_on)
            )
        else:
            total_tasks = len(rows)
            dag_ready_unseeded = 0
        # Tokens used this run: SUM(tokens_used) across all agent_calls. Best-effort —
        # not every agent reports tokens, so this is a lower bound.
        tokens_row = c.execute("SELECT COALESCE(SUM(tokens_used), 0) AS t FROM agent_calls").fetchone()
        tokens_total = int(tokens_row["t"]) if tokens_row and tokens_row["t"] else None
        header = HeaderSnapshot(
            workspace_path=str(self.workspace),
            stacking_strategy=cfg.stacking_strategy.value,
            max_parallel=cfg.max_parallel,
            cpu_per_task=cfg.cpu_per_task,
            mem_per_task_gb=cfg.mem_per_task_gb,
            in_flight=counts["in_flight"],
            awaiting=counts["awaiting"],
            blocked=counts["blocked"],
            merged=counts["merged"],
            total_in_scope=total_tasks,
            dag_ready_unseeded=dag_ready_unseeded,
            tokens_total=tokens_total,
            orchestrator_running=False,  # wired up by app.py with parting status check
        )

        detail = self._build_detail(selected_task_id, rows)
        return PollSnapshot(
            header=header,
            tasks=task_rows,
            activity=activity,
            resources=resources,
            detail=detail,
            error=None,
        )

    def _build_detail(self, selected_task_id: str | None, all_rows: list[sqlite3.Row]) -> DetailSnapshot:
        if not selected_task_id or self._conn is None:
            return DetailSnapshot(task_id="(none)")
        c = self._conn
        match = next((r for r in all_rows if r["id"] == selected_task_id), None)
        if match is None:
            return DetailSnapshot(task_id=selected_task_id)

        dag = self._load_dag_cached()
        node = dag.nodes.get(selected_task_id) if dag else None
        title = node.title if node else ""

        # Phase context: latest state_log entry (note + ts) and worktree mtime.
        # The phase line surfaces "whole-spec fixup attempt 1 · 32m in" so the
        # user can tell something is running even when no subtask is in flight.
        log_row = c.execute(
            "SELECT to_state, note, ts FROM state_log WHERE task_id = ? ORDER BY ts DESC LIMIT 1",
            (selected_task_id,),
        ).fetchone()
        now = time.time()
        last_state_note = ""
        in_state_for = ""
        if log_row:
            last_state_note = log_row["note"] or ""
            in_state_for = _humanize_seconds(now - float(log_row["ts"]))
        last_worktree_edit = ""
        wt_path = match["worktree_path"]  # column included in the tasks SELECT above
        if wt_path:
            mtime = _worktree_recent_mtime(Path(wt_path))
            if mtime is not None:
                last_worktree_edit = _humanize_seconds(now - mtime)

        # Subtasks — structured rows for the DataTable, with active-index hint.
        sub_rows: list[SubtaskRowSnapshot] = []
        active_idx = -1
        active_states = {"doing", "checking", "triaging"}
        for i, r in enumerate(
            c.execute(
                "SELECT subtask_id, state, retries, title FROM subtasks WHERE task_id = ? ORDER BY id",
                (selected_task_id,),
            ).fetchall()
        ):
            sub_rows.append(
                SubtaskRowSnapshot(
                    subtask_id=r["subtask_id"],
                    title=r["title"] or "",
                    state=r["state"],
                    retries=int(r["retries"] or 0),
                )
            )
            if r["state"] in active_states and active_idx < 0:
                active_idx = i

        # Agent calls — rendered newest-first (caller reverses on render).
        calls = [
            "  {ts}  {phase:<18s}  {cli:<8s}  rc={rc}  dur={dur}  tokens={tok}".format(
                ts=_dt.datetime.fromtimestamp(r["ts"], tz=ZoneInfo("UTC")).astimezone().strftime("%H:%M:%S"),
                phase=r["phase"],
                cli=r["cli"],
                rc=r["rc"],
                dur=f"{r['duration_s']:.1f}s" if r["duration_s"] else "—",
                tok=r["tokens_used"] or "—",
            )
            for r in c.execute(
                "SELECT ts, phase, cli, rc, duration_s, tokens_used "
                "FROM agent_calls WHERE task_id = ? ORDER BY ts DESC LIMIT 20",
                (selected_task_id,),
            ).fetchall()
        ]

        # v3 review-loop surfacing: when addressing_feedback, fish round
        # count + active thread count out of intervention_request (JSON blob
        # the worker stashes when it picks up a review). Best-effort — older
        # rows may not have these fields; phase line falls back to a generic
        # "responding to review feedback" note.
        review_round: int | None = None
        review_threads_count: int | None = None
        try:
            rr = match["review_round"]
            review_round = int(rr) if rr is not None else None
        except (KeyError, IndexError, ValueError, TypeError):
            review_round = None
        try:
            blob = match["intervention_request"]
            if blob:
                parsed = json.loads(blob)
                threads = parsed.get("threads") if isinstance(parsed, dict) else None
                if isinstance(threads, list):
                    review_threads_count = len(threads)
        except (KeyError, IndexError, json.JSONDecodeError, TypeError):
            review_threads_count = None

        # v3.6 pre-PR audit gauntlet: parse the most recent cycle summary so
        # the detail panel can render one row per stage with pass/fail/queued.
        pre_pr_audit_cycle: int | None = None
        pre_pr_audit_stages: list[dict] = []
        try:
            blob = match["pre_pr_audit_summary"]
        except (KeyError, IndexError):
            blob = None
        if blob:
            try:
                parsed = json.loads(blob)
                if isinstance(parsed, dict):
                    pre_pr_audit_cycle = int(parsed["cycle"]) if parsed.get("cycle") is not None else None
                    stages = parsed.get("stages") or []
                    if isinstance(stages, list):
                        pre_pr_audit_stages = [s for s in stages if isinstance(s, dict)]
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        return DetailSnapshot(
            task_id=selected_task_id,
            title=title,
            subtasks=sub_rows,
            agent_calls=calls,
            active_subtask_idx=active_idx,
            task_state=match["state"],
            last_state_note=last_state_note,
            in_state_for=in_state_for,
            last_worktree_edit=last_worktree_edit,
            review_round=review_round,
            review_threads_count=review_threads_count,
            pre_pr_audit_cycle=pre_pr_audit_cycle,
            pre_pr_audit_stages=pre_pr_audit_stages,
        )


# ----- helpers -----


def _empty_snapshot(workspace: Path, *, error: str | None) -> PollSnapshot:
    return PollSnapshot(
        header=HeaderSnapshot(
            workspace_path=str(workspace),
            stacking_strategy="—",
            max_parallel=0,
            cpu_per_task=0,
            mem_per_task_gb=0,
            in_flight=0,
            awaiting=0,
            blocked=0,
            merged=0,
            total_in_scope=0,
        ),
        tasks=[],
        activity=[],
        resources=ResourcesSnapshot(
            host_cpus=None,
            host_mem_gb=None,
            cpu_per_task=0,
            mem_per_task_gb=0,
            max_parallel=0,
            containers=[],
        ),
        detail=DetailSnapshot(
            task_id="(none)",
            plan_summary=f"[yellow]workspace not ready[/]\n\n{error or ''}",
        ),
        error=error,
    )


def _branch_or_pr(r: sqlite3.Row) -> str:
    """Pick the most useful identifier for the table's right column."""
    if r["pr_number"]:
        return f"#{r['pr_number']}"
    if r["branch"]:
        return r["branch"]
    if r["last_error"]:
        # Truncate aggressively — the table column has finite width.
        excerpt = r["last_error"].split("\n", 1)[0]
        return f"[red]{excerpt[:60]}[/]"
    return "—"


def _humanize_seconds(s: float) -> str:
    if s < 0:
        return "—"
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s // 60)}m{int(s % 60):02d}s"
    h = int(s // 3600)
    return f"{h}h{int((s % 3600) // 60):02d}m"


def _read_host_caps() -> tuple[int | None, int | None]:
    """Best-effort: read host CPU + memory from /proc."""
    cpus: int | None
    cpus = os.cpu_count()
    mem_gb: int | None = None
    try:
        with Path("/proc/meminfo").open() as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    mem_gb = round(kb / 1024 / 1024)
                    break
    except OSError:
        mem_gb = None
    return cpus, mem_gb


# Skip these path components when computing worktree mtime — they churn
# constantly even when the agent isn't actually working (cache lookups,
# build-tool metadata) and would mask real idleness.
_WORKTREE_MTIME_SKIP = (".git", "target", ".rumdl_cache", "node_modules", ".next", "__pycache__")


def _worktree_recent_mtime(worktree: Path) -> float | None:
    """Most recent file mtime under the worktree, ignoring caches/build dirs.

    Cheap-ish: walks once. The TUI polls every 1s; for a 200-file worktree
    this is ~10ms. For pathological worktrees we'd want a watchman-style
    delta but it's not the bottleneck right now.
    """
    if not worktree.exists():
        return None
    latest = 0.0
    try:
        for root, dirs, files in os.walk(worktree):
            # In-place prune: skip churning dirs.
            dirs[:] = [d for d in dirs if d not in _WORKTREE_MTIME_SKIP]
            for f in files:
                try:
                    m = (Path(root) / f).stat().st_mtime
                except OSError:
                    continue
                latest = max(latest, m)
    except OSError:
        return None
    return latest if latest > 0 else None
