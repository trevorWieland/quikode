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


class Review(BaseModel):
    """One formal GitHub PR review (the `pullRequest.reviews` slice).

    The post-plan-28 polling surface only acts on `state == "CHANGES_REQUESTED"`
    from a non-bot author; APPROVED triggers auto-merge (when configured); and
    COMMENTED is bundled as context but never as a transition trigger.
    """

    review_id: str  # GraphQL node id (used as `last_processed_review_id`)
    database_id: int | None
    state: str  # APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED | PENDING
    submitted_at: float  # unix timestamp; 0.0 if missing/parse-failure
    body: str
    author: str
    is_bot: bool


class PRComment(BaseModel):
    """One PR-level issue comment (general PR conversation, not inline)."""

    comment_id: str
    database_id: int | None
    body: str
    created_at: float
    author: str
    is_bot: bool


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


# Plan 28: only formal Reviews (with `state` ∈ {APPROVED, CHANGES_REQUESTED,
# COMMENTED}) drive post-PR state transitions. Inline review-thread comments
# (handled by `_REVIEW_THREADS_QUERY` above) and PR-level issue comments
# become bundled CONTEXT for the fixup planner — never polling triggers.
_PR_REVIEWS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviews(last: 50) {
        nodes {
          id
          databaseId
          state
          submittedAt
          body
          author { login }
        }
      }
      comments(last: 50) {
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


def _fetch_pr_reviews_payload(repo: str, pr_number: int, *, gh_bin: str = "gh") -> dict | None:
    """Run `_PR_REVIEWS_QUERY` and return the parsed payload, or None on error.

    Used by both `get_latest_reviews` and `bundle_pr_context` so we hit GitHub
    once for the same data (plan 28's polling tick fetches reviews + comments
    in one round trip).
    """
    try:
        owner, name = _split_repo(repo)
    except ValueError as e:
        log.warning("get_latest_reviews: %s", e)
        return None
    cmd = [
        gh_bin,
        "api",
        "graphql",
        "-f",
        f"query={_PR_REVIEWS_QUERY}",
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
        log.warning("_fetch_pr_reviews_payload: subprocess error: %s", e)
        return None
    if r.returncode != 0:
        log.warning(
            "_fetch_pr_reviews_payload: gh exited %d; stderr=%s", r.returncode, (r.stderr or "")[:300]
        )
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as e:
        log.warning("_fetch_pr_reviews_payload: bad json from gh: %s", e)
        return None


def _parse_reviews(payload: dict) -> list[Review]:
    nodes = (
        payload.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviews", {})
        .get("nodes", [])
    )
    if not isinstance(nodes, list):
        return []
    out: list[Review] = []
    for node in nodes:
        try:
            author_login = ((node.get("author") or {}).get("login")) or ""
            db_id = node.get("databaseId")
            out.append(
                Review(
                    review_id=node.get("id") or "",
                    database_id=int(db_id) if db_id is not None else None,
                    state=str(node.get("state") or ""),
                    submitted_at=_parse_iso8601(node.get("submittedAt") or ""),
                    body=str(node.get("body") or ""),
                    author=author_login,
                    is_bot=is_bot_author(author_login),
                )
            )
        except (TypeError, ValueError, KeyError) as e:
            log.warning("_parse_reviews: bad node, skipping: %s", e)
            continue
    out.sort(key=lambda r: r.submitted_at)
    return out


def _parse_comments(payload: dict) -> list[PRComment]:
    nodes = (
        payload.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("comments", {})
        .get("nodes", [])
    )
    if not isinstance(nodes, list):
        return []
    out: list[PRComment] = []
    for node in nodes:
        try:
            author_login = ((node.get("author") or {}).get("login")) or ""
            db_id = node.get("databaseId")
            out.append(
                PRComment(
                    comment_id=node.get("id") or "",
                    database_id=int(db_id) if db_id is not None else None,
                    body=str(node.get("body") or ""),
                    created_at=_parse_iso8601(node.get("createdAt") or ""),
                    author=author_login,
                    is_bot=is_bot_author(author_login),
                )
            )
        except (TypeError, ValueError, KeyError) as e:
            log.warning("_parse_comments: bad node, skipping: %s", e)
            continue
    out.sort(key=lambda c: c.created_at)
    return out


def get_latest_reviews(repo: str, pr_number: int, *, gh_bin: str = "gh") -> list[Review]:
    """Fetch formal GitHub Reviews for a PR via GraphQL.

    Best-effort: returns empty list on any failure. Reviews are sorted by
    submission time ascending, so the caller can `[-1]` for "most recent" or
    iterate from the last processed id.
    """
    payload = _fetch_pr_reviews_payload(repo, pr_number, gh_bin=gh_bin)
    if payload is None:
        return []
    return _parse_reviews(payload)


def bundle_pr_context(
    repo: str,
    pr_number: int,
    *,
    gh_bin: str = "gh",
    max_chars: int = 12000,
) -> str:
    """Render every contextual signal on a PR into one prompt-ready block.

    Plan 28 model: when a CHANGES_REQUESTED review fires (or CI fails), the
    fixup planner needs the full context — every PR-level comment, every
    *unresolved* inline thread, every formal Review's body — bundled so the
    planner can decide what to fix. **Resolved threads are excluded** by
    design (resolved = a human dismissed the AI/bot suggestion → ignore).

    Returns a string suitable for `fixup-planner.md`'s context section, capped
    at `max_chars` so we never blow the agent's prompt budget. On fetch
    failure returns an empty string — the planner falls back to the base
    instructions without external context.
    """
    payload = _fetch_pr_reviews_payload(repo, pr_number, gh_bin=gh_bin)
    reviews = _parse_reviews(payload) if payload else []
    comments = _parse_comments(payload) if payload else []
    threads = get_review_threads(repo, pr_number, gh_bin=gh_bin)
    unresolved = [t for t in threads if not t.is_resolved]

    sections: list[str] = []
    if reviews:
        rendered: list[str] = []
        for r in reviews[-10:]:
            body = (r.body or "").strip().replace("\n", "\n    ")
            if len(body) > 1200:
                body = body[:1200] + "…"
            tag = f"[{r.state}]" + (" [bot]" if r.is_bot else "")
            head = f"  - {tag} by {r.author or '(unknown)'}: "
            rendered.append(head + (body or "(no body)"))
        sections.append("Recent reviews:\n" + "\n".join(rendered))
    if unresolved:
        rendered = []
        for i, t in enumerate(unresolved, 1):
            path_line = f"{t.path or '(no path)'}:{t.line or '?'}"
            body = (t.last_comment_body or "").strip().replace("\n", " ")
            if len(body) > 600:
                body = body[:600] + "…"
            bot_tag = " [bot]" if t.last_comment_is_bot else ""
            rendered.append(
                f"  {i}. [{path_line}] by {t.last_comment_author or '(unknown)'}{bot_tag}: {body}"
            )
        sections.append(
            "Unresolved inline threads (resolved threads omitted by design):\n" + "\n".join(rendered)
        )
    if comments:
        rendered = []
        for c in comments[-10:]:
            body = (c.body or "").strip().replace("\n", "\n    ")
            if len(body) > 800:
                body = body[:800] + "…"
            bot_tag = " [bot]" if c.is_bot else ""
            rendered.append(f"  - by {c.author or '(unknown)'}{bot_tag}: {body}")
        sections.append("PR-level comments:\n" + "\n".join(rendered))

    if not sections:
        return ""
    bundle = "\n\n".join(sections)
    if len(bundle) > max_chars:
        bundle = bundle[: max_chars - 80] + "\n…(truncated; bundle exceeded budget)"
    return bundle


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
