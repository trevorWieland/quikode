"""quikode TUI — mission control dashboard for the orchestrator.

Design: see `docs/design-tui.md`. The TUI is a pure read-mostly view of the
SQLite store with a slash-command bar that dispatches to the same core
functions the CLI uses. The orchestrator runs as a managed subprocess so it
survives TUI restarts.

Public entry point is `run_tui(config_root)` — wired up by `quikode.cli` as
the `quikode tui` subcommand.
"""

from __future__ import annotations

from .app import QuikodeTUI, run_tui

__all__ = ["QuikodeTUI", "run_tui"]
