"""gh CLI wrappers — both inside-container (push, pr create) and host-side (poll)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .docker_env import TaskContainer, exec_in


@dataclass
class PRStatus:
    number: int
    url: str
    state: str  # OPEN | MERGED | CLOSED
    mergeable: str  # MERGEABLE | CONFLICTING | UNKNOWN
    checks_status: str  # success | failure | pending | none
    failed_checks: list[dict]
    base_ref_name: str = ""  # base branch the PR targets — empty if unknown
    head_sha: str = ""  # head commit sha — used for cascade-on-push detection


def _exec_gh(
    handle: TaskContainer, args: list[str], log_path: Path | None = None, timeout: int = 60
) -> tuple[int, str, str]:
    return exec_in(handle, ["bash", "-lc", "gh " + " ".join(args)], log_path=log_path, timeout=timeout)


def commit_all(handle: TaskContainer, message: str, log_path: Path | None = None) -> tuple[int, str]:
    """Stage everything, commit. Returns (rc, output)."""
    cmd = f"git add -A && git commit -m {_sh_quote(message)}"
    rc, out, err = exec_in(handle, ["bash", "-lc", cmd], log_path=log_path)
    return rc, out + err


def push(
    handle: TaskContainer, branch: str, remote: str = "origin", log_path: Path | None = None
) -> tuple[int, str]:
    rc, out, err = exec_in(
        handle,
        ["bash", "-lc", f"git push -u {remote} {branch}"],
        log_path=log_path,
        timeout=180,
    )
    return rc, out + err


def ahead_count(handle: TaskContainer, branch: str, base: str = "main", log_path: Path | None = None) -> int:
    """How many commits is `branch` ahead of `origin/base`? 0 if base ref unknown.

    Used post-v3 to detect "branch already has work via per-subtask commits"
    even when the working tree is clean (so commit_all says 'nothing to commit').
    """
    rc, out, _err = exec_in(
        handle,
        ["bash", "-lc", f"git rev-list --count origin/{base}..{branch} 2>/dev/null || echo 0"],
        log_path=log_path,
        timeout=30,
    )
    if rc != 0:
        return 0
    try:
        return int(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0


def open_pr(
    handle: TaskContainer, title: str, body: str, base: str = "main", log_path: Path | None = None
) -> tuple[int, str, str]:
    """Returns (rc, pr_url, raw_output)."""
    # Write body to a tempfile inside the container to avoid quoting hell
    write = exec_in(handle, ["bash", "-lc", "cat > /tmp/qk_pr_body.md"], stdin=body)
    if write[0] != 0:
        return write[0], "", write[1] + write[2]
    rc, out, err = _exec_gh(
        handle,
        ["pr", "create", "--title", _sh_quote(title), "--body-file", "/tmp/qk_pr_body.md", "--base", base],
        log_path=log_path,
        timeout=120,
    )
    url = ""
    for line in (out + err).splitlines():
        if line.startswith("https://github.com/"):
            url = line.strip()
            break
    return rc, url, out + err


def pr_view(
    repo: Path,
    pr_number: int,
    fields: str = "state,mergeable,statusCheckRollup,url,baseRefName,headRefOid",
) -> dict[str, Any]:
    """Run gh pr view on the host, against the host repo.

    NOTE: review-thread state lives in GraphQL only — see
    `quikode/github_graphql.py:get_review_threads`. This REST surface gives
    us merge/check/state but cannot distinguish open vs resolved review
    threads, which is what the v3 review-watcher needs.
    """
    r = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--json", fields],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return {}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {}


def poll_pr(repo: Path, pr_number: int) -> PRStatus:
    """Fetch state/mergeable/check rollup for a PR.

    This is a thin status fetcher — it does NOT return review comments. For
    review-thread polling, use `github_graphql.get_review_threads`.

    Also captures `head_sha` (the PR's current head commit) so the
    cascade-on-push detector can notice when a parent's branch advances
    without going through MERGE_READY.
    """
    data = pr_view(repo, pr_number)
    state = data.get("state", "UNKNOWN")
    mergeable = data.get("mergeable", "UNKNOWN")
    rollup = data.get("statusCheckRollup", []) or []
    failed = [
        c
        for c in rollup
        if (c.get("conclusion") or c.get("state")) in ("FAILURE", "TIMED_OUT", "STARTUP_FAILURE")
    ]
    pending = [c for c in rollup if (c.get("status") or "").upper() in ("IN_PROGRESS", "QUEUED", "PENDING")]
    if not rollup:
        checks = "none"
    elif failed:
        checks = "failure"
    elif pending:
        checks = "pending"
    else:
        checks = "success"

    return PRStatus(
        number=pr_number,
        url=data.get("url", ""),
        state=state,
        mergeable=mergeable,
        checks_status=checks,
        failed_checks=failed,
        base_ref_name=data.get("baseRefName", "") or "",
        head_sha=str(data.get("headRefOid") or ""),
    )


def fetch_failed_check_logs(repo: Path, pr_number: int, max_lines: int = 200) -> str:
    """Best-effort: grab a snippet from the most recently failed check run."""
    # gh pr checks doesn't return logs directly; use gh run list + view
    r = subprocess.run(
        ["gh", "pr", "checks", str(pr_number)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout or ""


def _sh_quote(s: str) -> str:
    """Single-quote for bash -lc consumption."""
    return "'" + s.replace("'", "'\"'\"'") + "'"
