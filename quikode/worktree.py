"""Git worktree management for the target repo.

Thread safety: git's worktree subsystem maintains its own lock
(`<repo>/.git/worktrees.lock`) but two parallel `git worktree add` calls can
still race in pathological ways — we've seen exit-255 with no stderr when the
second call sneaks in before the first finalizes its directory. A module-level
RLock serializes our calls so there's only ever one in flight per process.
"""

from __future__ import annotations

import logging
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .docker_env import exec_in

log = logging.getLogger("quikode.worktree")

if TYPE_CHECKING:
    from .docker_env import TaskContainer
    from .subtask_schema import Subtask

_worktree_lock = threading.RLock()


# ----- per-subtask commit (v3 Phase A) -----


@dataclass
class CommitResult:
    """Outcome of `commit_subtask`. `success=True` iff add+commit+push all
    landed (or push was skipped). On failure, `transient` distinguishes
    container/network glitches (free retry) from real failures (burns a
    retry, surfaces as a checker-style FAIL)."""

    success: bool
    commit_sha: str | None
    transient: bool
    output: str


# stderr fragments that mean "git push failed because the network/remote
# was unhealthy" — those should free-retry. Anything else (e.g. "non-fast-
# forward", auth rejection, hook rejection) is a real failure.
_TRANSIENT_GIT_PUSH_MARKERS: tuple[str, ...] = (
    "Could not resolve host",
    "Connection refused",
    "Connection timed out",
    "Operation timed out",
    "Could not read from remote repository",
    "TLS connection",
    "Failed to connect to",
    "remote end hung up unexpectedly",
    "RPC failed",
    "early EOF",
    "fatal: unable to access",
)


def _is_transient_git_failure(rc: int, output: str) -> bool:
    """Detect transient git failures (network blips, GitHub 5xx, DNS
    weirdness). Conservative: rc=0 is never transient; rc!=0 with no
    matching marker is treated as a real failure so the operator/triage
    sees it.
    """
    if rc == 0:
        return False
    if not output:
        return False
    return any(marker in output for marker in _TRANSIENT_GIT_PUSH_MARKERS)


