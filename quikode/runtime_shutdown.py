"""Process-local shutdown flag shared by the CLI and worker threads."""

from __future__ import annotations

import threading

_STOP_REQUESTED = threading.Event()


class ShutdownRequested(RuntimeError):
    """Raised when a worker should discard partial results during shutdown."""


def request_stop() -> None:
    _STOP_REQUESTED.set()


def clear_stop() -> None:
    _STOP_REQUESTED.clear()


def stop_requested() -> bool:
    return _STOP_REQUESTED.is_set()
