"""Slash command dispatch. Maps `/verb [args...]` strings to actions on the App.

Design points:
- Read-mostly commands (show, ready, status, etc.) just adjust the TUI's view.
- Mutating commands (retry, abort, mark-merged) shell out to the same `quikode`
  CLI subcommand the user could have typed manually. This avoids duplicating
  the shutdown/cleanup logic and keeps the TUI a thin shell.
- Destructive commands (reset, abort) wrap their handler in a ConfirmModal.
- Errors are surfaced to the activity feed, not raised, so a typo never crashes
  the dashboard.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from quikode.config import AgentCli
from quikode.config_loader import load_config

from ..widgets.activity_feed import ActivityFeed
from ..widgets.confirm_modal import ConfirmModal
from ..widgets.settings_modal import SettingsModal
from ..widgets.tasks_table import TasksTable
from . import orchestrator_control
from .slash_catalog import SLASH_CATALOG

_AGENT_PHASES = {"planner", "doer", "checker", "triage", "conflict_resolver", "intent_reviewer"}

if TYPE_CHECKING:
    from ..app import QuikodeTUI


@dataclass(frozen=True)
class ParsedCommand:
    verb: str
    args: list[str]
    raw: str


def parse_slash(raw: str) -> ParsedCommand | None:
    """Parse `/verb arg1 arg2`. Returns None for non-slash input."""
    s = raw.strip()
    if not s.startswith("/"):
        return None
    body = s[1:]
    if not body:
        return None
    parts = shlex.split(body, posix=True)
    if not parts:
        return None
    return ParsedCommand(verb=parts[0], args=parts[1:], raw=raw)


# ---------- handlers ----------


def _selected_or_arg(app: QuikodeTUI, args: list[str]) -> str | None:
    """Pull the task id from the first arg, else fall back to the table selection."""
    if args:
        return args[0]
    table = app.query_one("#tasks-panel", TasksTable)
    return table.selected_task_id()


def _toast(app: QuikodeTUI, msg: str) -> None:
    app.query_one("#activity-panel", ActivityFeed).write(msg)


def _shell_out_async(app: QuikodeTUI, argv: list[str]) -> None:
    """Spawn `quikode <subcommand>` in the background; surface result asynchronously."""

    async def runner() -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            out, err = await proc.communicate()
            if proc.returncode == 0:
                summary = out.decode().strip().splitlines()[-1] if out else "ok"
                _toast(app, f"[green]✓[/] {' '.join(argv)} ({summary})")
            else:
                _toast(app, f"[red]✗[/] {' '.join(argv)}: {(err or out).decode().strip()[:200]}")
        except Exception as e:
            _toast(app, f"[red]error running {argv[0]}: {e}[/]")

    app.run_worker(runner(), exclusive=False)


def _quikode_argv() -> list[str]:
    """Locate the quikode CLI binary. Prefer explicit env so tests can intercept."""
    env_override = os.environ.get("QUIKODE_BIN")
    if env_override:
        return [env_override]
    return [sys.executable, "-m", "quikode.cli"]


def _confirm_then(app: QuikodeTUI, message: str, action: Callable[[], None]) -> None:
    def on_done(result: bool) -> None:
        if result:
            action()

    app.push_screen(ConfirmModal(message, on_done=on_done))


# ----- read-only commands -----


def _handle_show(app: QuikodeTUI, parsed: ParsedCommand) -> None:
    target = _selected_or_arg(app, parsed.args)
    if not target:
        _toast(app, "[yellow]/show needs a task id (or select a row)[/]")
        return
    app._selected_task_id = target
    app.refresh_now()
    _toast(app, f"[dim]→ {target}[/]")


def _handle_help(app: QuikodeTUI, _parsed: ParsedCommand) -> None:
    lines = ["[b]slash commands:[/]"]
    for name, desc in SLASH_CATALOG.items():
        lines.append(f"  [b]/{name}[/] [dim]— {desc}[/]")
    for ln in lines:
        _toast(app, ln)


def _handle_ready(app: QuikodeTUI, _parsed: ParsedCommand) -> None:
    """List DAG nodes whose dependencies are all merged. Shows seeded + unseeded
    so the user can see what /run would pick up next."""
    poller = app.poller
    if not poller._ensure_open():
        _toast(app, f"[red]workspace not configured: {poller._last_error}[/]")
        return
    dag = poller._load_dag_cached()
    if dag is None:
        _toast(app, "[red]could not load DAG[/]")
        return
    assert poller._conn is not None
    merged = {r["id"] for r in poller._conn.execute("SELECT id FROM tasks WHERE state = 'merged'").fetchall()}
    seeded = {r["id"] for r in poller._conn.execute("SELECT id FROM tasks").fetchall()}
    ready = [
        (nid, n)
        for nid, n in dag.nodes.items()
        if nid not in merged and all(d in merged for d in n.depends_on)
    ]
    if not ready:
        _toast(app, "[dim](nothing ready)[/]")
        return
    _toast(app, f"[b]DAG-ready ({len(ready)}):[/]")
    for nid, n in ready[:30]:
        marker = "[dim]·[/]" if nid in seeded else "[cyan]+[/]"
        _toast(app, f"  {marker} [b]{nid}[/]  {n.title[:80]}")
    if len(ready) > 30:
        _toast(app, f"  [dim]... and {len(ready) - 30} more[/]")


def _handle_keybindings(app: QuikodeTUI, _parsed: ParsedCommand) -> None:
    _toast(
        app,
        "[b]keybindings:[/] q=quit · /=command · ?=help · Tab=cycle detail · "
        "r=retry · a=abort · o=open PR · d=export · t=tail · v=plan · e=explain · m=mark-merged · .=refresh",
    )


def _handle_quit(app: QuikodeTUI, _parsed: ParsedCommand) -> None:
    app.action_quit_with_confirm()


# ----- per-task actions -----


def _handle_retry(app: QuikodeTUI, parsed: ParsedCommand) -> None:
    target = _selected_or_arg(app, parsed.args)
    if not target:
        _toast(app, "[yellow]/retry needs a task id[/]")
        return
    _confirm_then(
        app,
        f"Reset task {target} to PENDING?\n(this clears its worktree)",
        lambda: _shell_out_async(app, [*_quikode_argv(), "retry", target]),
    )


def _handle_abort(app: QuikodeTUI, parsed: ParsedCommand) -> None:
    target = _selected_or_arg(app, parsed.args)
    if not target:
        _toast(app, "[yellow]/abort needs a task id[/]")
        return
    _confirm_then(
        app,
        f"Abort task {target} (mark ABORTED + tear down container)?",
        lambda: _shell_out_async(app, [*_quikode_argv(), "abort", target]),
    )


def _handle_mark_merged(app: QuikodeTUI, parsed: ParsedCommand) -> None:
    target = _selected_or_arg(app, parsed.args)
    if not target:
        _toast(app, "[yellow]/mark-merged needs a task id[/]")
        return
    _confirm_then(
        app,
        f"Mark {target} as MERGED (manual override)?",
        lambda: _shell_out_async(app, [*_quikode_argv(), "mark-merged", target]),
    )


def _handle_open_pr(app: QuikodeTUI, parsed: ParsedCommand) -> None:
    target = _selected_or_arg(app, parsed.args)
    if not target:
        _toast(app, "[yellow]/open-pr needs a task id[/]")
        return
    poller = app.poller
    if not poller._ensure_open():
        _toast(app, "[red]workspace not configured[/]")
        return
    assert poller._conn is not None
    r = poller._conn.execute("SELECT pr_url FROM tasks WHERE id = ?", (target,)).fetchone()
    if not r or not r["pr_url"]:
        _toast(app, f"[yellow]no PR URL recorded for {target}[/]")
        return
    try:
        webbrowser.open(r["pr_url"])
        _toast(app, f"[green]opened[/] {r['pr_url']}")
    except OSError as e:
        _toast(app, f"[red]failed to open: {e}[/]")


def _handle_open_log(app: QuikodeTUI, parsed: ParsedCommand) -> None:
    target = _selected_or_arg(app, parsed.args)
    if not target:
        _toast(app, "[yellow]/open-log needs a task id[/]")
        return
    poller = app.poller
    if not poller._ensure_open():
        _toast(app, "[red]workspace not configured[/]")
        return
    assert poller._cfg is not None
    log_path: Path = poller._cfg.log_dir / f"{target}.log"
    if not log_path.exists():
        _toast(app, f"[yellow]no log file at {log_path}[/]")
        return
    editor = os.environ.get("EDITOR", "less")
    try:
        # Suspend textual to give the editor the terminal.
        with app.suspend():
            subprocess.run([editor, str(log_path)], check=False)
    except OSError as e:
        _toast(app, f"[red]editor failed: {e}[/]")


# ----- orchestrator control -----


def _handle_run(app: QuikodeTUI, parsed: ParsedCommand) -> None:
    try:
        s = orchestrator_control.spawn(app.workspace, extra_args=parsed.args)
    except FileExistsError as e:
        _toast(app, f"[yellow]{e}[/]")
        return
    except OSError as e:
        _toast(app, f"[red]/run failed: {e}[/]")
        return
    _toast(app, f"[green]✓[/] orchestrator spawned (pid {s.pid})")


def _handle_stop(app: QuikodeTUI, _parsed: ParsedCommand) -> None:
    s = orchestrator_control.status(app.workspace)
    if not s.running:
        _toast(app, "[dim]no orchestrator to stop[/]")
        return

    async def runner() -> None:
        ok = await asyncio.get_event_loop().run_in_executor(None, orchestrator_control.stop, app.workspace)
        if ok:
            _toast(app, "[green]✓[/] orchestrator stopped")
        else:
            _toast(app, "[yellow]graceful stop timed out — try /force-quit[/]")

    app.run_worker(runner(), exclusive=False)


def _handle_force_quit(app: QuikodeTUI, _parsed: ParsedCommand) -> None:
    s = orchestrator_control.status(app.workspace)
    if not s.running:
        _toast(app, "[dim]no orchestrator running[/]")
        return

    def do_force() -> None:
        if orchestrator_control.force_quit(app.workspace):
            _toast(app, "[red]✗[/] orchestrator force-killed (run /clean-containers to clean up)")
        else:
            _toast(app, "[red]force quit failed[/]")

    _confirm_then(
        app,
        "Force-kill the orchestrator?\nContainers will be stranded; you'll need /clean-containers after.",
        do_force,
    )


# ----- settings -----


def _handle_settings(app: QuikodeTUI, _parsed: ParsedCommand) -> None:
    try:
        cfg = load_config(app.workspace)
    except FileNotFoundError as e:
        _toast(app, f"[red]{e}[/]")
        return
    toml_path = app.workspace / ".quikode" / "config.toml"

    def on_apply(new_cfg) -> None:
        _toast(app, "[green]✓[/] config updated")

    def after_close(restart: bool | None) -> None:
        if restart:
            # Apply + Restart was clicked. Issue stop + run.
            s = orchestrator_control.status(app.workspace)
            if s.running:
                _toast(app, "[dim]stopping orchestrator before restart...[/]")

                async def runner() -> None:
                    if not await asyncio.get_event_loop().run_in_executor(
                        None, orchestrator_control.stop, app.workspace
                    ):
                        _toast(app, "[yellow]graceful stop timed out[/]")
                        return
                    try:
                        new_status = orchestrator_control.spawn(app.workspace)
                        _toast(app, f"[green]✓[/] orchestrator restarted (pid {new_status.pid})")
                    except (FileExistsError, OSError) as e:
                        _toast(app, f"[red]restart failed: {e}[/]")

                app.run_worker(runner(), exclusive=False)

    app.push_screen(SettingsModal(cfg, toml_path, on_apply=on_apply), after_close)


def _handle_config(app: QuikodeTUI, _parsed: ParsedCommand) -> None:
    """Open config.toml in $EDITOR."""
    toml_path = app.workspace / ".quikode" / "config.toml"
    if not toml_path.exists():
        _toast(app, f"[yellow]no config at {toml_path} — run `quikode init` first[/]")
        return
    editor = os.environ.get("EDITOR", "vi")
    try:
        with app.suspend():
            subprocess.run([editor, str(toml_path)], check=False)
    except OSError as e:
        _toast(app, f"[red]editor failed: {e}[/]")


# ----- agent assignment -----


def _handle_set_model(app: QuikodeTUI, parsed: ParsedCommand) -> None:
    """`/set-model <phase> <cli>:<model>` — change which CLI+model runs a phase.

    Examples:
      /set-model planner claude:claude-opus-4-7
      /set-model doer opencode:zai-coding-plan/glm-5.1
      /set-model triage claude:claude-sonnet-4-6
    """
    if len(parsed.args) != 2:
        _toast(
            app,
            f"[yellow]usage:[/] /set-model <phase> <cli>:<model>  phases: {', '.join(sorted(_AGENT_PHASES))}",
        )
        return
    phase, spec = parsed.args
    if phase not in _AGENT_PHASES:
        _toast(app, f"[red]unknown phase '{phase}'.[/] valid: {', '.join(sorted(_AGENT_PHASES))}")
        return
    if ":" not in spec:
        _toast(app, "[red]model spec must be `<cli>:<model>` (e.g. claude:claude-opus-4-7)[/]")
        return
    cli_name, model = spec.split(":", 1)
    if cli_name not in {c.value for c in AgentCli}:
        _toast(
            app,
            f"[red]unknown cli '{cli_name}'.[/] valid: {', '.join(c.value for c in AgentCli)}",
        )
        return
    if not model.strip():
        _toast(app, "[red]model id must not be empty[/]")
        return
    toml_path = app.workspace / ".quikode" / "config.toml"
    try:
        _set_agent_role_in_toml(toml_path, phase, cli_name, model)
    except OSError as e:
        _toast(app, f"[red]write failed: {e}[/]")
        return
    _toast(
        app,
        f"[green]✓[/] {phase} → {cli_name}:{model} "
        "(saved; in-flight tasks keep prior model — restart orchestrator to apply)",
    )


def _set_agent_role_in_toml(toml_path: Path, phase: str, cli_name: str, model: str) -> None:
    """Write or replace [agents.<phase>] in config.toml. Idempotent."""
    if not toml_path.exists():
        toml_path.parent.mkdir(parents=True, exist_ok=True)
        toml_path.write_text("# quikode config\n")
    text = toml_path.read_text()
    lines = text.splitlines()
    header = f"[agents.{phase}]"
    sec_start = next((i for i, ln in enumerate(lines) if ln.strip() == header), -1)
    new_block = [header, f'cli = "{cli_name}"', f'model = "{model}"']
    if sec_start < 0:
        sep = [""] if lines and lines[-1].strip() != "" else []
        lines.extend(sep + new_block)
    else:
        # Replace through end-of-section (next [header] or EOF)
        sec_end = next(
            (i for i in range(sec_start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
            len(lines),
        )
        lines = lines[:sec_start] + new_block + lines[sec_end:]
    toml_path.write_text("\n".join(lines) + "\n")


# ----- view controls (TUI-only) -----


def _handle_sort(app: QuikodeTUI, parsed: ParsedCommand) -> None:
    # Step 5 stub — full implementation lands when we add the in-memory filter
    # layer in step 8 polish.
    if not parsed.args:
        _toast(app, "[yellow]/sort needs a key (state|age|cost|retries)[/]")
        return
    _toast(app, f"[dim](sort by {parsed.args[0]} not yet wired — coming in step 8)[/]")


def _handle_filter(app: QuikodeTUI, parsed: ParsedCommand) -> None:
    if not parsed.args:
        _toast(app, "[yellow]/filter needs a state name[/]")
        return
    _toast(app, f"[dim](filter {parsed.args[0]} not yet wired — coming in step 8)[/]")


def _handle_clear_filter(app: QuikodeTUI, _parsed: ParsedCommand) -> None:
    _toast(app, "[dim](filter clear not yet wired)[/]")


# ----- registration -----


HANDLERS: dict[str, Callable[[QuikodeTUI, ParsedCommand], None]] = {
    "show": _handle_show,
    "help": _handle_help,
    "keybindings": _handle_keybindings,
    "ready": _handle_ready,
    "quit": _handle_quit,
    "retry": _handle_retry,
    "abort": _handle_abort,
    "mark-merged": _handle_mark_merged,
    "open-pr": _handle_open_pr,
    "open-log": _handle_open_log,
    "sort": _handle_sort,
    "filter": _handle_filter,
    "clear-filter": _handle_clear_filter,
    "run": _handle_run,
    "stop": _handle_stop,
    "force-quit": _handle_force_quit,
    "settings": _handle_settings,
    "config": _handle_config,
    "set-model": _handle_set_model,
}


def dispatch(app: QuikodeTUI, raw: str) -> None:
    """Top-level entrypoint called from the App's command bar submission."""
    parsed = parse_slash(raw)
    if parsed is None:
        if raw.strip() and not raw.startswith("/"):
            _toast(app, f"[yellow]free text reserved for chat (v2.5+):[/] {raw}")
        return
    handler = HANDLERS.get(parsed.verb)
    if handler is None:
        if parsed.verb in SLASH_CATALOG:
            _toast(app, f"[dim](/{parsed.verb} known but not yet implemented)[/]")
        else:
            _toast(app, f"[red]unknown command: /{parsed.verb}[/]")
        return
    try:
        handler(app, parsed)
    except Exception as e:
        _toast(app, f"[red]/{parsed.verb} failed: {e}[/]")
