"""Shared asyncio helpers for the UI layer.

qasync makes Qt's main thread the asyncio event loop, so UI slots can
schedule async work directly. But ``asyncio.Task`` is held only weakly
by the loop (see Python issue python/cpython#88831) — without a strong
reference, the GC can collect a running task. UI fire-and-forget paths
(registry close on config-swap, manual-control dispatch, camera close
on engine-state transition) rely on these helpers to keep references
alive until the task completes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

_BG_TASKS: set[asyncio.Task[Any]] = set()


def schedule_bg(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any] | None:
    """Schedule ``coro`` on the running loop and retain a strong reference
    until it completes.

    Returns the :class:`asyncio.Task` on success, or ``None`` when no
    event loop is running (typical in unit tests that don't construct
    a qasync loop). The unscheduled coroutine is closed before returning
    so the "UI not running" path does not leak RuntimeWarnings. Callers
    can use the ``None`` return to surface a "UI not running" warning
    without raising.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return None
    task = loop.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


__all__ = ["schedule_bg"]
