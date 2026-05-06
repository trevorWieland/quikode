"""GitHub review-thread access via `gh api graphql`.

The `gh pr view --json reviews` REST surface lacks thread state — it can't
distinguish open vs resolved threads, can't resolve threads, and conflates
reviews with issue comments. For the v3 continuous-review loop we need:
- which threads are unresolved
- whether the latest comment is human or bot
- the ability to mark threads resolved after we push a fix

All of this is GraphQL-only.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import UTC, datetime

from pydantic import BaseModel

from . import net_retry

log = logging.getLogger("quikode.github_graphql")


# Bot-author allowlist. Anything in this set (or matching the `[bot]` suffix
# convention used by GitHub apps) is treated as a non-human review-thread
# author. The set is module-scope so callers can reference it; runtime
# additions come from the QUIKODE_BOT_ALLOWLIST env var (comma-separated).
_BOT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "chatgpt-codex-connector",
        "github-actions",
        "dependabot",
        "claude",
        "codecov-commenter",
    }
)


def _extra_bots() -> frozenset[str]:
    raw = os.environ.get("QUIKODE_BOT_ALLOWLIST", "")
    if not raw:
        return frozenset()
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def is_bot_author(login: str) -> bool:
    """Return True if `login` looks like a bot/app reviewer rather than a human.

    GitHub app accounts always end with `[bot]`. Beyond that, we hand-curate a
    small allowlist of well-known automation accounts (codex, github-actions,
    dependabot, etc.). The env var QUIKODE_BOT_ALLOWLIST can extend the set
    at runtime without code changes (comma-separated logins).
    """
    if not login:
        return False
    if login.endswith("[bot]"):
        return True
    if login in _BOT_ALLOWLIST:
        return True
    return login in _extra_bots()


class ReviewThread(BaseModel):
    """One GitHub PR review thread (a comment chain anchored to a file/line)."""

    thread_id: str
    is_resolved: bool
    is_outdated: bool
    path: str | None
    line: int | None
    last_comment_id: str
    # REST integer id of the last comment, used for `/comments/{id}/replies`.
    # Optional because old data / synthetic ReviewThread rows may lack it.
    last_comment_database_id: int | None = None
    last_comment_author: str
    last_comment_body: str
    last_comment_created_at: float  # unix timestamp
    last_comment_is_bot: bool


_REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          comments(last: 1) {
            nodes {
              id
              databaseId
              body
              createdAt
              author { login }
            }
          }
        }
      }
    }
  }
}
""".strip()


