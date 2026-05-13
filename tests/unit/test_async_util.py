"""Tests for UI asyncio scheduling helpers."""

from __future__ import annotations

import asyncio
import gc
import warnings

import pytest

from capa.ui import async_util
from capa.ui.async_util import schedule_bg


def test_schedule_bg_closes_coroutine_when_no_loop_is_running() -> None:
    async def work() -> None:
        return None

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)

        task = schedule_bg(work())
        gc.collect()

    assert task is None
    assert [w for w in caught if issubclass(w.category, RuntimeWarning)] == []


@pytest.mark.anyio
async def test_schedule_bg_tracks_task_until_done() -> None:
    ran = asyncio.Event()

    async def work() -> str:
        ran.set()
        return "done"

    task = schedule_bg(work())

    assert task is not None
    assert task in async_util._BG_TASKS
    await asyncio.wait_for(ran.wait(), timeout=1)
    assert await task == "done"

    await asyncio.sleep(0)
    assert task not in async_util._BG_TASKS