def commit_subtask(
    handle: TaskContainer,
    subtask: Subtask,
    message: str,
    *,
    branch: str,
    remote: str = "origin",
    push: bool = True,
    log_path: Path | None = None,
    timeout: int = 300,
) -> CommitResult:
    """Run `git add <files_to_touch> && git commit -m <message> && git push`
    inside the container at /workspace.

    Only files declared in `subtask.files_to_touch` are added — if a doer
    wrote outside its lane, those files stay untracked and surface in the
    next checker / progress check. This is intentional: the planner's
    declared scope is the contract.

    Returns a `CommitResult`. `transient=True` means a free retry is safe
    (network blip, container glitch). On non-transient failure the caller
    should synthesize a Verdict.FAIL with the captured output as triage
    feedback.
    """
    if not subtask.files_to_touch:
        return CommitResult(
            success=False,
            commit_sha=None,
            transient=False,
            output="commit_subtask: subtask declared no files_to_touch; nothing to add",
        )

    # 1. Filter files_to_touch to only paths that actually exist in the worktree.
    # The planner declares files speculatively — for auto-generated outputs
    # (e.g. Paraglide message bundles, openapi-typegen output) the actual on-disk
    # name may differ from what the planner guessed (.ts vs .js, or generated
    # at all). Without this filter, `git add -- <missing>` fails rc=1 with
    # `pathspec did not match any files`, the worker synthesizes a checker FAIL,
    # triage retries the doer, doer no-ops because the implementation is
    # already committed, and the loop runs the full retry budget. Boundary
    # discipline is preserved: we still only add files the planner declared,
    # we just don't reject the whole commit because one declared file is a
    # ghost.
    declared = list(subtask.files_to_touch)
    _rc_check, check_out, _ = exec_in(
        handle,
        [
            "bash",
            "-lc",
            "cd /workspace && for f in "
            + " ".join(shlex.quote(p) for p in declared)
            + '; do [ -e "$f" ] && echo "$f"; done',
        ],
        log_path=log_path,
        timeout=30,
    )
    existing = [line for line in (check_out or "").splitlines() if line.strip()]
    missing = [p for p in declared if p not in existing]
    if not existing:
        return CommitResult(
            success=False,
            commit_sha=None,
            transient=False,
            output=(
                "commit_subtask: none of the planner-declared files_to_touch exist "
                f"on disk: {declared!r}. Doer likely wrote nothing OR planner declared "
                "ghost paths. Treating as a real failure so triage can re-prompt."
            ),
        )
    if missing and log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as f:
            f.write(
                f"[commit_subtask] {len(missing)} declared file(s) missing on disk "
                f"(skipped from `git add`): {missing!r}\n"
            )

    # 2. git add — only the existing, planner-declared files.
    quoted = " ".join(shlex.quote(p) for p in existing)
    rc, out, err = exec_in(
        handle,
        ["bash", "-lc", f"cd /workspace && git add -- {quoted}"],
        log_path=log_path,
        timeout=timeout,
    )
    if rc != 0:
        return CommitResult(
            success=False,
            commit_sha=None,
            transient=False,  # `git add` doesn't network — failure is real
            output=f"git add failed (rc={rc}):\n{out}\n{err}",
        )

    # 3. git commit -m <message>. If there's nothing staged it usually
    # means the doer made no diff — but it can ALSO mean a prior attempt
    # already committed this subtask's work and the worker re-entered.
    # Detect the second case by checking whether HEAD has commits beyond
    # the worktree's stored base_ref_sha or the latest non-HEAD subtask
    # commit; if so, treat as already-done (idempotent re-entry) and
    # return success with the existing HEAD sha. Otherwise it's a real
    # no-diff failure that triage should see.
    rc, out, err = exec_in(
        handle,
        [
            "bash",
            "-lc",
            f"cd /workspace && git commit -m {shlex.quote(message)}",
        ],
        log_path=log_path,
        timeout=timeout,
    )
    nothing_to_commit = rc != 0 and "nothing to commit" in ((out or "") + (err or ""))
    if rc != 0 and not nothing_to_commit:
        combined = (out or "") + "\n" + (err or "")
        return CommitResult(
            success=False,
            commit_sha=None,
            transient=False,
            output=f"git commit failed (rc={rc}):\n{combined}",
        )
    if nothing_to_commit:
        # Idempotent re-entry guard: if HEAD has at least one commit on
        # this branch beyond `main` (the base), the prior attempt's work
        # already landed locally. Treat as success and skip to push so
        # remote stays aligned.
        _rc_ahead, ahead_out, _ = exec_in(
            handle,
            ["bash", "-lc", "cd /workspace && git rev-list --count main..HEAD 2>/dev/null || echo 0"],
            log_path=log_path,
            timeout=30,
        )
        ahead = 0
        try:
            ahead = int((ahead_out or "0").strip().splitlines()[-1])
        except (ValueError, IndexError):
            ahead = 0
        if ahead == 0:
            combined = (out or "") + "\n" + (err or "")
            return CommitResult(
                success=False,
                commit_sha=None,
                transient=False,
                output=f"git commit failed (rc={rc}):\n{combined}",
            )
        # Fall through with ahead > 0 — capture HEAD + push (idempotent).

    # 4. capture the new HEAD sha.
    rc, sha_out, _sha_err = exec_in(
        handle,
        ["bash", "-lc", "cd /workspace && git rev-parse HEAD"],
        log_path=log_path,
        timeout=30,
    )
    commit_sha = sha_out.strip() if rc == 0 else None

    # 5. git push (optional).
    if push:
        rc, out, err = exec_in(
            handle,
            [
                "bash",
                "-lc",
                f"cd /workspace && git push {shlex.quote(remote)} {shlex.quote(branch)}",
            ],
            log_path=log_path,
            timeout=timeout,
        )
        if rc != 0:
            combined = (out or "") + "\n" + (err or "")
            return CommitResult(
                success=False,
                commit_sha=commit_sha,
                transient=_is_transient_git_failure(rc, combined),
                output=f"git push failed (rc={rc}):\n{combined}",
            )

    return CommitResult(
        success=True,
        commit_sha=commit_sha,
        transient=False,
        output="ok",
    )


