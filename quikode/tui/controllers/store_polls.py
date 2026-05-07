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

from quikode.config import Config
from quikode.config_loader import load_config
from quikode.daemon import read_heartbeat
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
    "conflict_resolving": "conflict_res",
    "addressing_feedback": "addressing_fb",
    "pending_ci": "pending_ci",
    "awaiting_review": "awaiting_rev",
    "local_ci_checking": "local_ci",
    "pre_pr_auditing": "pre_pr_audit",
    "fixup_planning": "fixup_plan",
}

# Long-form descriptions used by the detail-panel phase line + briefing.
# The state column in tables is too narrow for these.
_LONG_STATE_DESCRIPTION = {
    "checking_subtask": "per-subtask checker",
    "triaging_subtask": "per-subtask triage (root-causing FAIL)",
    "local_ci_checking": "local CI gate (just ci)",
    "pre_pr_auditing": "pre-PR audit gauntlet",
    "fixup_planning": "planning fixup subtasks",
    "addressing_feedback": "fixup planner + per-subtask doer (CI fail or CHANGES_REQUESTED)",
    "conflict_resolving": "spawned conflict-resolver agent",
    "rebasing_to_main": "rebasing onto main (parent merged)",
    "pending_ci": "PR open · CI running",
    "awaiting_review": "CI green · awaiting formal GitHub review",
    "doing_subtask": "running per-subtask doer",
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
        State.REBASING_TO_MAIN.value,
        State.CONFLICT_RESOLVING.value,
        State.FIXUP_PLANNING.value,
        State.ADDRESSING_FEEDBACK.value,
    },
    "awaiting": {
        State.PENDING_CI.value,
        State.AWAITING_REVIEW.value,
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
        rows = self._task_rows(c)
        task_rows, counts = self._build_task_rows(rows)
        header = self._build_header(cfg, c, rows, counts)
        detail = self._build_detail(selected_task_id, rows)
        return PollSnapshot(
            header=header,
            tasks=task_rows,
            activity=self._build_activity(c),
            resources=self._build_resources(cfg, c, rows),
            detail=detail,
            error=None,
        )

    def _task_rows(self, c: sqlite3.Connection) -> list[sqlite3.Row]:
        return c.execute(
            "SELECT id, state, branch, pr_number, pr_url, worktree_path, "
            "ci_triage_retries, "
            "needs_intent_review, parent_task_ids, last_error, "
            "review_round, intervention_request, "
            "pre_pr_audit_summary, "
            "updated_at, created_at "
            "FROM tasks ORDER BY "
            "  CASE state "
            "    WHEN 'blocked' THEN 0 WHEN 'failed' THEN 0 "
            "    WHEN 'awaiting_review' THEN 1 "
            "    WHEN 'pending_ci' THEN 1 "
            "    WHEN 'merged' THEN 4 "
            "    WHEN 'aborted' THEN 4 "
            "    WHEN 'pending' THEN 3 "
            "    ELSE 2 END, id"
        ).fetchall()

    def _build_task_rows(self, rows: list[sqlite3.Row]) -> tuple[list[TaskRowSnapshot], dict[str, int]]:
        hidden = {State.MERGED.value, State.ABORTED.value, State.PENDING.value}
        last_log_per_task = self._latest_state_log_ts()
        attempt_start_per_task = self._attempt_start_ts()
        now = time.time()
        dag = self._load_dag_cached()
        task_rows: list[TaskRowSnapshot] = []
        counts = {"in_flight": 0, "awaiting": 0, "blocked": 0, "merged": 0}
        for row in rows:
            state = row["state"]
            for bucket, members in _NON_TERMINAL_AGGREGATES.items():
                if state in members:
                    counts[bucket] += 1
            if state in hidden:
                continue
            node = dag.nodes.get(row["id"]) if dag else None
            task_rows.append(
                TaskRowSnapshot(
                    task_id=row["id"],
                    title=node.title if node else "",
                    milestone=node.milestone if node else "",
                    state=_SHORT_STATE.get(state, state),
                    in_state_for=_humanize_seconds(now - last_log_per_task.get(row["id"], row["created_at"])),
                    runtime=_humanize_seconds(now - attempt_start_per_task.get(row["id"], row["created_at"])),
                    retries=str(row["ci_triage_retries"] or 0),
                    branch_or_pr=_branch_or_pr(row),
                )
            )
        return task_rows, counts

    def _latest_state_log_ts(self) -> dict[str, float]:
        assert self._conn is not None
        return {
            row["task_id"]: float(row["ts"])
            for row in self._conn.execute(
                "SELECT task_id, MAX(ts) AS ts FROM state_log GROUP BY task_id"
            ).fetchall()
        }

    def _attempt_start_ts(self) -> dict[str, float]:
        assert self._conn is not None
        return {
            row["task_id"]: float(row["ts"])
            for row in self._conn.execute(
                "SELECT task_id, MAX(ts) AS ts FROM state_log WHERE to_state = 'pending' GROUP BY task_id"
            ).fetchall()
        }

    def _build_activity(self, c: sqlite3.Connection) -> list[ActivityEntry]:
        rows = c.execute(
            "SELECT task_id, from_state, to_state, note, ts FROM state_log ORDER BY ts DESC LIMIT 30"
        ).fetchall()
        return [
            ActivityEntry(
                timestamp=_dt.datetime.fromtimestamp(row["ts"], tz=ZoneInfo("UTC"))
                .astimezone()
                .strftime("%H:%M:%S"),
                task_id=row["task_id"],
                transition=f"{row['from_state'] or '(start)'} → {row['to_state']}",
                note=row["note"] or "",
            )
            for row in rows
        ]

    def _build_resources(
        self, cfg: Config, c: sqlite3.Connection, rows: list[sqlite3.Row]
    ) -> ResourcesSnapshot:
        in_flight_ids = {row["id"] for row in rows if row["state"] in _NON_TERMINAL_AGGREGATES["in_flight"]}
        containers = [
            sample for sample in self._latest_container_samples(c) if sample.task_id in in_flight_ids
        ]
        containers.sort(key=lambda sample: sample.task_id)
        host_cpus, host_mem_gb = _read_host_caps()
        return ResourcesSnapshot(
            host_cpus=host_cpus,
            host_mem_gb=host_mem_gb,
            cpu_per_task=cfg.cpu_per_task,
            mem_per_task_gb=cfg.mem_per_task_gb,
            max_parallel=_runtime_max_parallel(cfg),
            containers=containers,
        )

    def _latest_container_samples(self, c: sqlite3.Connection) -> list[ContainerSample]:
        rows = c.execute(
            "SELECT cs1.task_id, cs1.cpu_pct, cs1.mem_bytes "
            "FROM container_stats cs1 "
            "INNER JOIN ("
            "  SELECT task_id, MAX(ts) AS mts FROM container_stats GROUP BY task_id"
            ") cs2 ON cs1.task_id = cs2.task_id AND cs1.ts = cs2.mts"
        ).fetchall()
        return [
            ContainerSample(
                task_id=row["task_id"],
                cpu_pct=float(row["cpu_pct"]) if row["cpu_pct"] is not None else None,
                rss_gb=float(row["mem_bytes"]) / 1024**3 if row["mem_bytes"] is not None else None,
            )
            for row in rows
        ]

    def _build_header(
        self,
        cfg: Config,
        c: sqlite3.Connection,
        rows: list[sqlite3.Row],
        counts: dict[str, int],
    ) -> HeaderSnapshot:
        total_tasks, dag_ready_unseeded = self._dag_progress(rows)
        tokens_row = c.execute("SELECT COALESCE(SUM(tokens_used), 0) AS t FROM agent_calls").fetchone()
        tokens_total = int(tokens_row["t"]) if tokens_row and tokens_row["t"] else None
        return HeaderSnapshot(
            workspace_path=str(self.workspace),
            stacking_strategy=cfg.stacking_strategy.value,
            max_parallel=_runtime_max_parallel(cfg),
            cpu_per_task=cfg.cpu_per_task,
            mem_per_task_gb=cfg.mem_per_task_gb,
            in_flight=counts["in_flight"],
            awaiting=counts["awaiting"],
            blocked=counts["blocked"],
            merged=counts["merged"],
            total_in_scope=total_tasks,
            dag_ready_unseeded=dag_ready_unseeded,
            tokens_total=tokens_total,
            orchestrator_running=False,
        )

    def _dag_progress(self, rows: list[sqlite3.Row]) -> tuple[int, int]:
        dag = self._load_dag_cached()
        if dag is None:
            return len(rows), 0
        seeded_ids = {row["id"] for row in rows}
        merged_ids = {row["id"] for row in rows if row["state"] == State.MERGED.value}
        dag_ready_unseeded = sum(
            1
            for node_id, node in dag.nodes.items()
            if node_id not in seeded_ids and all(dep in merged_ids for dep in node.depends_on)
        )
        return len(dag.nodes), dag_ready_unseeded

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

        last_state_note, in_state_for, last_worktree_edit = self._detail_phase(selected_task_id, match)
        sub_rows, active_idx = self._detail_subtasks(c, selected_task_id)
        review_round, review_threads_count = _detail_review_context(match)
        pre_pr_audit_cycle, pre_pr_audit_stages = _detail_pre_pr_audit(match)

        return DetailSnapshot(
            task_id=selected_task_id,
            title=title,
            subtasks=sub_rows,
            agent_calls=self._detail_agent_calls(c, selected_task_id),
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

    def _detail_phase(self, task_id: str, row: sqlite3.Row) -> tuple[str, str, str]:
        assert self._conn is not None
        log_row = self._conn.execute(
            "SELECT to_state, note, ts FROM state_log WHERE task_id = ? ORDER BY ts DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        now = time.time()
        last_state_note = log_row["note"] or "" if log_row else ""
        in_state_for = _humanize_seconds(now - float(log_row["ts"])) if log_row else ""
        last_worktree_edit = ""
        wt_path = row["worktree_path"]
        if wt_path:
            mtime = _worktree_recent_mtime(Path(wt_path))
            if mtime is not None:
                last_worktree_edit = _humanize_seconds(now - mtime)
        return last_state_note, in_state_for, last_worktree_edit

    def _detail_subtasks(self, c: sqlite3.Connection, task_id: str) -> tuple[list[SubtaskRowSnapshot], int]:
        rows = c.execute(
            "SELECT subtask_id, state, retries, title FROM subtasks WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        active_idx = -1
        sub_rows: list[SubtaskRowSnapshot] = []
        active_states = {"doing", "checking", "triaging"}
        for index, row in enumerate(rows):
            sub_rows.append(
                SubtaskRowSnapshot(
                    subtask_id=row["subtask_id"],
                    title=row["title"] or "",
                    state=row["state"],
                    retries=int(row["retries"] or 0),
                )
            )
            if row["state"] in active_states and active_idx < 0:
                active_idx = index
        return sub_rows, active_idx

    def _detail_agent_calls(self, c: sqlite3.Connection, task_id: str) -> list[str]:
        rows = c.execute(
            "SELECT ts, phase, cli, rc, duration_s, tokens_used "
            "FROM agent_calls WHERE task_id = ? ORDER BY ts DESC LIMIT 20",
            (task_id,),
        ).fetchall()
        return [
            "  {ts}  {phase:<18s}  {cli:<8s}  rc={rc}  dur={dur}  tokens={tok}".format(
                ts=_dt.datetime.fromtimestamp(row["ts"], tz=ZoneInfo("UTC"))
                .astimezone()
                .strftime("%H:%M:%S"),
                phase=row["phase"],
                cli=row["cli"],
                rc=row["rc"],
                dur=f"{row['duration_s']:.1f}s" if row["duration_s"] else "—",
                tok=row["tokens_used"] or "—",
            )
            for row in rows
        ]


# ----- helpers -----


def _detail_review_context(row: sqlite3.Row) -> tuple[int | None, int | None]:
    review_round: int | None = None
    review_threads_count: int | None = None
    try:
        rr = row["review_round"]
        review_round = int(rr) if rr is not None else None
    except (KeyError, IndexError, ValueError, TypeError):
        review_round = None
    try:
        blob = row["intervention_request"]
        if blob:
            parsed = json.loads(blob)
            threads = parsed.get("threads") if isinstance(parsed, dict) else None
            if isinstance(threads, list):
                review_threads_count = len(threads)
    except (KeyError, IndexError, json.JSONDecodeError, TypeError):
        review_threads_count = None
    return review_round, review_threads_count


def _detail_pre_pr_audit(row: sqlite3.Row) -> tuple[int | None, list[dict]]:
    try:
        blob = row["pre_pr_audit_summary"]
    except (KeyError, IndexError):
        blob = None
    if not blob:
        return None, []
    try:
        parsed = json.loads(blob)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, []
    if not isinstance(parsed, dict):
        return None, []
    cycle = int(parsed["cycle"]) if parsed.get("cycle") is not None else None
    stages = parsed.get("stages") or []
    if not isinstance(stages, list):
        return cycle, []
    return cycle, [stage for stage in stages if isinstance(stage, dict)]


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


def _runtime_max_parallel(cfg: Config) -> int:
    """Live max_parallel from the daemon's heartbeat, falling back to config.

    `qk daemon start --max-parallel N` overrides cfg in the daemon process
    only — the on-disk config still says whatever's in config.toml. The
    daemon writes its effective value into the heartbeat each tick; reading
    it here keeps the TUI honest about what the running daemon is using.
    Falls back to cfg.max_parallel when no heartbeat is present (daemon
    stopped, fresh workspace).
    """
    hb = read_heartbeat(cfg)
    if hb is not None:
        value = hb.get("max_parallel")
        if isinstance(value, int) and value > 0:
            return value
    return int(cfg.max_parallel)


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
