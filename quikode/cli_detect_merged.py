"""`qk detect-merged` — plan 56 operator-facing ancestry sweep.

Walks every task whose state is not already MERGED and checks whether
its task branch's tip is an ancestor of `origin/<base_branch>`. Dry-run
by default — prints a report (task_id, branch, ancestry yes/no,
would-action). `--apply` actually fires `enter_merged` for ancestry
matches, using the same FSM call as the worker's auto-detect path so
behavior is identical.

Useful for: (a) verifying behavior of the new auto-detect-merged code
path; (b) one-shot retroactive marking of already-integrated PRs
(release-batch workflow that closed PRs before the daemon was running
the plan-56 build); (c) auditing the auto-detect's decisions.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .ancestry import AncestryResult, branch_is_ancestor_of_main
from .cli_context import (
    State,
    Store,
    _open_store,
    app,
    console,
    fsm_runtime,
    load_config,
    subprocess,
    typer,
)

# Plan 56: walk every non-MERGED task whose state could plausibly carry
# a branch that has shipped via integration (i.e. anything past PR open).
# PENDING is excluded — no branch yet. MERGE_NODE_* are synthetic and
# never have a remote branch to ancestry-check.
_DETECT_MERGED_CANDIDATE_STATES: frozenset[str] = frozenset(
    {
        State.PROVISIONING.value,
        State.PLANNING.value,
        State.DOING_SUBTASK.value,
        State.CHECKING_SUBTASK.value,
        State.TRIAGING_SUBTASK.value,
        State.COMMITTING.value,
        State.PUSHING.value,
        State.LOCAL_CI_CHECKING.value,
        State.PRE_PR_AUDITING.value,
        State.FIXUP_PLANNING.value,
        State.PR_OPENING.value,
        State.PENDING_CI.value,
        State.AWAITING_REVIEW.value,
        State.ADDRESSING_FEEDBACK.value,
        State.REBASING_TO_MAIN.value,
        State.CONFLICT_RESOLVING.value,
        State.BLOCKED.value,
        State.FAILED.value,
        State.ABORTED.value,
    }
)


def _host_run_git(repo_path: Any, args: list[str]) -> tuple[int, str]:
    """`run_git` adapter for the CLI: shell out to host git in the source repo.

    Matches the `RunGit` signature defined in `quikode.ancestry`. Returns
    (rc, stdout+stderr) so the ancestry helper sees the same shape the
    worker's `_git_in_workspace` returns.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _check_task_ancestry(
    *,
    cfg: Any,
    row: Mapping[str, Any],
    fetch_first: bool,
) -> AncestryResult:
    """Run the ancestry check for one task row using host git.

    `fetch_first` is parameterized so the caller can fetch once per sweep
    (orchestration-level throttling) instead of once per task; the
    primitive itself accepts a `fetch_first=False` mode for that.
    """
    branch = row.get("branch")
    if not branch:
        return AncestryResult(
            is_ancestor=False,
            branch_tip="",
            reason="task has no recorded branch",
        )

    def _run_git(args: list[str]) -> tuple[int, str]:
        return _host_run_git(cfg.repo_path, args)

    return branch_is_ancestor_of_main(
        _run_git,
        branch_ref=str(branch),
        base_ref=f"{cfg.pr_remote}/{cfg.base_branch}",
        fetch_first=fetch_first,
        pr_remote=cfg.pr_remote,
        base_branch=cfg.base_branch,
    )


@app.command("detect-merged")
def detect_merged(
    apply: bool = typer.Option(
        False,
        "--apply",
        help=(
            "Fire enter_merged for tasks whose branch is an ancestor of "
            "origin/<base_branch>. Without this flag, the command is a "
            "read-only report."
        ),
    ),
    no_fetch: bool = typer.Option(
        False,
        "--no-fetch",
        help=(
            "Skip the initial `git fetch <remote> <base_branch>`. Use when "
            "you've already fetched in the current shell and want to avoid "
            "round-tripping the remote again."
        ),
    ),
) -> None:
    """Plan 56: report (and optionally apply) ancestry-based auto-merge detection.

    For each non-MERGED task, runs
    `git merge-base --is-ancestor <task_branch_tip> origin/<base_branch>`
    in the source repo. Tasks whose commits are already reachable from
    origin/<base_branch> would be auto-marked MERGED by the worker the
    next time their PR polls (or have been already, depending on the
    pattern's recency). This command lets the operator audit / one-shot
    catch up after enabling the auto-detect knob or after a release-
    batch push where the daemon was offline.

    Dry-run by default. With `--apply`, fires the same FSM call the
    worker uses (`fsm_runtime.mark_merged`) for every ancestry-match.
    """
    cfg = load_config()
    if not cfg.auto_detect_merged_via_ancestry and apply:
        console.print(
            "[yellow]warning: cfg.auto_detect_merged_via_ancestry is False; "
            "running --apply anyway since you explicitly asked.[/]"
        )
    store = _open_store(cfg)
    try:
        _run_detect_merged_sweep(cfg=cfg, store=store, apply=apply, no_fetch=no_fetch)
    finally:
        store.conn.close()


