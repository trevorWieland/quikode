"""Resources panel — host + per-container caps + live container stats."""

from __future__ import annotations

from dataclasses import dataclass

from textual.widgets import Static


@dataclass(frozen=True)
class ContainerSample:
    task_id: str
    cpu_pct: float | None
    rss_gb: float | None


@dataclass(frozen=True)
class ResourcesSnapshot:
    host_cpus: int | None
    host_mem_gb: int | None
    cpu_per_task: int
    mem_per_task_gb: int
    max_parallel: int
    containers: list[ContainerSample]


class ResourcesPanel(Static):
    DEFAULT_CSS = ""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._last_fp: tuple = ()

    def render_snapshot(self, snap: ResourcesSnapshot) -> None:
        fp = (
            snap.host_cpus,
            snap.host_mem_gb,
            snap.cpu_per_task,
            snap.mem_per_task_gb,
            snap.max_parallel,
            tuple((c.task_id, c.cpu_pct, c.rss_gb) for c in snap.containers),
        )
        if fp == self._last_fp:
            return
        self._last_fp = fp
        host_line = f"host: [b]{snap.host_cpus or '?'} cpus[/] · [b]{snap.host_mem_gb or '?'} GB[/]"
        cap_line = f"per-task cap: [b]{snap.cpu_per_task} cpus[/] · [b]{snap.mem_per_task_gb} GB[/]"
        parallel_line = f"max parallel: [b]{snap.max_parallel}[/]"
        lines = [host_line, cap_line, parallel_line, "", "[b]live containers:[/]"]
        if not snap.containers:
            lines.append("  [dim]no in-flight containers[/]")
        else:
            for c in snap.containers:
                cpu = f"{c.cpu_pct:.0f}%" if c.cpu_pct is not None else "—"
                rss = f"{c.rss_gb:.1f} GB" if c.rss_gb is not None else "—"
                lines.append(f"  [b]{c.task_id}[/]  cpu {cpu}  rss {rss}")
        self.update("\n".join(lines))
