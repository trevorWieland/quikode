"""Plan 59 fix B: helper for wiring agent_call status visibility.

The agents-layer `_run_with_retry` flips `agent_calls.status` to
`backoff_auth` / `backoff_container` (and back to `running`) via the
`agent_call_status_callback` ContextVar so the TUI sees fine-grained
in-flight state without threading a callback through every transport
method's signature.

`agent_call_status_scope_for(store, call_id)` returns the context
manager workers wrap their `agent.invoke(...)` call in. The bound
callback updates the matching `agent_calls.status` column for the
worker's most-recent start-marker row.
"""

from __future__ import annotations

import logging
from typing import Any

from quikode.agents.json_protocol import agent_call_status_scope

log = logging.getLogger("quikode.workers.agent_call_status")


def agent_call_status_scope_for(store: Any, call_id: int) -> agent_call_status_scope:
    """Return a context manager that flips `agent_calls.status` while
    `_run_with_retry` is sleeping between auth-refresh / container
    retries.

    `store` is the Store instance the caller will eventually update
    via `record_agent_call_finished`; `call_id` is the row id returned
    by the matching `record_agent_call_started`. The bound callback
    swallows any update failure (logging only) — the agent call must
    not be derailed by a status-column write that races against a
    concurrent reader.
    """

    def _update(status: str) -> None:
        try:
            store.update_agent_call_status(call_id, status)
        except Exception as exc:
            log.warning("update_agent_call_status(call_id=%d, %s) raised %s", call_id, status, exc)

    return agent_call_status_scope(_update)


__all__ = ["agent_call_status_scope_for"]
