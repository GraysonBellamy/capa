"""Lifecycle task registry for the :class:`RunController` and shutdown.

The GUI runs several non-trivial tasks on the qasync loop that are not
"fire and forget" in the loose sense — they hold references to the pool,
the conductor, or open bridges, and the
:class:`~capa.ui.shutdown.ShutdownCoordinator` needs to know about each
one so it can either await it (critical) or cancel it (non-critical)
before calling ``pool.shutdown_close()``.

Pool open, old-pool close, preview drainers, the run task, and the
conductor-state poll are all registered here; the coordinator drains
the registry by category instead of guessing what to wait for.

The :class:`LifecycleKind` enum distinguishes the lifecycle classes the
coordinator treats differently:

* :attr:`LifecycleKind.POOL_OPEN`, :attr:`LifecycleKind.OLD_POOL_CLOSE`,
  :attr:`LifecycleKind.RUN` — *critical*. Cancellable but the
  coordinator awaits them with a bounded ``wait_for`` so an in-flight
  pool open can't race shutdown into a half-opened state.
* :attr:`LifecycleKind.PREVIEW_DRAIN`, :attr:`LifecycleKind.STATE_POLL`,
  :attr:`LifecycleKind.MANUAL_COMMAND` — *non-critical*. The coordinator
  cancels them and moves on; the awaits don't block the shutdown sequence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog

_logger = structlog.get_logger("capa.ui.lifecycle")


class LifecycleKind(StrEnum):
    """Lifecycle-task category. Shapes the coordinator's drain order.

    The coordinator's cancel-lifecycle stage iterates entries in
    enum-declaration order — kinds listed first are cancelled first so
    e.g. manual commands stop dispatching before the run task is asked
    to exit.
    """

    MANUAL_COMMAND = "manual_command"
    DISCOVERY = "discovery"
    PREVIEW_DRAIN = "preview_drain"
    STATE_POLL = "state_poll"
    POOL_OPEN = "pool_open"
    OLD_POOL_CLOSE = "old_pool_close"
    RUN = "run"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True, slots=True)
class LifecycleEntry:
    """One registered task. The registry holds these; the coordinator
    iterates a snapshot of the registry.

    ``critical`` distinguishes "the coordinator must await this with a
    bounded wait_for" (True) from "cancel and move on" (False).
    """

    kind: LifecycleKind
    name: str
    task: asyncio.Task[Any]
    critical: bool


class LifecycleRegistry:
    """In-memory registry of lifecycle tasks owned by the controller.

    Thread-safety: this lives on the qasync loop. Every
    :meth:`register` / :meth:`unregister` happens from a slot or a
    callback that fires on that loop, so a plain ``dict`` suffices —
    no lock required.

    The registry is intentionally minimal: it stores entries, fires
    callbacks on done so entries auto-unregister, and exposes a
    snapshot for the coordinator. By design, this is not a
    general-purpose task supervisor.
    """

    def __init__(self) -> None:
        self._entries: dict[int, LifecycleEntry] = {}

    def register(
        self,
        kind: LifecycleKind,
        name: str,
        task: asyncio.Task[Any],
        *,
        critical: bool = True,
    ) -> LifecycleEntry:
        """Add a task; auto-removes on completion.

        The registry holds a strong reference to the task — the
        controller does not need to keep its own. This is the single
        source of truth for lifecycle-critical tasks.
        """
        entry = LifecycleEntry(kind=kind, name=name, task=task, critical=critical)
        key = id(task)
        self._entries[key] = entry

        def _on_done(_t: asyncio.Task[Any]) -> None:
            self._entries.pop(key, None)

        task.add_done_callback(_on_done)
        _logger.debug(
            "lifecycle.register",
            kind=kind.value,
            name=name,
            critical=critical,
        )
        return entry

    def unregister(self, entry: LifecycleEntry) -> None:
        """Drop an entry by task identity. Idempotent."""
        self._entries.pop(id(entry.task), None)

    def snapshot(self) -> tuple[LifecycleEntry, ...]:
        """Snapshot every live entry. The coordinator iterates this.

        Done tasks self-unregister via the registration callback, so a
        snapshot is effectively "tasks still running right now."
        """
        return tuple(self._entries.values())

    def by_kind(self, kind: LifecycleKind) -> tuple[LifecycleEntry, ...]:
        """Snapshot every live entry of one kind. Used by the
        coordinator to e.g. await the run task specifically."""
        return tuple(e for e in self._entries.values() if e.kind == kind)

    def __iter__(self) -> Iterator[LifecycleEntry]:
        return iter(self.snapshot())

    def __len__(self) -> int:
        return len(self._entries)


__all__ = [
    "LifecycleEntry",
    "LifecycleKind",
    "LifecycleRegistry",
]