def _run_detect_merged_sweep(
    *,
    cfg: Any,
    store: Store,
    apply: bool,
    no_fetch: bool,
) -> None:
    rows = store.all_tasks()
    targets: list[Mapping[str, Any]] = [r for r in rows if r["state"] in _DETECT_MERGED_CANDIDATE_STATES]
    if not targets:
        console.print("[yellow]no non-MERGED tasks found; nothing to check[/]")
        return
    # One fetch up-front (orchestration-level throttling — not per task).
    # The ancestry primitive then runs with fetch_first=False so we don't
    # re-fetch on every iteration. Skipped entirely with --no-fetch.
    if not no_fetch:
        console.print(f"[dim]→ git fetch {cfg.pr_remote} {cfg.base_branch}[/]")
        rc, out = _host_run_git(cfg.repo_path, ["fetch", cfg.pr_remote, cfg.base_branch])
        if rc != 0:
            console.print(
                f"[red]fetch failed (rc={rc}); continuing against the local view of "
                f"{cfg.pr_remote}/{cfg.base_branch}:[/]\n{out.strip()[:300]}"
            )

    ancestor_hits: list[tuple[Mapping[str, Any], AncestryResult]] = []
    misses: list[tuple[Mapping[str, Any], AncestryResult]] = []
    for row in targets:
        result = _check_task_ancestry(cfg=cfg, row=row, fetch_first=False)
        if result.is_ancestor:
            ancestor_hits.append((row, result))
        else:
            misses.append((row, result))

    _print_report(targets, ancestor_hits, misses, apply=apply)

    if not apply:
        return
    for row, result in ancestor_hits:
        _apply_one(store, row, result, cfg=cfg)


def _print_report(
    targets: list[Mapping[str, Any]],
    ancestor_hits: list[tuple[Mapping[str, Any], AncestryResult]],
    misses: list[tuple[Mapping[str, Any], AncestryResult]],
    *,
    apply: bool,
) -> None:
    console.print(
        f"[bold]detect-merged sweep:[/] checked {len(targets)} task(s); "
        f"[green]{len(ancestor_hits)}[/] ancestor-match · "
        f"[dim]{len(misses)}[/] not-an-ancestor"
    )
    for row, result in ancestor_hits:
        action = "[green]→ MARK MERGED[/]" if apply else "[cyan]would mark MERGED (dry-run)[/]"
        console.print(
            f"  [green]✓[/] {row['id']} ({row['state']})  "
            f"branch={row.get('branch') or '?'}  tip={result.branch_tip[:12]}  {action}"
        )
    for row, result in misses:
        console.print(
            f"  [dim]·[/] {row['id']} ({row['state']})  "
            f"branch={row.get('branch') or '?'}  reason={result.reason}"
        )


def _apply_one(
    store: Store,
    row: Mapping[str, Any],
    result: AncestryResult,
    *,
    cfg: Any,
) -> None:
    """Fire `fsm_runtime.mark_merged` with the same note shape the worker uses."""
    note = (
        f"auto-merged via ancestry (qk detect-merged --apply): PR closed without "
        f"GH merge, but {result.branch_tip[:12]} is reachable from "
        f"{cfg.pr_remote}/{cfg.base_branch} (release-batch integration pattern)"
    )
    try:
        fsm_runtime.mark_merged(store, row["id"], note=note)
        console.print(f"  [green]✓ marked {row['id']} MERGED[/]")
    except Exception as exc:
        # Surface the failure but keep going — one bad row shouldn't sink the sweep.
        console.print(f"  [red]failed to mark {row['id']} merged:[/] {exc}")
