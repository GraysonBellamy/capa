"""Tests for :class:`LifecycleRegistry`.

The registry is small but load-bearing: the ShutdownCoordinator iterates
its snapshot to decide what to cancel vs await. The interesting cases
are auto-unregister on done, and the critical/non-critical partition.
"""

from __future__ import annotations

import asyncio

import pytest

from capa.ui.lifecycle import LifecycleEntry, LifecycleKind, LifecycleRegistry


@pytest.mark.anyio
async def test_register_returns_entry_and_records_in_snapshot() -> None:
    registry = LifecycleRegistry()

    async def _noop() -> None:
        await asyncio.sleep(10)

    task = asyncio.create_task(_noop())
    try:
        entry = registry.register(LifecycleKind.RUN, "run", task, critical=True)
        assert isinstance(entry, LifecycleEntry)
        assert entry.kind is LifecycleKind.RUN
        assert entry.name == "run"
        assert entry.critical is True
        assert entry in registry.snapshot()
        assert len(registry) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.anyio
async def test_completed_task_self_unregisters() -> None:
    registry = LifecycleRegistry()

    async def _fast() -> None:
        return

    task = asyncio.create_task(_fast())
    registry.register(LifecycleKind.STATE_POLL, "poll", task, critical=False)
    await task
    # Let the done callback fire.
    await asyncio.sleep(0)
    assert len(registry) == 0


@pytest.mark.anyio
async def test_by_kind_filters() -> None:
    registry = LifecycleRegistry()
    tasks = []

    async def _wait() -> None:
        await asyncio.sleep(10)

    try:
        t1 = asyncio.create_task(_wait())
        tasks.append(t1)
        registry.register(LifecycleKind.RUN, "run", t1)
        t2 = asyncio.create_task(_wait())
        tasks.append(t2)
        registry.register(LifecycleKind.PREVIEW_DRAIN, "pv", t2, critical=False)
        t3 = asyncio.create_task(_wait())
        tasks.append(t3)
        registry.register(LifecycleKind.PREVIEW_DRAIN, "pv2", t3, critical=False)

        runs = registry.by_kind(LifecycleKind.RUN)
        previews = registry.by_kind(LifecycleKind.PREVIEW_DRAIN)
        assert len(runs) == 1
        assert len(previews) == 2
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.anyio
async def test_unregister_removes_entry() -> None:
    registry = LifecycleRegistry()

    async def _wait() -> None:
        await asyncio.sleep(10)

    task = asyncio.create_task(_wait())
    try:
        entry = registry.register(LifecycleKind.MANUAL_COMMAND, "cmd", task)
        assert len(registry) == 1
        registry.unregister(entry)
        assert len(registry) == 0
        # Second unregister is a no-op.
        registry.unregister(entry)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
