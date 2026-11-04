"""Cooperative cancellation for one existing-game attachment attempt."""
from __future__ import annotations

import threading


class ConnectionCancelled(ValueError):
    """The caller cancelled; initialization must clean up before returning."""


def check_cancelled(event: threading.Event | None, phase: str) -> None:
    if event is not None and event.is_set():
        raise ConnectionCancelled("Connection cancelled at " + phase)
