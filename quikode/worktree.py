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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import git_push_recovery
from .execution import exec_in

log = logging.getLogger("quikode.worktree")

if TYPE_CHECKING:
    from .execution import ExecutionSandbox
    from .subtask_schema import Subtask

_worktree_lock = threading.RLock()


# ----- per-subtask commit (v3 Phase A) -----


@dataclass
class CommitResult:
    """Outcome of `commit_subtask`. `success=True` iff add+commit+push all
    landed (or push was skipped). On failure, `transient` distinguishes
    container/network glitches (free retry) from real failures (burns a
    retry, surfaces as a checker-style FAIL).

    `accepted_files` is the effective set of files staged at commit
    time — Plan 33 retired scope-review, so this is just the actual
    touched set. Empty on failure paths.
    """

    success: bool
    commit_sha: str | None
    transient: bool
    output: str
    accepted_files: list[str] = field(default_factory=list)


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


def _classify_empty_staging(
    handle: ExecutionSandbox,
    declared: list[str],
    actually_touched: list[str],
    log_path: Path | None,
    timeout: int,
) -> CommitResult | None:
    """Plan 24 Z-99 helper: triage the empty-staging cases.

    `declared=[] AND nothing staged` → gate-only stabilization succeeded
    by definition (doer ran the gate, it passed, no edits needed) →
    return success with no new commit. `declared=[X] AND nothing staged`
    → real failure (doer didn't produce expected edits). Pre-fix this
    branch was a permanent BLOCK; R-0041 looped 16+ times on it.
    """
    if actually_touched:
        return None
    if declared:
        return _commit_failure(
            f"commit_subtask: subtask declared files_to_touch={declared} but doer produced no edits"
        )
    rc, head_sha, _ = exec_in(
        handle, ["bash", "-lc", "cd /workspace && git rev-parse HEAD"], log_path=log_path, timeout=timeout
    )
    sha = (head_sha or "").strip().splitlines()[0] if rc == 0 and head_sha else None
    return CommitResult(
        success=True,
        commit_sha=sha,
        transient=False,
        output="commit_subtask: gate-only success (declared=[] AND no edits required)",
        accepted_files=[],
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
    handle: ExecutionSandbox,
    subtask: Subtask,
    message: str,
    *,
    branch: str,
    remote: str = "origin",
    push: bool = True,
    log_path: Path | None = None,
    timeout: int = 300,
) -> CommitResult:
    """`git add -A && git commit && git push` for one subtask. Plan 33
    retired the scope-review adjudicator; `files_to_touch` is advisory
    only and there is no commit-time gating against it.

    `transient=True` → free retry. Plan 24's Z-99 declares
    `files_to_touch=[]` by design (lane is "make the gate green");
    empty staging there is success, not failure — see
    `_classify_empty_staging` (load-bearing for Z-99's gate-only
    success path).
    """
    declared = list(subtask.files_to_touch)

    rc, out, err = exec_in(
        handle, ["bash", "-lc", "cd /workspace && git add -A"], log_path=log_path, timeout=timeout
    )
    if rc != 0:
        return _commit_failure(f"git add -A failed (rc={rc}):\n{out}\n{err}")

    rc, diff_out, _ = exec_in(
        handle,
        ["bash", "-lc", "cd /workspace && git diff --cached --name-only"],
        log_path=log_path,
        timeout=30,
    )
    actually_touched: list[str] = [str(line) for line in (diff_out or "").splitlines() if line.strip()]
    accepted_files: list[str] = list(actually_touched)

    early = _classify_empty_staging(handle, declared, actually_touched, log_path, timeout)
    if early is not None:
        return early

    commit_outcome = _commit_or_idempotent(handle, message, log_path, timeout)
    if isinstance(commit_outcome, CommitResult):
        return commit_outcome

    rc, sha_out, _sha_err = exec_in(
        handle, ["bash", "-lc", "cd /workspace && git rev-parse HEAD"], log_path=log_path, timeout=30
    )
    commit_sha = sha_out.strip() if rc == 0 else None

    if push:
        return git_push_recovery.push_with_recovery(
            handle,
            branch,
            remote,
            exec_fn=exec_in,
            log_path=log_path,
            timeout=timeout,
            is_transient=_is_transient_git_failure,
            success=_commit_success(commit_sha, accepted_files),
            failure=lambda msg, transient=False: _commit_failure(
                msg, commit_sha=commit_sha, transient=transient
            ),
        )
    return _commit_success(commit_sha, accepted_files)