def commit_response(
    handle: TaskContainer,
    message: str,
    *,
    branch: str,
    remote: str = "origin",
    push: bool = True,
    log_path: Path | None = None,
    timeout: int = 300,
) -> CommitResult:
    """Commit + push a whole-spec edit (no per-file scoping).

    Used by the v3 review-response cycle in `worker.run_review_response`,
    where the doer is given free rein to edit anything within the existing
    worktree to address human/bot review feedback. Mirrors `commit_subtask`
    but uses `git add -A` so any cross-file edit a thread requires lands.

    Returns a `CommitResult` with the same semantics as `commit_subtask`:
    `transient=True` means a free retry is safe.
    """
    rc, out, err = exec_in(
        handle,
        ["bash", "-lc", "cd /workspace && git add -A"],
        log_path=log_path,
        timeout=timeout,
    )
    if rc != 0:
        return CommitResult(
            success=False,
            commit_sha=None,
            transient=False,
            output=f"git add -A failed (rc={rc}):\n{out}\n{err}",
        )

    rc, out, err = exec_in(
        handle,
        ["bash", "-lc", f"cd /workspace && git commit -m {shlex.quote(message)}"],
        log_path=log_path,
        timeout=timeout,
    )
    if rc != 0:
        combined = (out or "") + "\n" + (err or "")
        return CommitResult(
            success=False,
            commit_sha=None,
            transient=False,
            output=f"git commit failed (rc={rc}):\n{combined}",
        )

    rc, sha_out, _sha_err = exec_in(
        handle,
        ["bash", "-lc", "cd /workspace && git rev-parse HEAD"],
        log_path=log_path,
        timeout=30,
    )
    commit_sha = sha_out.strip() if rc == 0 else None

    if push:
        rc, out, err = exec_in(
            handle,
            [
                "bash",
                "-lc",
                f"cd /workspace && git push {shlex.quote(remote)} {shlex.quote(branch)}",
            ],
            log_path=log_path,
            timeout=timeout,
        )
        if rc != 0:
            combined = (out or "") + "\n" + (err or "")
            return CommitResult(
                success=False,
                commit_sha=commit_sha,
                transient=_is_transient_git_failure(rc, combined),
                output=f"git push failed (rc={rc}):\n{combined}",
            )

    return CommitResult(
        success=True,
        commit_sha=commit_sha,
        transient=False,
        output="ok",
    )


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def sanitize_branch_name(task_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._\-/]", "-", task_id)


def branch_for(task_id: str, prefix: str = "quikode", unique_suffix: bool = True) -> str:
    """Return a per-run branch name.

    With unique_suffix=True (default), append a short hex token so each
    fresh `quikode run` gets its own branch, avoiding collisions with
    prior pushes that may already exist on the remote (which we can't
    always delete).
    """
    base = f"{prefix}/{sanitize_branch_name(task_id).lower()}"
    if unique_suffix:
        return f"{base}-{secrets.token_hex(3)}"
    return base


def fetch_base(repo: Path, remote: str, base_branch: str, *, retries: int = 3) -> None:
    """Fetch the base branch into the parent repo. Retries on lock contention.

    Multiple workers spinning up in parallel can each call `git fetch origin
    main` on the same repo at the same time. Git serializes via a lockfile;
    losing the race produces `error: cannot lock ref ... unable to create ...`
    or similar. The fetch is itself idempotent (fetching the same ref twice
    is a no-op), so we retry with short backoff.
    """
    last_err: subprocess.CalledProcessError | None = None
    for attempt in range(retries):
        try:
            _run(["git", "fetch", remote, base_branch], cwd=repo)
            return
        except subprocess.CalledProcessError as e:
            last_err = e
            stderr = (e.stderr or "").lower()
            # Only retry on lock-contention markers; on real auth/network
            # failures fail fast so the task FAILS visibly.
            transient = any(
                m in stderr for m in ("cannot lock ref", "unable to create", "another git process")
            )
            if not transient or attempt == retries - 1:
                raise
            time.sleep(0.5 * (attempt + 1))
    if last_err is not None:
        raise last_err


def add_worktree(
    repo: Path, worktree_path: Path, branch: str, base_branch: str, remote: str = "origin"
) -> None:
    """Create branch off origin/<base_branch> and add a worktree.

    Idempotent for resume scenarios:
    - If the worktree path is already registered with git, reuse it as-is
      (the in-progress changes from a prior attempt are preserved).
    - If only the branch already exists, attach a fresh worktree to it.
    - Otherwise, branch off origin/<base_branch> and add a new worktree.

    Serialized via _worktree_lock — see module docstring.
    """
    with _worktree_lock:
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        listing = _run(["git", "worktree", "list", "--porcelain"], cwd=repo, check=False)
        wt_target = str(worktree_path)
        already_registered = any(
            line.strip() == f"worktree {wt_target}" for line in listing.stdout.splitlines()
        )
        if already_registered and worktree_path.exists():
            return  # resume: reuse the existing worktree as-is, including any uncommitted edits
        existing = _run(["git", "branch", "--list", branch], cwd=repo, check=False)
        if existing.stdout.strip():
            _run(["git", "worktree", "add", str(worktree_path), branch], cwd=repo)
        else:
            _run(
                ["git", "worktree", "add", "-b", branch, str(worktree_path), f"{remote}/{base_branch}"],
                cwd=repo,
            )


