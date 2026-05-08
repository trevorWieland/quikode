"""Typer CLI entry point."""

from __future__ import annotations

from . import (
    cli_briefing_dev,
    cli_cache_stats,
    cli_core,
    cli_daemon,
    cli_lifecycle,
    cli_monitor,
    cli_reset_plan,
    cli_resources,
    cli_show_export,
    cli_standards,
    cli_workspace,
)
from .cli_context import _compute_max_parallel, app, docker_env, subprocess

_command_modules = (
    cli_briefing_dev,
    cli_cache_stats,
    cli_core,
    cli_daemon,
    cli_lifecycle,
    cli_monitor,
    cli_reset_plan,
    cli_resources,
    cli_show_export,
    cli_standards,
    cli_workspace,
)
_PATCH_EXPORTS = (subprocess, docker_env, _compute_max_parallel)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
