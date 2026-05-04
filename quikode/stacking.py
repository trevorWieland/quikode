"""v3.5 Phase 2 — multi-parent stacking helpers.

When a child task depends on multiple un-merged parents (D depends on B
and C, both AWAITING_REVIEW), there is no single branch to fork from.
The chain still needs to advance — that's the whole point of stacked
diffs — so we build a synthetic *merge-base branch* by `git merge`-ing
the parents' tips into a transient ref. The child's worktree then
forks from that merge-base.

This module exposes two pure-ish primitives:

  - `compute_merge_base_branch_name(task_id) → str` — deterministic name
    so a re-provision of the same task reuses (or recreates) the same
    branch. Includes a 6-hex suffix derived from the parent set, so a
    *change* in the parent set (e.g. one parent merged) gives a fresh
    name and avoids accidentally reusing a stale merge.
  - `construct_merge_base(repo_path, parent_branches, branch_name) →
    sha | None` — `git merge`s the parents into the named branch. On
    success, returns the resulting commit sha; on conflict, leaves the
    repo clean (aborts the merge) and returns None so the caller can
    fall through to the conflict resolver.

Lives outside the worker monolith so the cascade-rebase scheduler
(future Phase 2 follow-up) can call it without an `import worker`.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("quikode.stacking")


def compute_merge_base_branch_name(task_id: str, parent_branches: list[str]) -> str:
    """Return a deterministic merge-base branch name for `task_id` rooted on
    the given `parent_branches`. The 6-hex suffix is a stable hash of the
    sorted parent set, so two re-provisions with the *same* parents reuse
    the same name; a parent change yields a fresh name (and lets the caller
    delete the stale ref without affecting other live trees).
    """
    if not parent_branches:
        raise ValueError("compute_merge_base_branch_name needs at least one parent_branch")
    digest = hashlib.sha1(("\x00".join(sorted(parent_branches))).encode("utf-8")).hexdigest()[:6]
    slug = task_id.lower().replace("_", "-")
    return f"quikode/{slug}-base-{digest}"


def construct_merge_base(
    *,
    repo_path: Path,
    parent_branches: list[str],
    branch_name: str,
    base_branch: str = "main",
    timeout_s: int = 120,
) -> str | None:
    """Create / reset `branch_name` to a synthetic merge of `parent_branches`.

    Sequence (all run with cwd=repo_path):

      1. `git fetch <pr_remote> <each_parent>` — defer to caller; we only
         need the local refs to be present. This helper assumes the caller
         already fetched.
      2. `git checkout -B <branch_name> <base_branch>` — start fresh from
         the target merge target (typically main). Discards any prior state
         on the branch — that's intentional; the merge-base is regenerated
         on every re-provision.
      3. `git merge --no-ff --no-edit <p1> <p2> ...` — true octopus merge
         when the parents are independent, or sequential merges when they
         conflict pairwise. We rely on git's default octopus strategy first;
         on failure, fall back to N-1 sequential `git merge` calls.
      4. On any merge conflict, `git merge --abort`, then return None.
         Caller decides whether to invoke the conflict-resolver agent.
      5. On clean merge, `git rev-parse HEAD` → return sha.

    Returns the merge-commit sha on success, None on conflict / git error.
    """
    if not parent_branches:
        return None

    def _git(*args: str) -> tuple[int, str]:
        try:
            r = subprocess.run(
                ["git", *args],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("construct_merge_base git %s raised: %s", args, e)
            return (-1, str(e))
        return (r.returncode, (r.stdout or "") + (r.stderr or ""))

    # 2. Reset/start the merge-base branch from base_branch.
    rc, out = _git("checkout", "-B", branch_name, base_branch)
    if rc != 0:
        log.warning("construct_merge_base: checkout -B %s %s failed: %s", branch_name, base_branch, out[:200])
        return None

    # 3a. Try octopus merge first (works when no parent conflicts pairwise).
    rc, out = _git("merge", "--no-ff", "--no-edit", *parent_branches)
    if rc == 0:
        rc2, sha = _git("rev-parse", "HEAD")
        return sha.strip().splitlines()[-1] if rc2 == 0 and sha.strip() else None

    # Octopus failed — abort and fall back to sequential merges.
    _git("merge", "--abort")
    rc, out = _git("checkout", "-B", branch_name, base_branch)
    if rc != 0:
        return None
    for p in parent_branches:
        rc, out = _git("-c", "core.editor=true", "merge", "--no-ff", "--no-edit", p)
        if rc != 0:
            log.info(
                "construct_merge_base: sequential merge of %s into %s conflicted; aborting",
                p,
                branch_name,
            )
            _git("merge", "--abort")
            return None
    rc2, sha = _git("rev-parse", "HEAD")
    return sha.strip().splitlines()[-1] if rc2 == 0 and sha.strip() else None
