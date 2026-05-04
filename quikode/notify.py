"""Settled-task notifications — ping the operator when a task is review-ready.

Two backends:
- ntfy (https://ntfy.sh, zero-auth, iOS/Android push apps)
- Slack incoming webhook

Both backends:
- Best-effort: HTTP failures are logged + swallowed; the orchestrator never
  raises into the daemon loop.
- Retry-once-with-backoff: a transient 503 / connection reset gets a single
  retry after 5s before giving up.
- Stateless: the caller (daemon) is responsible for tracking
  `tasks.last_notified_settled_ts` and gating re-notify on state transitions.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import Config

log = logging.getLogger("quikode.notify")

_HTTP_TIMEOUT_S = 10
_RETRY_BACKOFF_S = 5


@dataclass(frozen=True)
class SettledMessage:
    """Payload for a settled-task notification.

    Backends format this differently (ntfy uses HTTP headers + body;
    Slack wraps in a JSON block-kit message), so the dataclass keeps the
    fields canonical and lets each backend render its own shape.
    """

    task_id: str
    title: str
    pr_url: str
    summary: str  # one-line state summary
    cost_usd: float | None = None

    def short_text(self) -> str:
        """Plain-text body suitable for ntfy / SMS-style channels."""
        lines = [
            f"{self.task_id}: {self.title}",
            self.pr_url,
            self.summary,
        ]
        if self.cost_usd is not None:
            lines.append(f"Cost so far: ${self.cost_usd:.2f}")
        return "\n".join(lines)


# ---------- backends ----------


def _http_post(
    url: str,
    *,
    body: bytes,
    headers: dict[str, str] | None = None,
    timeout_s: int = _HTTP_TIMEOUT_S,
) -> bool:
    """Single HTTP POST with one retry on transient failure. Returns True on
    any 2xx, False otherwise."""
    headers = headers or {}
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                if 200 <= resp.status < 300:
                    return True
                log.warning("notify POST %s returned status=%d (attempt %d)", url, resp.status, attempt)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            log.warning("notify POST %s raised %s: %s (attempt %d)", url, type(e).__name__, e, attempt)
        if attempt == 1:
            time.sleep(_RETRY_BACKOFF_S)
    return False


def ntfy_send(cfg: Config, msg: SettledMessage) -> bool:
    """POST a notification to ntfy. Title goes in the X-Title header
    so the push notification's banner shows it; body is plain text.
    Returns True on delivery."""
    if not cfg.notify_ntfy_topic:
        log.warning("ntfy notify requested but cfg.notify_ntfy_topic is empty")
        return False
    base = cfg.notify_ntfy_url.rstrip("/")
    url = f"{base}/{cfg.notify_ntfy_topic}"
    title = f"✅ {msg.task_id} ready for review"
    body = msg.short_text().encode("utf-8")
    headers = {
        "Title": title,
        "Click": msg.pr_url or "",
        "Tags": "white_check_mark",
        "Priority": "default",
    }
    return _http_post(url, body=body, headers=headers)


def slack_send(cfg: Config, msg: SettledMessage) -> bool:
    """POST a notification to a Slack incoming webhook. Returns True on
    delivery. Slack expects JSON `{text: "..."}` (and optionally
    block-kit blocks); we use the simple text form for reliability."""
    if not cfg.notify_slack_webhook_url:
        log.warning("slack notify requested but cfg.notify_slack_webhook_url is empty")
        return False
    pr_link = f"<{msg.pr_url}|{msg.task_id}>" if msg.pr_url else msg.task_id
    text = f"✅ {pr_link} ready for review — {msg.title}\n{msg.summary}"
    if msg.cost_usd is not None:
        text += f" · ${msg.cost_usd:.2f}"
    body = json.dumps({"text": text}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    return _http_post(cfg.notify_slack_webhook_url, body=body, headers=headers)


# ---------- multi-channel dispatch ----------


def notify_settled(cfg: Config, msg: SettledMessage) -> bool:
    """Dispatch via the configured channel(s). Returns True if at least
    one backend delivered. The daemon stamps last_notified_settled_ts
    only on a True return; on False it'll retry next poll tick."""
    channel = cfg.notify_settled_channel
    if channel == "none":
        log.debug("notify_settled: channel='none', skipping")
        return False
    delivered: list[str] = []
    if channel in ("ntfy", "both") and ntfy_send(cfg, msg):
        delivered.append("ntfy")
    if channel in ("slack", "both") and slack_send(cfg, msg):
        delivered.append("slack")
    if delivered:
        log.info(
            "notify_settled %s: delivered via %s",
            msg.task_id,
            ",".join(delivered),
        )
        return True
    log.warning("notify_settled %s: NO channel delivered (channel=%s)", msg.task_id, channel)
    return False
