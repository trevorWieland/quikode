"""Catalog of slash commands available in the TUI.

Source of truth — fed to the command-bar autocomplete and to the help modal.
Keep entries terse; descriptions render inline as suggestions.

Commands marked with TUI-ONLY exist only inside the TUI (they don't have a
CLI equivalent — they manipulate the TUI's local view, like /sort or
/open-pr that shells out to xdg-open).
"""

from __future__ import annotations

from quikode.tui.widgets.command_bar import CommandSuggestionList

# Ordered for predictability — tests rely on this ordering.
SLASH_CATALOG: dict[str, str] = {
    # ----- run control -----
    "run": "start orchestrator (background subprocess)",
    "stop": "graceful stop of orchestrator",
    "force-quit": "kill orchestrator subprocess (containers stranded)",
    # ----- read-only (mirror CLI) -----
    "status": "focus tasks panel",
    "watch": "alias of /status",
    "show": "drill in to task <id>",
    "explain": "why is <id> blocked? who depends on it?",
    "ready": "list tasks with all deps merged",
    "dag-stats": "per-group breakdown",
    "briefing": "wake-up snapshot",
    "subtasks": "show subtask list for <id>",
    "resources": "host + container stats",
    "disk-usage": "what quikode is using",
    "doctor": "preflight checks",
    # ----- per-task actions -----
    "retry": "reset BLOCKED/FAILED <id> to PENDING",
    "abort": "mark <id> ABORTED + tear down container",
    "mark-merged": "manually mark <id> MERGED",
    "export": "bundle plan + verdict + diff for <id>",
    "tail": "tail <id> log",
    "logs": "print log path for <id>",
    "open-pr": "xdg-open the PR for <id> (TUI-only)",
    "open-log": "$EDITOR the log for <id> (TUI-only)",
    "open-worktree": "$SHELL into the worktree for <id> (TUI-only)",
    # ----- maintenance -----
    "reset": "tear down state (destructive — confirm modal)",
    "prune": "trim sccache + worktrees of done tasks",
    "clean-containers": "remove stranded qk-* containers",
    "build-image": "build dev image (--flavor)",
    "dev-test": "run fixture self-test",
    # ----- TUI-only view controls -----
    "sort": "re-sort tasks (state|age|cost|retries)",
    "filter": "show only tasks in <state>(s)",
    "clear-filter": "show all tasks",
    "config": "open .quikode/config.toml in $EDITOR",
    "settings": "open the schema-driven settings modal",
    "set-model": "<phase> <cli>:<model> — change agent assignment",
    "set-retry-budget": "<n> — change triage_budget_per_phase",
    "set-max-parallel": "<n>",
    "set-stacking": "off|within-milestone|aggressive",
    "help": "help modal",
    "keybindings": "show keymap",
    "quit": "exit (= q)",
}


def filter_catalog(query: str) -> list[tuple[str, str]]:
    """Return ranked (cmd, desc) for a query (with or without leading /)."""
    return CommandSuggestionList(SLASH_CATALOG).filter(query)
