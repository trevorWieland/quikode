"""Activity feed — last N state transitions."""

from __future__ import annotations

from dataclasses import dataclass

from textual.widgets import RichLog


@dataclass(frozen=True)
class ActivityEntry:
    timestamp: str  # already-formatted "HH:MM:SS"
    task_id: str
    transition: str  # e.g. "doing → awaiting_merge"
    note: str = ""


class ActivityFeed(RichLog):
    """Append-only log surface; render_entries replaces contents on each tick."""

    DEFAULT_CSS = ""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._last_fp: tuple = ()

    def on_mount(self) -> None:
        self.wrap = False
        self.markup = True
        # Soft buffer cap (RichLog drops oldest when exceeded). Display is bounded
        # by the panel's proportional height; this just controls how far the
        # operator can scroll back. 1000 covers a multi-hour session without
        # losing early activity, while staying inexpensive to render.
        self.max_lines = 1000

    def render_entries(self, entries: list[ActivityEntry]) -> None:
        # Caller passes entries newest-first (matches the SQL ORDER BY ts DESC).
        # Write oldest-first so the bottom is the newest entry; RichLog
        # auto_scroll lands the viewport on the newest line (tail -f feel).
        # Skip the clear+rewrite when nothing changed to avoid 1s flicker.
        fp = tuple((e.timestamp, e.task_id, e.transition, e.note) for e in entries)
        if fp == getattr(self, "_last_fp", ()):
            return
        self._last_fp = fp
        self.clear()
        for e in reversed(entries):
            line = f"[dim]{e.timestamp}[/] [b]{e.task_id}[/] {e.transition}"
            if e.note:
                line += f" [dim]({e.note})[/]"
            self.write(line)
