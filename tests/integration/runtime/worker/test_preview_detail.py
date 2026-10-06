""":class:`Worker.set_preview_detail` integration test.

The switch runs on the worker loop and forwards through the hosted
adapter's ``set_preview_detail`` probe; adapters without one (every
non-camera adapter) resolve to ``False``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest

from capa.devices.adapter import Capability
from capa.runtime.errors import UnknownDeviceError
from capa.runtime.runner import InlineRunner, ThreadedRunner, WorkerRunner
from capa.runtime.worker import Worker
from tests.integration.runtime.fakes import make_fake_adapter


@pytest.fixture(params=["inline", "threaded"])
def make_runner(request: pytest.FixtureRequest) -> Callable[[str], WorkerRunner]:
    kind = request.param

    def _factory(name: str) -> WorkerRunner:
        if kind == "inline":
            return InlineRunner(name=name)
        return ThreadedRunner(name=name)

    return _factory


async def _wait(fut: object) -> object:
    return await asyncio.wrap_future(fut)  # type: ignore[arg-type]


class _AdapterWithDetail:
    """Stand-in CameraDeviceAdapter for the preview-detail path. Real
    :class:`CameraDeviceAdapter` forwarding is covered by
    :mod:`tests.unit.runtime.test_camera_adapter_metadata`."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.resource_id = f"fake:{name}"
        self.capabilities: frozenset[Capability] = frozenset()
        self.calls: list[bool] = []

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def start(self, ctx: object) -> None:
        pass

    async def stop(self) -> None:
        pass

    def stream(self) -> AsyncIterator[object]:  # pragma: no cover
        async def _gen() -> AsyncIterator[object]:
            empty: tuple[object, ...] = ()
            for item in empty:
                yield item

        return _gen()

    async def snapshot(self) -> object:
        raise NotImplementedError

    async def command(self, cmd: object) -> object:
        raise NotImplementedError

    def set_preview_detail(self, enabled: bool) -> bool:
        self.calls.append(enabled)
        return True


class TestWorkerSetPreviewDetail:
    @pytest.mark.anyio
    async def test_forwards_to_camera_adapter(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        adapter = _AdapterWithDetail("cam0")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],  # type: ignore[list-item]
            runner=make_runner("detail-cam"),
        )
        await worker.async_start()
        try:
            assert await _wait(worker.set_preview_detail("cam0", True)) is True
            assert await _wait(worker.set_preview_detail("cam0", False)) is True
            assert adapter.calls == [True, False]
        finally:
            await worker.async_close(grace_s=1.0)

    @pytest.mark.anyio
    async def test_false_for_non_camera_adapter(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        adapter = make_fake_adapter("heater")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("detail-noncam"),
        )
        await worker.async_start()
        try:
            assert await _wait(worker.set_preview_detail("heater", True)) is False
        finally:
            await worker.async_close(grace_s=1.0)

    @pytest.mark.anyio
    async def test_unknown_adapter_raises(self, make_runner: Callable[[str], WorkerRunner]) -> None:
        adapter = make_fake_adapter("heater")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("detail-unknown"),
        )
        await worker.async_start()
        try:
            with pytest.raises(UnknownDeviceError):
                await _wait(worker.set_preview_detail("missing", True))
        finally:
            await worker.async_close(grace_s=1.0)
