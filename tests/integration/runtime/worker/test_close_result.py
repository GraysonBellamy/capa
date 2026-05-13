"""Tests for :class:`WorkerCloseResult` and bounded ``adapter.close()``.

``Worker.close()`` returns a structured result instead of raising on
adapter-level errors. Per-adapter ``close()`` is bounded by
``adapter_close_grace_s``.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from capa.runtime.runner import ThreadedRunner
from capa.runtime.shutdown import WorkerCloseResult, WorkerShutdownConfig
from capa.runtime.worker import Worker
from tests.integration.runtime.fakes import (
    make_fake_adapter,
    make_hanging_close_adapter,
)


async def _wait(fut: object) -> object:
    return await asyncio.wrap_future(fut)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_close_returns_clean_result_on_happy_path() -> None:
    adapter = make_fake_adapter("a")
    worker = Worker(
        resource_id=adapter.resource_id,
        adapters=[adapter],
        runner=ThreadedRunner(name="close-clean"),
    )
    await _wait(worker.start())
    result = await _wait(worker.close(grace_s=1.0))
    assert isinstance(result, WorkerCloseResult)
    assert result.resource_id == adapter.resource_id
    assert result.adapter_stop_errors == ()
    assert result.adapter_close_errors == ()
    assert result.disarm_result is None  # never disarmed
    assert result.runner_stop.joined is True
    assert result.state_before == "idle"


@pytest.mark.anyio
async def test_close_bounds_hanging_adapter_and_captures_timeout() -> None:
    """``adapter.close()`` is wrapped in ``asyncio.wait_for``; a hanging
    close becomes a timeout error in the result, not a wedged worker."""
    adapter = make_hanging_close_adapter("hangs-close")
    cfg = WorkerShutdownConfig(
        adapter_close_grace_s=0.2,
        runner_stop_grace_s=1.0,
    )
    worker = Worker(
        resource_id=adapter.resource_id,
        adapters=[adapter],
        runner=ThreadedRunner(name="close-bounded"),
        shutdown_config=cfg,
    )
    await _wait(worker.start())

    t0 = time.monotonic()
    result = await _wait(worker.close(grace_s=1.0))
    elapsed = time.monotonic() - t0

    assert isinstance(result, WorkerCloseResult)
    assert len(result.adapter_close_errors) == 1
    err = result.adapter_close_errors[0]
    assert "timeout" in err
    assert "hangs-close" in err
    # Bound: close timeout (0.2) + runner stop join (1.0) + slack.
    assert elapsed < 2.5, f"bounded close took {elapsed:.2f}s"
    # The runner thread itself joined cleanly — the hung close was
    # cancelled by wait_for.
    assert result.runner_stop.joined is True


@pytest.mark.anyio
async def test_close_attempts_every_adapter_even_when_first_times_out() -> None:
    """A timeout on one adapter must not skip the others — every
    adapter is given the chance to release its bus."""
    hangs = make_hanging_close_adapter("hangs")
    ok = make_fake_adapter("ok")
    # Co-host on the same worker (shared resource_id).
    hangs.resource_id = "sim:shared"
    ok.resource_id = "sim:shared"
    cfg = WorkerShutdownConfig(
        adapter_close_grace_s=0.2,
        runner_stop_grace_s=1.0,
    )
    worker = Worker(
        resource_id="sim:shared",
        adapters=[hangs, ok],
        runner=ThreadedRunner(name="close-mixed"),
        shutdown_config=cfg,
    )
    await _wait(worker.start())
    result = await _wait(worker.close(grace_s=1.0))

    assert isinstance(result, WorkerCloseResult)
    # The good adapter still got its close called (close iterates in
    # reverse, so "ok" closes after "hangs" times out).
    assert ok.close_calls == 1
    # The hung adapter's timeout is captured.
    assert any("hangs" in e and "timeout" in e for e in result.adapter_close_errors)
