"""Typer command group."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .cli_context import (
    DAG,
    Config,
    Path,
    State,
    Store,
    _dir_size,
    _humanize_bytes,
    _humanize_secs,
    _last_state_change,
    _open_store,
    _worktree_age_seconds,
    _worktree_mtime,
    app,
    console,
    docker_env,
    find_config_root,
    json,
    load_config,
    run_tui,
    subprocess,
    sys,
    time,
    typer,
)


@app.command()
def briefing(
    output_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of human-readable output"
    ),
):
    """One-shot snapshot for waking up to: in-flight states, recent transitions,
    cost so far, dag progress, disk, warnings. Designed for `quikode briefing` to be
    the first command you run in the morning."""
    cfg = load_config()
    store = _open_store(cfg)
    dag = DAG.load(cfg.dag_path)
    rows = store.all_tasks()
    now = time.time()
    if output_json:
        print(json.dumps(_briefing_json_payload(cfg, store, dag, rows, now), indent=2, default=str))
        return

    console.rule("[bold cyan]quikode briefing[/]")
    active_states = (
        State.PROVISIONING,
        State.PLANNING,
        State.DOING_SUBTASK,
        State.CHECKING_SUBTASK,
        State.TRIAGING_SUBTASK,
        State.COMMITTING,
        State.PUSHING,
        State.PR_OPENING,
    )
    actives = store.in_state(*active_states)
    post_pr_groups = _post_pr_groups(store)
    blocked = list(post_pr_groups["blocked"]) + list(post_pr_groups["failed"])

    _print_state_summary(rows)
    _print_active_tasks(store, actives, now)
    _print_post_pr_groups(store, post_pr_groups)
    _print_recent_transitions(store)
    merged_rows = _print_recent_merges(store, rows)
    _print_agent_cost(store, merged_rows)
    total = len(dag.nodes)
    merged = sum(1 for r in rows if r["state"] == State.MERGED.value)
    awaiting_count = len(
        post_pr_groups["pending_ci"] + post_pr_groups["awaiting_review"] + post_pr_groups["merge_ready"]
    )
    pct = (100 * merged // total) if total else 0
    console.print(
        f"\n[bold]DAG[/]  {merged}/{total} merged ({pct}%)  +{awaiting_count} awaiting merge  +{len(actives)} active  +{len(blocked)} blocked"
    )

    # 7. Disk
    sccache = _dir_size(cfg.sccache_dir)
    worktrees = _dir_size(cfg.worktree_root)
    logs = _dir_size(cfg.log_dir)
    console.print(
        f"\n[bold]Disk[/]  sccache={_humanize_bytes(sccache)}  worktrees={_humanize_bytes(worktrees)}  logs={_humanize_bytes(logs)}"
    )

    _print_warnings(cfg, actives, now)
    _print_briefing_hints()


def _briefing_active_states() -> tuple[State, ...]:
    return (
        State.PROVISIONING,
        State.PLANNING,
        State.DOING_SUBTASK,
        State.CHECKING_SUBTASK,
        State.TRIAGING_SUBTASK,
        State.COMMITTING,
        State.PUSHING,
        State.PR_OPENING,
        State.PENDING_CI,
        State.REBASING_TO_MAIN,
        State.CONFLICT_RESOLVING,
        State.TRIAGING_FEEDBACK,
        State.FIXUP_PLANNING,
    )


def _briefing_row_summary(r: Mapping[str, Any]) -> dict:
    return {
        "id": r["id"],
        "state": r["state"],
        "pr_number": r.get("pr_number"),
        "pr_url": r.get("pr_url"),
        "branch": r.get("branch"),
        "review_round": r.get("review_round"),
        "last_error": r.get("last_error"),
    }


def _briefing_json_payload(
    cfg: Config, store: Store, dag: DAG, rows: Sequence[Mapping[str, Any]], now: float
) -> dict:
    actives = store.in_state(*_briefing_active_states())
    cost_rows = list(
        store.conn.execute(
            "SELECT cli, COUNT(*) AS n, SUM(duration_s) AS total_s, SUM(tokens_used) AS total_tok "
            "FROM agent_calls GROUP BY cli ORDER BY cli"
        )
    )
    return {
        "tasks_by_state": {s: sum(1 for r in rows if r["state"] == s) for s in {r["state"] for r in rows}},
        "pending_ci": [_briefing_row_summary(r) for r in rows if r["state"] == State.PENDING_CI.value],
        "awaiting_review": [
            _briefing_row_summary(r) for r in rows if r["state"] == State.AWAITING_REVIEW.value
        ],
        "merge_ready": [_briefing_row_summary(r) for r in rows if r["state"] == State.MERGE_READY.value],
        "triaging_feedback": [
            _briefing_row_summary(r) for r in rows if r["state"] == State.TRIAGING_FEEDBACK.value
        ],
        "addressing_feedback": [
            _briefing_row_summary(r) for r in rows if r["state"] == State.ADDRESSING_FEEDBACK.value
        ],
        "rebasing_to_main": [
            _briefing_row_summary(r) for r in rows if r["state"] == State.REBASING_TO_MAIN.value
        ],
        "blocked_needs_intervention": [
            _briefing_row_summary(r) for r in rows if r["state"] == State.BLOCKED.value
        ],
        "in_flight": [_briefing_in_flight_row(store, r, now) for r in actives],
        "agent_cost": {
            r["cli"]: {"calls": r["n"], "total_seconds": r["total_s"], "total_tokens": r["total_tok"]}
            for r in cost_rows
        },
        "dag": {
            "total_nodes": len(dag.nodes),
            "merged": sum(1 for r in rows if r["state"] == State.MERGED.value),
        },
        "disk": {
            "sccache_bytes": _dir_size(cfg.sccache_dir),
            "worktrees_bytes": _dir_size(cfg.worktree_root),
            "logs_bytes": _dir_size(cfg.log_dir),
        },
    }


def _briefing_in_flight_row(store: Store, r: Mapping[str, Any], now: float) -> dict:
    return {
        "id": r["id"],
        "state": r["state"],
        "in_state_seconds": (now - (_last_state_change(store, r["id"]) or now)),
        "worktree_mtime_age_seconds": _worktree_age_seconds(now, r.get("worktree_path")),
        "max_rss_bytes": store.task_max_rss(r["id"]),
        "pr_number": r.get("pr_number"),
    }


def _post_pr_groups(store: Store) -> dict[str, list]:
    return {
        "pending_ci": store.in_state(State.PENDING_CI),
        "awaiting_review": store.in_state(State.AWAITING_REVIEW),
        "merge_ready": store.in_state(State.MERGE_READY),
        "triaging_feedback": store.in_state(State.TRIAGING_FEEDBACK),
        "addressing_feedback": store.in_state(State.ADDRESSING_FEEDBACK),
        "rebasing": store.in_state(State.REBASING_TO_MAIN),
        "blocked": store.in_state(State.BLOCKED),
        "failed": store.in_state(State.FAILED),
    }


def _print_state_summary(rows: Sequence[Mapping[str, Any]]) -> None:
    by_state: dict[str, int] = {}
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    if not by_state:
        console.print("\n[dim]No tasks recorded yet.[/]")
        return
    console.print("\n[bold]Task states[/]")
    for s, n in sorted(by_state.items()):
        console.print(f"  {s}: {n}")


def _print_active_tasks(store: Store, actives: Sequence[Mapping[str, Any]], now: float) -> None:
    if not actives:
        return
    console.print("\n[bold]In-flight[/]")
    for r in actives:
        last = _last_state_change(store, r["id"])
        wt_mt = _worktree_mtime(Path(str(r["worktree_path"]))) if r.get("worktree_path") else None
        mx = store.task_max_rss(r["id"])
        cost = store.task_total_cost_usd(r["id"])
        console.print(
            f"  [cyan]{r['id']}[/] [{r['state']}] "
            f"in-state {_humanize_secs((now - last) if last else None)}; "
            f"worktree edit {_humanize_secs((now - wt_mt) if wt_mt else None)} ago"
            + (f"  pr#{r['pr_number']}" if r.get("pr_number") else "")
            + (f"  max_rss={mx / (1024**3):.1f}GB" if mx else "")
            + (f"  · ${cost:.2f}" if cost else "")
        )


def _print_post_pr_groups(store: Store, groups: dict[str, list]) -> None:
    _print_group(store, "Merge ready", "bright_green", groups["merge_ready"])
    _print_group(store, "Awaiting review (CI green)", "blue", groups["awaiting_review"])
    _print_group(store, "Pending CI", "yellow", groups["pending_ci"])
    _print_group(store, "Triaging feedback (Python)", "cyan", groups["triaging_feedback"])
    _print_group(
        store, "Addressing feedback", "cyan", groups["addressing_feedback"], suffix_fn=_review_round_suffix
    )
    _print_rebasing_group(groups["rebasing"])
    _print_terminal_group("Blocked — needs intervention", groups["blocked"], unblock=True)
    _print_terminal_group("Failed", groups["failed"], unblock=False)


def _print_group(store: Store, label: str, color: str, rows: list, *, suffix_fn=None) -> None:
    if not rows:
        return
    console.print(f"\n[bold {color}]{label}[/]")
    for r in rows:
        cost = store.task_total_cost_usd(r["id"])
        extra = suffix_fn(r) if suffix_fn else ""
        console.print(
            f"  [cyan]{r['id']}[/]{extra}  PR: {r.get('pr_url') or '(local only)'}"
            + (f"  · ${cost:.2f}" if cost else "")
        )


def _review_round_suffix(r: Mapping[str, Any]) -> str:
    rr = r.get("review_round")
    return f" round {rr}" if rr else ""


def _print_rebasing_group(rows: list) -> None:
    if not rows:
        return
    console.print("\n[bold yellow]Rebasing onto main[/]")
    for r in rows:
        console.print(f"  [cyan]{r['id']}[/]  branch: {r.get('branch') or '(unknown)'}")


def _print_terminal_group(label: str, rows: list, *, unblock: bool) -> None:
    if not rows:
        return
    console.print(f"\n[bold red]{label}[/]")
    for r in rows:
        note = (r.get("last_error") or "")[:120]
        suffix = f"\n    [dim]-> quikode unblock {r['id']}[/]" if unblock else ""
        console.print(f"  [cyan]{r['id']}[/] {note}{suffix}")


def _print_recent_transitions(store: Store) -> None:
    recent = list(
        store.conn.execute(
            "SELECT task_id, from_state, to_state, ts, note FROM state_log ORDER BY ts DESC LIMIT 20",
        )
    )
    if not recent:
        return
    console.print("\n[bold]Recent transitions[/]")
    for r in recent[:20]:
        ts_str = time.strftime("%m-%d %H:%M:%S", time.localtime(r["ts"]))
        note = f"  [dim]{r['note']}[/]" if r["note"] else ""
        console.print(
            f"  {ts_str}  {r['task_id']}  {r['from_state'] or '-'} -> [cyan]{r['to_state']}[/]{note}"
        )


def _print_recent_merges(store: Store, rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    merged_rows = [r for r in rows if r["state"] == State.MERGED.value]
    merge_ts = [_merge_timestamp_row(store, r) for r in merged_rows]
    merge_ts.sort(key=lambda t: t[1], reverse=True)
    if merge_ts:
        console.print("\n[bold]Recent merges[/]")
        for tid, ts, cost in merge_ts[:10]:
            ts_str = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"
            console.print(f"  {ts_str}  [cyan]{tid}[/]" + (f"  · ${cost:.2f}" if cost else ""))
    return merged_rows


def _merge_timestamp_row(store: Store, row: Mapping[str, Any]) -> tuple[str, float, float | None]:
    ts_row = store.conn.execute(
        "SELECT ts FROM state_log WHERE task_id = ? AND to_state = ? ORDER BY ts DESC LIMIT 1",
        (row["id"], State.MERGED.value),
    ).fetchone()
    return (row["id"], float(ts_row["ts"]) if ts_row else 0.0, store.task_total_cost_usd(row["id"]))


def _print_agent_cost(store: Store, merged_rows: Sequence[Mapping[str, Any]]) -> None:
    cost_rows = list(
        store.conn.execute(
            "SELECT cli, COUNT(*) AS n, SUM(duration_s) AS total_s, SUM(tokens_used) AS total_tok, "
            "SUM(cost_usd) AS total_cost FROM agent_calls GROUP BY cli ORDER BY cli"
        )
    )
    if not cost_rows:
        return
    console.print("\n[bold]Agent cost (this workspace)[/]")
    for r in cost_rows:
        cost_str = f", ${r['total_cost']:.2f}" if r["total_cost"] else ""
        console.print(
            f"  {r['cli']}: {r['n']} calls, {_humanize_secs(r['total_s'])} total, "
            f"{r['total_tok'] or 0:,} tokens{cost_str}"
        )
    total_cost = store.workspace_total_cost_usd()
    if total_cost:
        avg = (total_cost / len(merged_rows)) if merged_rows else None
        console.print(
            f"  [bold]total: ${total_cost:.2f}[/]" + (f"  (avg ${avg:.2f}/merged task)" if avg else "")
        )


def _print_warnings(cfg: Config, actives: Sequence[Mapping[str, Any]], now: float) -> None:
    warnings: list[str] = []
    for r in actives:
        if r["state"] == State.DOING_SUBTASK.value:
            wt_mt = _worktree_mtime(Path(str(r["worktree_path"]))) if r.get("worktree_path") else None
            if wt_mt and (now - wt_mt) > cfg.stall_warn_seconds:
                warnings.append(f"{r['id']} doer worktree quiet for {int((now - wt_mt) // 60)} min")
    active_slugs = {docker_env.slugify(r["id"]) for r in actives}
    warnings.extend(_orphan_container_warnings(cfg, active_slugs))
    if warnings:
        console.print("\n[bold yellow]Warnings[/]")
        for w in warnings:
            console.print(f"  · {w}")
    else:
        console.print("\n[green]No warnings.[/]")


def _orphan_container_warnings(cfg: Config, active_slugs: set[str]) -> list[str]:
    warnings: list[str] = []
    for c in docker_env.list_quikode_containers(label=docker_env.workspace_label(cfg)):
        name = c["name"]
        if not name.startswith("qk-"):
            continue
        inner = name[3:]
        for suffix in ("-dev", "-pg"):
            if inner.endswith(suffix):
                inner = inner[: -len(suffix)]
                break
        parts = inner.rsplit("-", 1)
        slug = parts[0] if len(parts) == 2 else inner
        if slug not in active_slugs:
            warnings.append(f"orphan container: {name}")
    return warnings


def _print_briefing_hints() -> None:
    console.print("\n[dim]Hints:[/]")
    console.print("  [dim]quikode show <id>      — full state + artifacts[/]")
    console.print("  [dim]quikode export <id>    — bundle for review[/]")
    console.print("  [dim]quikode dag-stats      — per-milestone breakdown[/]")
    console.print("  [dim]quikode watch          — live-updating table[/]")


@app.command("dev-test")
def dev_test(
    fixture_root: Path = typer.Option(
        Path("/home/trevor/github/quikode-runs/fixture"),
        "--root",
        help="Workspace root for the fixture run",
    ),
    timeout_min: int = typer.Option(
        15, "--timeout-min", help="Fail if T-001 doesn't reach pending_ci within N min"
    ),
):
    """End-to-end smoke test against the fastapi fixture. Exits 0 on success.

    Use this to validate quikode changes without burning a tanren cycle. Assumes
    the fixture workspace is already initialized (`quikode init` was run there
    pointing at quikode-fixture)."""
    if not (fixture_root / ".quikode" / "config.toml").exists():
        console.print(f"[red]no fixture config at {fixture_root}/.quikode/config.toml[/]")
        console.print(
            "Run: quikode init --repo /home/trevor/github/quikode-fixture --dag /home/trevor/github/quikode-fixture/dag.json (in --root)"
        )
        raise typer.Exit(2)
    # Reset
    r = subprocess.run(
        [sys.argv[0], "reset", "--yes", "--close-prs"],
        cwd=fixture_root,
        capture_output=True,
        text=True,
    )
    console.print(r.stdout)
    if r.returncode != 0:
        console.print(f"[red]reset failed: {r.stderr}[/]")
        raise typer.Exit(2)
    # Launch run in background
    log_file = fixture_root / ".quikode" / "dev-test.log"
    proc = subprocess.Popen(
        [sys.argv[0], "run", "--max-parallel", "1", "--log-level", "INFO"],
        cwd=fixture_root,
        stdout=log_file.open("w"),
        stderr=subprocess.STDOUT,
    )
    console.print(f"[cyan]started run pid {proc.pid}; tailing → {log_file}[/]")
    deadline = time.time() + timeout_min * 60
    cfg = load_config(root=fixture_root)
    store = _open_store(cfg)
    while time.time() < deadline:
        time.sleep(15)
        rows = store.all_tasks()
        states = {r["id"]: r["state"] for r in rows}
        elapsed = int(time.time() - (deadline - timeout_min * 60))
        console.print(
            f"  [{elapsed // 60:02d}:{elapsed % 60:02d}] " + " ".join(f"{i}={s}" for i, s in states.items())
        )
        if any(s in (State.PENDING_CI.value, State.MERGED.value) for s in states.values()):
            proc.terminate()
            proc.wait(timeout=5)
            console.print("[bold green]PASS[/] — fixture run reached pending_ci")
            return
        if (
            all(s in (State.BLOCKED.value, State.FAILED.value, State.ABORTED.value) for s in states.values())
            and states
        ):
            proc.terminate()
            console.print("[bold red]FAIL[/] — fixture run terminated unsuccessfully")
            console.print(f"see {log_file} and `quikode show <id>` for details")
            raise typer.Exit(1)
    proc.terminate()
    console.print(f"[bold red]TIMEOUT[/] — fixture run didn't finish in {timeout_min} min")
    raise typer.Exit(1)


@app.command()
def tui():
    """Launch the mission-control TUI (live dashboard for the orchestrator)."""
    cfg_root = find_config_root()
    run_tui(workspace=cfg_root)
