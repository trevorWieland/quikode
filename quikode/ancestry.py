"""Plan 56: ancestry-based merged-via-foundation detection.

A task's PR can land in main without GitHub's "Merge" button being
clicked — the operator's release-batch workflow pulls N AWAITING_REVIEW
PRs into a local integration branch, pushes that branch to main, then
closes each constituent PR with `gh pr close`. GitHub reports each PR
as `state=CLOSED, merged=false` even though every commit IS in main.

This module defines a small primitive — `branch_is_ancestor_of_main` —
that wraps `git merge-base --is-ancestor <tip> origin/<base>` so the
worker-side PR poll handler and the operator-facing `qk detect-merged`
CLI share identical semantics. Both call sites pass their own
`run_git` callable: the worker runs git inside the per-task container
via `_git_in_workspace`; the CLI shells out to host git via
`subprocess.run`. The shape of each callable is intentionally narrow
so neither layer leaks transport details into the other.

`maybe_auto_merge_via_ancestry` is the worker-facing convenience that
wraps the primitive with the cfg-flag check, branch-missing guard,
note construction, and `fsm_runtime.mark_merged` dispatch. Kept here
(not on the mixin) so `pr_lifecycle.py` stays under the 600-line
architecture budget.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from . import fsm_runtime

log = logging.getLogger("quikode.ancestry")

RunGit = Callable[[list[str]], tuple[int, str]]
"""Callable that runs ``git <args>`` and returns ``(returncode, output)``.

Output is stdout+stderr combined; callers don't need to distinguish
streams for these checks (failure modes are dominated by rc).
"""


@dataclass(frozen=True)
class AncestryResult:
    """Outcome of a single branch-tip-vs-origin/main ancestry check.

    Fields:
        is_ancestor: True iff `git merge-base --is-ancestor` returned 0.
        branch_tip: The resolved SHA of the branch tip (empty when the
            ref could not be resolved — branch missing locally and not
            on the remote).
        reason: Human-readable note when `is_ancestor` is False, for
            logs and the `qk detect-merged` report column.
    """

    is_ancestor: bool
    branch_tip: str
    reason: str


def branch_is_ancestor_of_main(
    run_git: RunGit,
    *,
    branch_ref: str,
    base_ref: str = "origin/main",
    fetch_first: bool = True,
    pr_remote: str = "origin",
    base_branch: str = "main",
) -> AncestryResult:
    """Return whether `branch_ref`'s tip is an ancestor of `base_ref`.

    When `fetch_first` is True (the default for production call sites),
    runs `git fetch <pr_remote> <base_branch>` first so the local
    `origin/main` view reflects the current remote. Callers that need
    fetch-throttling (e.g. one fetch per polling cycle, not per task)
    should pass `fetch_first=False` and run their own fetch out-of-band.

    Branch-missing cases (rev-parse rc != 0) return
    `is_ancestor=False, branch_tip=""` so the caller can route to the
    existing closed-without-merge handling instead of a false-positive
    auto-merge.
    """
    if fetch_first:
        run_git(["fetch", pr_remote, base_branch])
    rc, out = run_git(["rev-parse", branch_ref])
    branch_tip = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if rc != 0 or not branch_tip:
        return AncestryResult(
            is_ancestor=False,
            branch_tip="",
            reason=f"could not resolve branch tip for {branch_ref!r}",
        )
    rc_anc, anc_out = run_git(["merge-base", "--is-ancestor", branch_tip, base_ref])
    if rc_anc == 0:
        return AncestryResult(
            is_ancestor=True,
            branch_tip=branch_tip,
            reason=f"{branch_tip[:12]} is an ancestor of {base_ref}",
        )
    return AncestryResult(
        is_ancestor=False,
        branch_tip=branch_tip,
        reason=f"{branch_tip[:12]} is NOT an ancestor of {base_ref}: {anc_out.strip()[:200]}",
    )


def maybe_auto_merge_via_ancestry(
    *,
    cfg: Any,
    store: Any,
    task_id: str,
    row_now: Mapping[str, Any],
    run_git: RunGit,
) -> bool:
    """Plan 56 worker-facing helper.

    Returns True iff the auto-merge path fired (caller should propagate
    a MERGED outcome). Returns False iff the caller should fall through
    to the existing closed-without-merge handling — either because the
    cfg flag is disabled, the task has no recorded branch, or the
    ancestry check did not match.

    Pulls in `fsm_runtime.mark_merged` for the side effect; uses the
    same FSM call `qk mark-merged` and `qk detect-merged --apply` use,
    so the audit trail looks identical regardless of how the auto-merge
    was triggered.
    """
    if not getattr(cfg, "auto_detect_merged_via_ancestry", True):
        return False
    branch = row_now.get("branch")
    if not branch:
        return False
    result = branch_is_ancestor_of_main(
        run_git,
        branch_ref=str(branch),
        base_ref=f"{cfg.pr_remote}/{cfg.base_branch}",
        fetch_first=True,
        pr_remote=cfg.pr_remote,
        base_branch=cfg.base_branch,
    )
    if not result.is_ancestor:
        log.info(
            "task %s: PR closed-not-merged; ancestry check did not match (%s); "
            "falling through to existing closed-without-merge handling",
            task_id,
            result.reason,
        )
        return False
    note = (
        f"auto-merged via ancestry: PR closed without GH merge, but "
        f"{result.branch_tip[:12]} is reachable from "
        f"{cfg.pr_remote}/{cfg.base_branch} "
        f"(release-batch integration pattern detected)"
    )
    log.info("task %s: %s", task_id, note)
    fsm_runtime.mark_merged(store, task_id, note=note)
    return True
