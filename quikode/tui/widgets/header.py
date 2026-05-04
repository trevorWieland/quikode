"""Workspace header — one-glance health of the current run."""

from __future__ import annotations

from dataclasses import dataclass

from textual.widgets import Static


@dataclass(frozen=True)
class HeaderSnapshot:
    """All inputs the header needs, computed once per tick by store_polls."""

    workspace_path: str
    stacking_strategy: str
    max_parallel: int
    cpu_per_task: int
    mem_per_task_gb: int
    in_flight: int
    awaiting: int
    blocked: int
    merged: int
    total_in_scope: int
    dag_ready_unseeded: int = 0  # ready per DAG but not yet in the store
    tokens_total: int | None = None
    orchestrator_running: bool = False
    heartbeat_age_s: float | None = None
    heartbeat_stale: bool = False


class WorkspaceHeader(Static):
    """Top bar showing workspace + counts + global config."""

    DEFAULT_CSS = ""  # styled via #header-bar id

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._last_snap: HeaderSnapshot | None = None

    def render_snapshot(self, snap: HeaderSnapshot) -> None:
        if snap == self._last_snap:
            return
        self._last_snap = snap
        progress = (
            f"{snap.merged}/{snap.total_in_scope}"
            f" ({snap.merged * 100 // snap.total_in_scope if snap.total_in_scope else 0}%)"
        )
        if snap.orchestrator_running:
            # Show heartbeat age when meaningfully old. Fresh heartbeats (<5s)
            # stay quiet — surfacing every tick would be noise. Stale (>cfg
            # threshold, default 30s) gets a yellow warning so the operator
            # can tell "alive but stuck" from "alive and ticking".
            age = snap.heartbeat_age_s
            if snap.heartbeat_stale:
                age_txt = f"{age:.0f}s ago" if age is not None else "missing"
                orch = f"[yellow]running · heartbeat {age_txt}[/]"
            elif age is not None and age > 5:
                orch = f"[green]running[/] · heartbeat {age:.0f}s ago"
            else:
                orch = "[green]running[/]"
        else:
            orch = "[dim]stopped[/]"
        tokens = f"{snap.tokens_total / 1_000_000:.1f}M" if snap.tokens_total else "—"
        line1 = (
            f"[b]quikode[/] · {snap.workspace_path} · stacking [b]{snap.stacking_strategy}[/]"
            f" · max-parallel [b]{snap.max_parallel}[/]"
            f" · cpu/mem [b]{snap.cpu_per_task}/{snap.mem_per_task_gb}GB[/] per task"
            f" · orchestrator {orch}"
        )
        ready_seg = (
            f" · ready in DAG [b cyan]{snap.dag_ready_unseeded}[/]" if snap.dag_ready_unseeded > 0 else ""
        )
        line2 = (
            f"in-flight [b]{snap.in_flight}[/] · awaiting [b green]{snap.awaiting}[/]"
            f" · blocked [b red]{snap.blocked}[/]"
            f"{ready_seg}"
            f" · merged [b]{progress}[/] · tokens this run [b]{tokens}[/]"
        )
        self.update(f"{line1}\n{line2}")

    def render_unloaded(self, message: str) -> None:
        """Used before the first poll completes or when no workspace is configured."""
        self.update(f"[b]quikode[/] · {message}")
