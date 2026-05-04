"""DAG viewer (TUI v1) — node-level graph + headline stats sidebar.

Pure-Python layout/render/stats are exposed for testability; the textual
`DAGScreen` is the composing shell.
"""

from __future__ import annotations

from .layout import Edge, columns, edges, ranks
from .render import Cell, Filter, ascii_canvas, critical_path_from
from .screen import DAGScreen
from .stats import HeadlineStats, compute_headline_stats

__all__ = [
    "Cell",
    "DAGScreen",
    "Edge",
    "Filter",
    "HeadlineStats",
    "ascii_canvas",
    "columns",
    "compute_headline_stats",
    "critical_path_from",
    "edges",
    "ranks",
]
