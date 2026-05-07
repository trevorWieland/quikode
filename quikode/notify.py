"""ntfy.sh notification delivery (plan 30).

A task hits AWAITING_REVIEW for ≥ `cfg.review_ready_settle_s` and the
review-watcher fires `notify_review_ready(...)`. ntfy is the only channel —
plan 28 retired the previous slack/multi-channel surface by user decision;
plan 30 reintroduces it ntfy-only on the same signal that gates stacked-diff
dependent kickoff (one threshold, two consumers).

Best-effort: no exception escapes; the orchestrator continues if delivery
fails. ntfy doesn't require auth for public topics, but the topic itself
is a secret (anyone who knows it can publish + read).
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("quikode.notify")


@dataclass(frozen=True)
class ReviewReadyMessage:
    """Inputs for one ntfy push."""

    task_id: str
    title: str  # task title (DAG node title)
    pr_url: str  # canonical PR url; ntfy "Click" header points here
    summary: str  # short body — "settled 18min · 2 review round(s) · CI green"


def notify_review_ready(
    *, ntfy_url: str, ntfy_topic: str, msg: ReviewReadyMessage, timeout_s: float = 10.0
) -> bool:
    """Post one ntfy notification for a review-ready-settled task.

    Returns True iff the publish succeeded (HTTP 2xx). Empty topic is a
    no-op returning False — caller should gate on `cfg.notify_ntfy_topic`
    being set rather than rely on the no-op behavior.
    """
    if not ntfy_topic.strip():
        return False
    url = f"{ntfy_url.rstrip('/')}/{ntfy_topic.strip()}"
    title = f"{msg.task_id}: ready for review"
    body = f"{msg.title}\n\n{msg.summary}\n\n{msg.pr_url}".strip()
    headers = {
        "Title": title,
        "Priority": "default",
        "Tags": "eyes,thumbsup",
    }
    if msg.pr_url:
        headers["Click"] = msg.pr_url
    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            if 200 <= r.status < 300:
                log.info("ntfy posted for task %s -> %s", msg.task_id, url)
                return True
            log.warning("ntfy non-2xx for task %s: %d", msg.task_id, r.status)
            return False
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        log.warning("ntfy delivery failed for task %s: %s", msg.task_id, e)
        return False