def _commit_or_idempotent(
    handle: ExecutionSandbox, message: str, log_path: Path | None, timeout: int
) -> CommitResult | None:
    """Run `git commit`. Return None if the commit landed (or idempotent
    re-entry detected — fall through to push); return a CommitResult on
    real failure."""
    rc, out, err = exec_in(
        handle,
        ["bash", "-lc", f"cd /workspace && git commit -m {shlex.quote(message)}"],
        log_path=log_path,
        timeout=timeout,
    )
    nothing_to_commit = rc != 0 and "nothing to commit" in ((out or "") + (err or ""))
    combined = (out or "") + "\n" + (err or "")
    if rc != 0 and not nothing_to_commit:
        return _commit_failure(f"git commit failed (rc={rc}):\n{combined}")
    if not nothing_to_commit:
        return None
    # Idempotent re-entry: nothing to commit but the branch may already
    # carry the work from a prior attempt. Push if ahead of base.
    _rc_ahead, ahead_out, _ = exec_in(
        handle,
        ["bash", "-lc", "cd /workspace && git rev-list --count main..HEAD 2>/dev/null || echo 0"],
        log_path=log_path,
        timeout=30,
    )
    try:
        ahead = int((ahead_out or "0").strip().splitlines()[-1])
    except (ValueError, IndexError):
        ahead = 0
    if ahead == 0:
        return _commit_failure(f"git commit failed (rc={rc}):\n{combined}")
    return None


def _commit_failure(output: str, *, commit_sha: str | None = None, transient: bool = False) -> CommitResult:
    return CommitResult(success=False, commit_sha=commit_sha, transient=transient, output=output)


def _commit_success(commit_sha: str | None, accepted_files: list[str]) -> CommitResult:
    return CommitResult(
        success=True,
        commit_sha=commit_sha,
        transient=False,
        output="ok",
        accepted_files=accepted_files,
    )


def commit_response(
    handle: ExecutionSandbox,
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
        handle, ["bash", "-lc", "cd /workspace && git add -A"], log_path=log_path, timeout=timeout
    )
    if rc != 0:
        return _commit_failure(f"git add -A failed (rc={rc}):\n{out}\n{err}")
    rc, out, err = exec_in(
        handle,
        ["bash", "-lc", f"cd /workspace && git commit -m {shlex.quote(message)}"],
        log_path=log_path,
        timeout=timeout,
    )
    if rc != 0:
        return _commit_failure(f"git commit failed (rc={rc}):\n{(out or '')}\n{(err or '')}")
    rc, sha_out, _sha_err = exec_in(
        handle, ["bash", "-lc", "cd /workspace && git rev-parse HEAD"], log_path=log_path, timeout=30
    )
    commit_sha = sha_out.strip() if rc == 0 else None
    if push:
        rc, out, err = exec_in(
            handle,
            ["bash", "-lc", f"cd /workspace && git push {shlex.quote(remote)} {shlex.quote(branch)}"],
            log_path=log_path,
            timeout=timeout,
        )
        if rc != 0:
            combined = (out or "") + "\n" + (err or "")
            return _commit_failure(
                f"git push failed (rc={rc}):\n{combined}",
                commit_sha=commit_sha,
                transient=_is_transient_git_failure(rc, combined),
            )
    return CommitResult(success=True, commit_sha=commit_sha, transient=False, output="ok")


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
    registered = _registered_worktree_paths(repo_path)
    try:
        removed = _remove_unregistered_worktree_dirs(worktree_root, registered)
    except OSError as e:
        log.warning("prune_stale_worktrees: iterdir failed on %s: %s", worktree_root, e)
    _prune_git_worktree_records(repo_path)
    return removed


def _registered_worktree_paths(repo_path: Path) -> set[Path]:
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
        return set()
    registered: set[Path] = set()
    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            if line.startswith("worktree "):
                try:
                    registered.add(Path(line[len("worktree ") :]).resolve())
                except OSError:
                    pass
    return registered


def _remove_unregistered_worktree_dirs(worktree_root: Path, registered: set[Path]) -> list[Path]:
    removed: list[Path] = []
    for child in worktree_root.iterdir():
        if _should_remove_worktree_dir(child, registered):
            try:
                shutil.rmtree(child)
                removed.append(child)
                log.info("prune_stale_worktrees: removed stale dir %s", child)
            except OSError as e:
                log.warning("prune_stale_worktrees: could not rmtree %s: %s", child, e)
    return removed


def _should_remove_worktree_dir(child: Path, registered: set[Path]) -> bool:
    if not child.is_dir():
        return False
    try:
        resolved = child.resolve()
    except OSError:
        resolved = child
    if resolved in registered:
        return False
    git_link = child / ".git"
    return not _has_active_git_link(git_link)


def _has_active_git_link(git_link: Path) -> bool:
    if not git_link.exists():
        return False
    try:
        if git_link.is_file():
            return git_link.read_text().strip().startswith("gitdir:")
        return git_link.is_dir()
    except OSError as e:
        log.debug("could not inspect git link %s: %s", git_link, e)
        return False


def _prune_git_worktree_records(repo_path: Path) -> None:
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