def add_worktree_off_branch(
    repo: Path,
    worktree_path: Path,
    branch: str,
    parent_branch: str,
    *,
    remote: str | None = None,
) -> None:
    """Create a worktree branched off a local branch (used for Phase C stacking).

    When `remote` is given, fetches `parent_branch` from the remote first so
    the local ref exists. This is the v3 stacked-diff path: the parent's
    branch is on origin (because the parent's PR is open), but may not yet
    exist locally on the host repo if this is the first time we're stacking
    on it.
    """
    with _worktree_lock:
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        if remote:
            # Fetch the parent ref so it lands locally (idempotent).
            _run(["git", "fetch", remote, f"{parent_branch}:{parent_branch}"], cwd=repo, check=False)
        _run(["git", "worktree", "add", "-b", branch, str(worktree_path), parent_branch], cwd=repo)


def remove_worktree(repo: Path, worktree_path: Path, force: bool = True) -> None:
    args = ["git", "worktree", "remove", str(worktree_path)]
    if force:
        args.append("--force")
    _run(args, cwd=repo, check=False)


def delete_branch(repo: Path, branch: str) -> None:
    _run(["git", "branch", "-D", branch], cwd=repo, check=False)


def prune(repo: Path) -> None:
    _run(["git", "worktree", "prune"], cwd=repo, check=False)


def prune_stale_worktrees(repo_path: Path, worktree_root: Path) -> list[Path]:
    """Remove on-disk worktree directories that git has forgotten about.

    Two cleanup paths:

    1. Directories under `worktree_root/` that are NOT registered in
       `git worktree list --porcelain` AND don't contain a `.git` reference
       linking back to the repo are stale debris (a crashed `quikode run`
       can leave these behind). They get `shutil.rmtree`'d.
    2. Worktrees in `git worktree list` whose paths don't exist on disk
       leave dangling git records — `git worktree prune` cleans those.

    Returns the list of directories removed in step 1 (the dangling git
    records cleanup is silent — git doesn't tell us what it pruned).

    Best-effort: if the repo is missing or git is uncooperative, we log
    and return an empty list rather than raising. Used at:
    - `quikode run` startup (clean slate before scheduling)
    - daemon supervisor restart (defensive against crashed inner runs)
    """
    removed: list[Path] = []
    if not worktree_root.exists():
        return removed

    # 1. Snapshot git's current worktree list. `git worktree list --porcelain`
    # emits blank-line-separated stanzas; `worktree <path>` is the first line
    # of each stanza.
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("prune_stale_worktrees: git worktree list failed: %s", e)
        return removed
    registered: set[Path] = set()
    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            if line.startswith("worktree "):
                try:
                    registered.add(Path(line[len("worktree ") :]).resolve())
                except OSError:
                    pass

    # 2. Walk worktree_root and prune anything not registered. We only
    # rmtree directories whose `.git` either doesn't exist or doesn't point
    # back at our repo's `.git/worktrees/...` — that's the smell of a stale
    # dir vs. one that git just hasn't re-listed yet.
    try:
        for child in worktree_root.iterdir():
            if not child.is_dir():
                continue
            try:
                resolved = child.resolve()
            except OSError:
                resolved = child
            if resolved in registered:
                continue
            # If the child has a valid .git linkage, skip it — git might
            # know about it via a different path normalization.
            git_link = child / ".git"
            if git_link.exists():
                # `.git` is usually a file with `gitdir: ...` for worktrees.
                try:
                    if git_link.is_file():
                        content = git_link.read_text().strip()
                        if content.startswith("gitdir:"):
                            # Still linked — leave it alone; the next
                            # `git worktree prune` call below will clean
                            # up git's records if the gitdir target is
                            # itself stale.
                            continue
                    elif git_link.is_dir():
                        # A bare nested repo, not a worktree — don't touch.
                        continue
                except OSError:
                    pass
            try:
                shutil.rmtree(child)
                removed.append(child)
                log.info("prune_stale_worktrees: removed stale dir %s", child)
            except OSError as e:
                log.warning("prune_stale_worktrees: could not rmtree %s: %s", child, e)
    except OSError as e:
        log.warning("prune_stale_worktrees: iterdir failed on %s: %s", worktree_root, e)

    # 3. Tell git to drop any registered worktrees whose paths are gone.
    try:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("prune_stale_worktrees: git worktree prune failed: %s", e)

    return removed


def list_worktrees(repo: Path) -> list[dict]:
    out = _run(["git", "worktree", "list", "--porcelain"], cwd=repo).stdout
    blocks = [b for b in out.split("\n\n") if b.strip()]
    result = []
    for blk in blocks:
        d = {}
        for line in blk.splitlines():
            if " " in line:
                k, v = line.split(" ", 1)
                d[k] = v
            else:
                d[line] = True
        result.append(d)
    return result