_RESOLVE_THREAD_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
""".strip()


def _parse_iso8601(ts: str) -> float:
    """Parse GitHub's ISO-8601 timestamps (e.g. `2026-05-02T14:31:45Z`) to unix.

    Returns 0.0 on parse failure rather than raising — graphql output is
    generally well-formed but we don't want a single weird row to crash the
    daemon's review-watcher pass.
    """
    if not ts:
        return 0.0
    try:
        # Python's fromisoformat handles the trailing 'Z' since 3.11.
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).astimezone(UTC).timestamp()
    except (ValueError, TypeError):
        log.warning("could not parse timestamp %r", ts)
        return 0.0


def _split_repo(repo: str) -> tuple[str, str]:
    """Split `owner/name` into `(owner, name)`."""
    if "/" not in repo:
        raise ValueError(f"repo must be 'owner/name'; got {repo!r}")
    owner, _, name = repo.partition("/")
    return owner, name


def get_review_threads(repo: str, pr_number: int, *, gh_bin: str = "gh") -> list[ReviewThread]:
    """Fetch all review threads for a PR via `gh api graphql`.

    Returns an empty list on any error (bad json, gh failure, missing fields).
    Best-effort — the daemon's review-watcher loop runs forever, so we never
    raise from here; we log and move on.
    """
    try:
        owner, name = _split_repo(repo)
    except ValueError as e:
        log.warning("get_review_threads: %s", e)
        return []

    cmd = [
        gh_bin,
        "api",
        "graphql",
        "-f",
        f"query={_REVIEW_THREADS_QUERY}",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-F",
        f"number={pr_number}",
    ]
    try:
        r = net_retry.run_with_backoff(cmd, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("get_review_threads: subprocess error: %s", e)
        return []
    if r.returncode != 0:
        log.warning("get_review_threads: gh exited %d; stderr=%s", r.returncode, (r.stderr or "")[:300])
        return []
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        log.warning("get_review_threads: bad json from gh: %s", e)
        return []

    nodes = (
        payload.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes", [])
    )
    if not isinstance(nodes, list):
        return []

    threads: list[ReviewThread] = []
    for node in nodes:
        try:
            comments = (node.get("comments") or {}).get("nodes") or []
            if not comments:
                # Thread with no comments — skip; nothing to act on.
                continue
            last = comments[-1]
            author_login = ((last.get("author") or {}).get("login")) or ""
            db_id = last.get("databaseId")
            threads.append(
                ReviewThread(
                    thread_id=node.get("id") or "",
                    is_resolved=bool(node.get("isResolved")),
                    is_outdated=bool(node.get("isOutdated")),
                    path=node.get("path"),
                    line=node.get("line"),
                    last_comment_id=last.get("id") or "",
                    last_comment_database_id=int(db_id) if db_id is not None else None,
                    last_comment_author=author_login,
                    last_comment_body=last.get("body") or "",
                    last_comment_created_at=_parse_iso8601(last.get("createdAt") or ""),
                    last_comment_is_bot=is_bot_author(author_login),
                )
            )
        except (TypeError, ValueError, KeyError) as e:
            log.warning("get_review_threads: bad node, skipping: %s", e)
            continue
    return threads


def reply_to_review_thread(
    *,
    repo: str,
    pr_number: int,
    last_comment_database_id: int,
    body: str,
    gh_bin: str = "gh",
) -> bool:
    """Post a reply on an existing review thread via REST.

    GitHub's GraphQL surface doesn't have a "reply to thread" mutation; the
    documented path is REST `POST /repos/.../pulls/{n}/comments/{id}/replies`
    where `{id}` is the integer (databaseId) of any comment in the thread.

    Returns True iff `gh api` exits 0. Best-effort: failures are logged.
    """
    if not last_comment_database_id or not body:
        return False
    cmd = [
        gh_bin,
        "api",
        "-X",
        "POST",
        f"/repos/{repo}/pulls/{pr_number}/comments/{last_comment_database_id}/replies",
        "-f",
        f"body={body}",
    ]
    try:
        r = net_retry.run_with_backoff(cmd, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("reply_to_review_thread: subprocess error: %s", e)
        return False
    if r.returncode != 0:
        log.warning(
            "reply_to_review_thread: gh exited %d; stderr=%s",
            r.returncode,
            (r.stderr or "")[:300],
        )
        return False
    return True


def resolve_thread(thread_id: str, *, gh_bin: str = "gh") -> bool:
    """Mark a review thread resolved via the `resolveReviewThread` mutation.

    Returns True iff the response says `thread.isResolved == true`. Best-effort:
    network/gh errors return False without raising.
    """
    if not thread_id:
        return False
    cmd = [
        gh_bin,
        "api",
        "graphql",
        "-f",
        f"query={_RESOLVE_THREAD_MUTATION}",
        "-F",
        f"threadId={thread_id}",
    ]
    try:
        r = net_retry.run_with_backoff(cmd, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("resolve_thread: subprocess error: %s", e)
        return False
    if r.returncode != 0:
        log.warning("resolve_thread: gh exited %d; stderr=%s", r.returncode, (r.stderr or "")[:300])
        return False
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        log.warning("resolve_thread: bad json from gh: %s", e)
        return False
    thread = payload.get("data", {}).get("resolveReviewThread", {}).get("thread", {})
    return bool(thread.get("isResolved"))
