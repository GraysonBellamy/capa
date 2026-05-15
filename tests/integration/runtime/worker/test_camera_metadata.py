""":class:`Worker.camera_metadata` integration test.

Verifies the worker-loop routing for the cross-loop probe: a non-camera
adapter returns None; a camera adapter forwards through its
``camera_metadata()`` capability probe. The state-machine independence
matters here — metadata is a frozen read against pool-open attributes,
so the same call succeeds in IDLE, ARMED, SAMPLING, and even DRAINING /
CLOSED.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from capa.devices.camera.metadata import UvcRangeMetadata, WebcamMetadata
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


class _AdapterWithMetadata:
    """Stand-in CameraDeviceAdapter for the metadata path.

    Implements the slice of the DeviceAdapter Protocol the worker needs
    at open() / close() time plus the new ``camera_metadata`` capability
    probe. Real :class:`CameraDeviceAdapter` integration is covered by
    :mod:`tests.unit.runtime.test_camera_adapter_metadata`.
    """

    def __init__(self, name: str, metadata: WebcamMetadata | None) -> None:
        self.name = name
        self.resource_id = f"fake:{name}"
        self._metadata = metadata
        self.capabilities = frozenset()

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def start(self, ctx: object) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def stream(self):  # pragma: no cover — never iterated in this suite
        if False:
            yield None

    async def snapshot(self) -> object:
        raise NotImplementedError

    async def command(self, cmd: object) -> object:
        raise NotImplementedError

    def camera_metadata(self) -> WebcamMetadata | None:
        return self._metadata


def _sample_metadata() -> WebcamMetadata:
    return WebcamMetadata(
        supported_resolutions=((640, 480),),
        resolution_hint=(640, 480),
        uvc_ranges={
            "set_exposure": UvcRangeMetadata(
                minimum=-13,
                maximum=-1,
                step=1,
                default=-6,
                current=-5,
            ),
        },
    )


class TestWorkerCameraMetadata:
    @pytest.mark.anyio
    async def test_returns_metadata_for_camera_adapter(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        meta = _sample_metadata()
        adapter = _AdapterWithMetadata("cam0", meta)
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],  # type: ignore[list-item]
            runner=make_runner("md-cam"),
        )
        await worker.async_start()
        try:
            out = await _wait(worker.camera_metadata("cam0"))
            assert out is meta
        finally:
            await worker.async_close(grace_s=1.0)

    @pytest.mark.anyio
    async def test_returns_none_for_non_camera_adapter(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        # The standard FakeAdapter has no ``camera_metadata`` attr; the
        # worker's _camera_metadata_impl getattr-probe falls through to
        # None.
        adapter = make_fake_adapter("heater")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("md-noncam"),
        )
        await worker.async_start()
        try:
            out = await _wait(worker.camera_metadata("heater"))
            assert out is None
        finally:
            await worker.async_close(grace_s=1.0)

    @pytest.mark.anyio
    async def test_returns_none_for_camera_without_snapshot(
        self, make_runner: Callable[[str], WorkerRunner]
    ) -> None:
        # IR-camera shape: ``camera_metadata`` exists on the wrapper but
        # the underlying camera lacks ``snapshot_metadata``, so the
        # capability probe returns None.
        adapter = _AdapterWithMetadata("ir_cam0", metadata=None)
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],  # type: ignore[list-item]
            runner=make_runner("md-ir"),
        )
        await worker.async_start()
        try:
            out = await _wait(worker.camera_metadata("ir_cam0"))
            assert out is None
        finally:
            await worker.async_close(grace_s=1.0)

    @pytest.mark.anyio
    async def test_unknown_adapter_raises(self, make_runner: Callable[[str], WorkerRunner]) -> None:
        adapter = make_fake_adapter("heater")
        worker = Worker(
            resource_id=adapter.resource_id,
            adapters=[adapter],
            runner=make_runner("md-unknown"),
        )
        await worker.async_start()
        try:
            with pytest.raises(UnknownDeviceError):
                await _wait(worker.camera_metadata("missing"))
        finally:
            await worker.async_close(grace_s=1.0)
