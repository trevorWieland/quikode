"""Typer command group."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from quikode.state import SubtaskRow, TaskRow

from .cli_context import (
    DAG,
    Config,
    Path,
    Store,
    Table,
    _humanize_secs,
    _open_store,
    app,
    console,
    json,
    load_config,
    retry_classify,
    subprocess,
    time,
    typer,
)


@app.command()
def export(
    task_id: str,
    output: Path = typer.Option(
        None, "--output", "-o", help="File to write the bundle to (default: <task-id>.review.md)"
    ),
    include_diff: bool = typer.Option(True, "--diff/--no-diff"),
):
    """Bundle everything you'd want to review for a task into one markdown file:
    planner output, doer summary, checker verdict, triage notes, full git diff,
    PR link. For human review at end of run."""
    cfg = load_config()
    store = _open_store(cfg)
    dag = DAG.load(cfg.dag_path)
    row = store.get(task_id)
    if not row:
        console.print(f"[red]no such task: {task_id}[/]")
        raise typer.Exit(1)
    node = dag.nodes.get(task_id)
    out = output or Path(f"{task_id}.review.md")
    parts = _export_parts(cfg, store, task_id, row, node, include_diff=include_diff)
    out.write_text("\n".join(parts))
    console.print(f"[green]wrote {out}[/]  ({out.stat().st_size:,} bytes)")


def _latest_artifacts(store: Store, task_id: str) -> dict[str, str]:
    rows = store.conn.execute(
        "SELECT kind, content, ts FROM artifacts WHERE task_id = ? ORDER BY ts DESC",
        (task_id,),
    ).fetchall()
    latest: dict[str, str] = {}
    for r in rows:
        if r["kind"] not in latest:
            latest[str(r["kind"])] = str(r["content"] or "")
    return latest


def _export_parts(
    cfg: Config, store: Store, task_id: str, row: TaskRow, node, *, include_diff: bool
) -> list[str]:
    latest = _latest_artifacts(store, task_id)
    parts: list[str] = [f"# {task_id} review bundle\n"]
    if node:
        parts.extend(_export_node_summary(node, row))
    parts.extend(_export_state_timeline(store, task_id))
    parts.extend(_export_subtasks(store, task_id))
    parts.extend(_export_agent_artifacts(latest))
    if include_diff:
        parts.extend(_export_git_diff(cfg, row))
    return parts


def _export_node_summary(node, row: Mapping[str, Any]) -> list[str]:
    parts = [
        f"**Title:** {node.title}\n",
        f"**Milestone:** {node.milestone}\n",
        f"**Final state:** `{row['state']}`\n",
    ]
    if row.get("pr_url"):
        parts.append(f"**PR:** {row['pr_url']}\n")
    if row.get("branch"):
        parts.append(f"**Branch:** `{row['branch']}`\n")
    parts.extend(["", "## Scope\n", node.scope])
    if node.boundary_with_neighbors:
        parts.extend(["\n### Boundary with neighbors\n", node.boundary_with_neighbors])
    if node.expected_evidence:
        parts.extend(["\n## Expected evidence\n", *_expected_evidence_lines(node.expected_evidence)])
    return parts


def _expected_evidence_lines(items: list[dict]) -> list[str]:
    return [
        f"- **{ev.get('kind', '')}** for {ev.get('behavior_id', '')} on "
        f"{ev.get('interfaces', [])} - witnesses {ev.get('witnesses', [])}: {ev.get('description', '')}"
        for ev in items
    ]


def _export_state_timeline(store: Store, task_id: str) -> list[str]:
    log_rows = list(
        store.conn.execute(
            "SELECT from_state, to_state, note, ts FROM state_log WHERE task_id = ? ORDER BY ts",
            (task_id,),
        )
    )
    parts = ["\n## State timeline\n", "```"]
    for r in log_rows:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
        note = f"  ({r['note']})" if r["note"] else ""
        parts.append(f"  {ts}  {r['from_state'] or '-':>14} -> {r['to_state']}{note}")
    parts.append("```")
    return parts


def _export_subtasks(store: Store, task_id: str) -> list[str]:
    sub_rows = store.list_subtasks(task_id)
    if not sub_rows:
        return []
    parts = ["\n## Subtasks\n", "| ID | State | Retries | Title | Files |", "|---|---|---|---|---|"]
    for r in sub_rows:
        parts.append(_export_subtask_table_row(r))
    parts.append("\n### Subtask acceptance criteria\n")
    for r in sub_rows:
        parts.extend(_export_subtask_acceptance(r))
    return parts


def _export_subtask_table_row(r: SubtaskRow) -> str:
    files = json.loads(r["files_to_touch"] or "[]")
    files_short = ", ".join(f"`{f}`" for f in files[:4])
    if len(files) > 4:
        files_short += f" (+{len(files) - 4})"
    return f"| {r['subtask_id']} | {r['state']} | {r.get('retries') or 0} | {r.get('title') or ''} | {files_short} |"


def _export_subtask_acceptance(r: SubtaskRow) -> list[str]:
    parts = [f"\n**{r['subtask_id']}** - {r.get('title') or ''}"]
    parts.extend(f"- {c}" for c in json.loads(r["acceptance"] or "[]"))
    if r.get("triage_notes"):
        parts.append(f"\n_triage notes from last attempt:_\n```\n{str(r['triage_notes'])[:1000]}\n```")
    return parts


def _export_agent_artifacts(latest: dict[str, str]) -> list[str]:
    parts: list[str] = []
    sections = [
        ("planner_output", "\n## Plan (from planner agent)\n", None),
        ("doer_output", "\n## Doer summary\n", "tail"),
        ("checker_output", "\n## Checker verdict\n", "code"),
        ("triage_output", "\n## Latest triage notes\n", "code"),
    ]
    for key, title, mode in sections:
        if key in latest:
            parts.extend(_artifact_section(title, latest[key], mode))
    return parts


def _artifact_section(title: str, body: str, mode: str | None) -> list[str]:
    if mode == "tail":
        return [title, "```", body[-3000:], "```"]
    if mode == "code":
        return [title, "```", body, "```"]
    return [title, body]


def _export_git_diff(cfg: Config, row: Mapping[str, Any]) -> list[str]:
    if not row.get("worktree_path"):
        return []
    wt = Path(str(row["worktree_path"]))
    if not wt.exists():
        return []
    diff = subprocess.run(
        ["git", "diff", f"{cfg.base_branch}...HEAD"], cwd=wt, capture_output=True, text=True, check=False
    )
    stat = subprocess.run(
        ["git", "diff", "--stat", f"{cfg.base_branch}...HEAD"],
        cwd=wt,
        capture_output=True,
        text=True,
        check=False,
    )
    return [
        "\n## Git diff (full)\n",
        "```diff",
        diff.stdout[:200_000],
        "```",
        "\n## Files changed\n",
        "```",
        stat.stdout,
        "```",
    ]


@app.command()
def subtasks(
    task_id: str,
    output_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
):
    """List subtasks for a task: id, state, retries, files, acceptance bullets.
    Subtasks are a v2-Phase-0 concept emitted by the planner."""
    cfg = load_config()
    store = _open_store(cfg)
    rows = store.list_subtasks(task_id)
    if output_json:
        payload = {
            "task_id": task_id,
            "subtasks": [
                {
                    "id": r["subtask_id"],
                    "state": r["state"],
                    "retries": r.get("retries") or 0,
                    "title": r.get("title"),
                    "depends_on": json.loads(r["depends_on"] or "[]"),
                    "files_to_touch": json.loads(r["files_to_touch"] or "[]"),
                    "acceptance": json.loads(r["acceptance"] or "[]"),
                    "boundary": r.get("boundary"),
                    "last_error": r.get("last_error"),
                }
                for r in rows
            ],
        }
        print(json.dumps(payload, indent=2))
        return
    if not rows:
        console.print(
            f"[yellow]no subtasks recorded for {task_id} (task hasn't reached planning, or planner pre-dates v2)[/]"
        )
        return

    state_color = {
        "pending": "dim",
        "doing": "yellow",
        "checking": "cyan",
        "triaging": "yellow",
        "done": "green",
        "blocked": "red",
        "skipped": "dim",
    }
    table = Table(show_lines=False, expand=True, title=f"Subtasks for {task_id}")
    table.add_column("ID", no_wrap=True)
    table.add_column("State", no_wrap=True)
    table.add_column("Retries", justify="right", no_wrap=True)
    table.add_column("Title", overflow="fold")
    table.add_column("Files", overflow="fold")
    table.add_column("Note", overflow="fold")
    for r in rows:
        st = r["state"]
        c = state_color.get(st, "white")
        files = json.loads(r["files_to_touch"] or "[]")
        files_short = ", ".join(files[:3]) + (f" (+{len(files) - 3} more)" if len(files) > 3 else "")
        table.add_row(
            r["subtask_id"],
            f"[{c}]{st}[/]",
            str(r.get("retries") or 0),
            r.get("title") or "",
            files_short,
            (r.get("last_error") or "")[:80],
        )
    console.print(table)


@app.command()
def show(
    task_id: str,
    full: bool = typer.Option(False, "--full", help="Print full artifact bodies instead of truncating"),
):
    """Print task summary: state timeline, agent-call cost breakdown, and the
    latest planner / checker / triage artifacts."""
    cfg = load_config()
    store = _open_store(cfg)
    row = store.get(task_id)
    if not row:
        console.print(f"[red]no such task: {task_id}[/]")
        raise typer.Exit(1)
    _print_show_header(task_id, row)
    _print_show_timeline(store, task_id)
    _print_show_agent_calls(store, task_id)
    _print_show_subtasks(store, task_id)
    _print_show_progress_checks(store, task_id)
    _print_show_review_threads(store, task_id)
    _print_show_artifacts(store, task_id, full=full)


def _print_show_header(task_id: str, row: Mapping[str, Any]) -> None:
    console.print(f"[bold cyan]{task_id}[/] - state [yellow]{row['state']}[/]")
    for label, key in (("branch", "branch"), ("PR", "pr_url")):
        if row.get(key):
            console.print(f"  {label}: {row[key]}")
    if row.get("last_error"):
        console.print(f"  [red]last_error:[/] {row['last_error']}")
    console.print(f"  ci-triage retries: {row.get('ci_triage_retries') or 0}")


def _print_show_timeline(store: Store, task_id: str) -> None:
    log = list(
        store.conn.execute(
            "SELECT from_state, to_state, note, ts FROM state_log WHERE task_id = ? ORDER BY ts",
            (task_id,),
        )
    )
    if not log:
        return
    console.print("\n[bold]-- state timeline --[/]")
    prev_ts = None
    for r in log:
        ts_str = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
        dt = f" (+{int(r['ts'] - prev_ts)}s)" if prev_ts else ""
        note = f"  {r['note']}" if r["note"] else ""
        console.print(f"  {ts_str}{dt}  {r['from_state'] or '-':>14} -> [cyan]{r['to_state']}[/]{note}")
        prev_ts = r["ts"]


def _print_show_agent_calls(store: Store, task_id: str) -> None:
    calls = list(
        store.conn.execute(
            "SELECT phase, cli, model, rc, duration_s, tokens_used, ts "
            "FROM agent_calls WHERE task_id = ? ORDER BY ts",
            (task_id,),
        )
    )
    if not calls:
        return
    console.print("\n[bold]-- agent calls --[/]")
    table = _agent_calls_table(calls)
    console.print(table)
    total_tokens = sum(c["tokens_used"] or 0 for c in calls)
    total_secs = sum(c["duration_s"] or 0.0 for c in calls)
    suffix = " (codex only - others don't surface tokens in text mode)" if total_tokens else ""
    console.print(
        f"  total agent time: {_humanize_secs(total_secs)}, reported tokens: {total_tokens:,}{suffix}"
    )


def _agent_calls_table(calls: list[dict]) -> Table:
    table = Table(show_header=True, expand=True)
    for col in ("when", "phase", "cli/model", "rc", "duration", "tokens"):
        table.add_column(col, justify="right" if col in {"rc", "duration", "tokens"} else "left")
    for c in calls:
        table.add_row(
            time.strftime("%H:%M:%S", time.localtime(c["ts"])),
            c["phase"],
            f"{c['cli']} {c['model'] or ''}",
            str(c["rc"]) if c["rc"] is not None else "-",
            _humanize_secs(c["duration_s"]) if c["duration_s"] else "-",
            f"{c['tokens_used']:,}" if c["tokens_used"] else "-",
        )
    return table


def _print_show_subtasks(store: Store, task_id: str) -> None:
    sub_rows = store.list_subtasks(task_id)
    if not sub_rows:
        return
    sub_cost = _subtask_costs(store, task_id)
    console.print("\n[bold]-- subtasks --[/]")
    for r in sub_rows:
        _print_show_subtask_row(store, task_id, r, sub_cost)


def _subtask_costs(store: Store, task_id: str) -> dict[str, dict[str, float]]:
    rows = store.conn.execute(
        "SELECT subtask_id, COUNT(*) AS n, SUM(duration_s) AS dur, SUM(cost_usd) AS cost "
        "FROM agent_calls WHERE task_id = ? AND subtask_id IS NOT NULL GROUP BY subtask_id",
        (task_id,),
    )
    return {
        c["subtask_id"]: {"n": c["n"] or 0, "dur": c["dur"] or 0.0, "cost": c["cost"] or 0.0} for c in rows
    }


def _print_show_subtask_row(
    store: Store, task_id: str, row: SubtaskRow, sub_cost: dict[str, dict[str, float]]
) -> None:
    icon = {
        "done": "[green]✓[/]",
        "blocked": "[red]✗[/]",
        "skipped": "[dim]·[/]",
        "pending": "[dim]·[/]",
    }.get(row["state"], "[yellow]…[/]")
    retries = f" (retries={row['retries']})" if (row.get("retries") or 0) else ""
    console.print(
        f"  {icon} [cyan]{row['subtask_id']}[/]  {row['state']}{retries}  "
        f"{row.get('title') or ''}{_subtask_stats_text(sub_cost.get(row['subtask_id']))}"
    )
    _print_retry_reasons(store.retry_reasons(task_id, row["subtask_id"]))


def _subtask_stats_text(stats: dict[str, float] | None) -> str:
    if not stats or not stats["n"]:
        return ""
    parts = [f"{stats['n']} calls", _humanize_secs(stats["dur"])]
    if stats["cost"]:
        parts.append(f"${stats['cost']:.2f}")
    return f"  [dim]({', '.join(parts)})[/]"


def _print_retry_reasons(reasons: list[dict]) -> None:
    if not reasons:
        return
    hist = retry_classify.histogram(reasons)
    if hist:
        console.print(f"      [dim]retry causes: {retry_classify.format_histogram(hist)}[/]")
    last = reasons[-1]
    console.print(
        f"      [dim]most-recent: {last.get('category', '?')} - {(last.get('signature') or '')[:120]}[/]"
    )


def _print_show_progress_checks(store: Store, task_id: str) -> None:
    pc_rows = list(
        store.conn.execute(
            "SELECT subtask_id, ts, attempts_at_check, verdict, rationale "
            "FROM progress_checks WHERE task_id = ? ORDER BY ts ASC",
            (task_id,),
        )
    )
    if not pc_rows:
        return
    latest, counts = _progress_check_summary(pc_rows)
    console.print("\n[bold]-- progress checks --[/]")
    for sid, last in latest.items():
        _print_progress_check_row(sid, last, counts[sid])


def _progress_check_summary(rows: list[dict]) -> tuple[dict[str, dict], dict[str, dict[str, int]]]:
    latest: dict[str, dict] = {}
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        sid = row["subtask_id"]
        latest[sid] = dict(row)
        verdict = (row["verdict"] or "").lower()
        counts.setdefault(sid, {})
        counts[sid][verdict] = counts[sid].get(verdict, 0) + 1
    return latest, counts


def _print_progress_check_row(sid: str, last: dict, counts: dict[str, int]) -> None:
    verdict = (last["verdict"] or "").lower()
    color = {"flatlined": "red", "progressing": "green", "uncertain": "yellow"}.get(verdict, "white")
    tally = ", ".join(f"{v}={n}" for v, n in sorted(counts.items()))
    ts_str = time.strftime("%H:%M:%S", time.localtime(last["ts"]))
    rationale = (last.get("rationale") or "").strip().replace("\n", " ")
    if len(rationale) > 200:
        rationale = rationale[:200] + "…"
    console.print(
        f"  [cyan]{sid}[/]  {ts_str}  attempt={last['attempts_at_check']}  [{color}]{verdict}[/]  ({tally})"
    )
    if rationale:
        console.print(f"      [dim]{rationale}[/]")


def _print_show_review_threads(store: Store, task_id: str) -> None:
    rt_rows = list(
        store.conn.execute(
            "SELECT thread_id, is_resolved, addressed_in_commit_sha, last_comment_author, "
            "last_comment_is_bot, last_comment_ts, first_seen_ts "
            "FROM review_threads WHERE task_id = ? ORDER BY first_seen_ts",
            (task_id,),
        )
    )
    if not rt_rows:
        return
    by_commit, upstream_resolved, unresolved = _review_thread_groups(rt_rows)
    console.print("\n[bold]-- review threads --[/]")
    console.print(
        f"  total={len(rt_rows)}  [green]addressed={len(by_commit)}[/]  "
        f"[cyan]auto-resolved-upstream={len(upstream_resolved)}[/]  [red]unresolved={len(unresolved)}[/]"
    )
    _print_unresolved_threads(unresolved)
    if upstream_resolved:
        console.print(f"[dim]  {len(upstream_resolved)} thread(s) resolved by upstream tool.[/]")


def _review_thread_groups(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    by_commit: list[dict] = []
    upstream_resolved: list[dict] = []
    unresolved: list[dict] = []
    for row in rows:
        data = dict(row)
        if not data["is_resolved"]:
            unresolved.append(data)
        elif data.get("addressed_in_commit_sha"):
            by_commit.append(data)
        else:
            upstream_resolved.append(data)
    return by_commit, upstream_resolved, unresolved


def _print_unresolved_threads(unresolved: list[dict]) -> None:
    if not unresolved:
        return
    console.print("[red]  unresolved:[/]")
    for d in unresolved[:8]:
        ts = time.strftime("%H:%M:%S", time.localtime(d["last_comment_ts"] or 0))
        bot = " (bot)" if d.get("last_comment_is_bot") else ""
        console.print(f"    {d['thread_id']:30}  by {d.get('last_comment_author', '?')}{bot}  ts={ts}")


def _print_show_artifacts(store: Store, task_id: str, *, full: bool) -> None:
    artifacts = store.conn.execute(
        "SELECT kind, content, ts FROM artifacts WHERE task_id = ? ORDER BY ts DESC",
        (task_id,),
    ).fetchall()
    seen: set[str] = set()
    for a in artifacts:
        kind = a["kind"]
        if kind in seen:
            continue
        seen.add(kind)
        console.print(f"\n[bold]-- {kind} --[/]")
        body = a["content"] or ""
        truncated_suffix = ""
        if not full and len(body) > 4000:
            truncated_suffix = f"... ({len(a['content']) - 4000} more chars; pass --full)"
            body = body[:4000]
        console.print(body, markup=False, highlight=False)
        if truncated_suffix:
            console.print(f"\n[dim]{truncated_suffix}[/]")
