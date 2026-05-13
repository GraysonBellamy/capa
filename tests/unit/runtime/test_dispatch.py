"""Tests for :mod:`capa.runtime.dispatch` — the three dispatcher impls.

The dispatchers all satisfy :class:`CommandDispatcher`; tests verify each
impl routes correctly and produces the right failure mode on bad inputs.
The conductor-state gate is the most important behaviour to lock down,
since a procedure-issued command landing during DRAINING would race with
adapter.stop().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from capa.devices.adapter import CommandResult, DeviceCommand
from capa.runtime.dispatch import (
    AdapterDispatcher,
    CommandDispatcher,
    ConductorDispatcher,
    ManualClient,
    PoolDispatcher,
    UnknownDeviceError,
)
from capa.runtime.state import ConductorState

pytestmark = pytest.mark.anyio


def _cmd(kind: str = "set_setpoint", value: float = 1.0) -> DeviceCommand:
    return DeviceCommand(
        kind=kind,
        target="heater.setpoint",
        payload={"value": value},
        issued_by="test",
        authorization_id="auth-1",
        confirmed_by=None,
    )


def _ok() -> CommandResult:
    return CommandResult(accepted=True, detail="ok", t_mono_ns=1, t_utc=datetime.now(UTC))


# ---------------------------------------------------------------------------
# AdapterDispatcher
# ---------------------------------------------------------------------------


@dataclass
class _FakeAdapter:
    commands: list[DeviceCommand] = field(default_factory=list)
    raises: BaseException | None = None
    result: CommandResult | None = None

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        self.commands.append(cmd)
        if self.raises is not None:
            raise self.raises
        return self.result or _ok()


class TestAdapterDispatcher:
    def test_satisfies_protocol(self) -> None:
        d = AdapterDispatcher({})
        assert isinstance(d, CommandDispatcher)

    async def test_routes_to_named_adapter(self) -> None:
        a = _FakeAdapter()
        d = AdapterDispatcher({"heater": a})
        cmd = _cmd()
        result = await d.dispatch("heater", cmd)
        assert result.accepted
        assert a.commands == [cmd]

    async def test_unknown_device_raises(self) -> None:
        d = AdapterDispatcher({"heater": _FakeAdapter()})
        with pytest.raises(UnknownDeviceError) as exc_info:
            await d.dispatch("balance", _cmd())
        assert exc_info.value.device == "balance"

    async def test_propagates_adapter_errors(self) -> None:
        a = _FakeAdapter(raises=RuntimeError("serial timeout"))
        d = AdapterDispatcher({"heater": a})
        with pytest.raises(RuntimeError, match="serial timeout"):
            await d.dispatch("heater", _cmd())


# ---------------------------------------------------------------------------
# PoolDispatcher — uses a fake-pool stand-in (we don't need a real pool here)
# ---------------------------------------------------------------------------


class _FakePoolFuture:
    """Resolve a concurrent.futures.Future inline for tests."""

    def __init__(self, result: CommandResult | BaseException) -> None:
        import concurrent.futures as cf

        self._fut: cf.Future[CommandResult] = cf.Future()
        if isinstance(result, BaseException):
            self._fut.set_exception(result)
        else:
            self._fut.set_result(result)

    def future(self):
        return self._fut


@dataclass
class _FakePool:
    """Minimal pool stand-in with a ``dispatch`` method that returns a
    pre-resolved concurrent.futures.Future."""

    results: dict[str, CommandResult | BaseException] = field(default_factory=dict)
    seen: list[tuple[str, DeviceCommand]] = field(default_factory=list)

    def dispatch(self, device: str, cmd: DeviceCommand):
        self.seen.append((device, cmd))
        if device not in self.results:
            raise KeyError(device)
        return _FakePoolFuture(self.results[device]).future()


class TestPoolDispatcher:
    def test_satisfies_protocol(self) -> None:
        d = PoolDispatcher(pool=_FakePool())  # type: ignore[arg-type]
        assert isinstance(d, CommandDispatcher)

    async def test_routes_through_pool_and_returns_result(self) -> None:
        result = _ok()
        pool = _FakePool(results={"heater": result})
        d = PoolDispatcher(pool=pool)  # type: ignore[arg-type]
        out = await d.dispatch("heater", _cmd())
        assert out is result
        assert pool.seen[0][0] == "heater"

    async def test_unknown_device_raises(self) -> None:
        pool = _FakePool(results={})
        d = PoolDispatcher(pool=pool)  # type: ignore[arg-type]
        with pytest.raises(UnknownDeviceError):
            await d.dispatch("missing", _cmd())

    async def test_propagates_pool_side_errors(self) -> None:
        pool = _FakePool(results={"heater": RuntimeError("worker dead")})
        d = PoolDispatcher(pool=pool)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="worker dead"):
            await d.dispatch("heater", _cmd())


# ---------------------------------------------------------------------------
# ConductorDispatcher
# ---------------------------------------------------------------------------


class _FakeConductor:
    """Stand-in exposing only what the dispatcher reads (state + dispatch)."""

    def __init__(self, *, state: ConductorState, pool_results: dict[str, Any]) -> None:
        self.state = state
        self._pool_results = pool_results
        self.dispatch_calls: list[tuple[str, DeviceCommand]] = []

    def dispatch(self, device: str, cmd: DeviceCommand):
        self.dispatch_calls.append((device, cmd))
        if device not in self._pool_results:
            raise KeyError(device)
        return _FakePoolFuture(self._pool_results[device]).future()


class TestConductorDispatcher:
    def test_satisfies_protocol(self) -> None:
        c = _FakeConductor(state=ConductorState.RUNNING, pool_results={})
        d = ConductorDispatcher(conductor=c)  # type: ignore[arg-type]
        assert isinstance(d, CommandDispatcher)

    @pytest.mark.parametrize(
        "state",
        [ConductorState.PREPARING, ConductorState.RUNNING],
    )
    async def test_dispatch_permitted_in_active_states(self, state) -> None:
        c = _FakeConductor(state=state, pool_results={"heater": _ok()})
        d = ConductorDispatcher(conductor=c)  # type: ignore[arg-type]
        result = await d.dispatch("heater", _cmd())
        assert result.accepted
        assert len(c.dispatch_calls) == 1

    @pytest.mark.parametrize(
        "state",
        [
            ConductorState.DRAINING,
            ConductorState.FINALIZING,
            ConductorState.SEALED,
            ConductorState.FAILED,
        ],
    )
    async def test_dispatch_refused_outside_active_states(self, state) -> None:
        from capa.runtime.conductor import ConductorStateError

        c = _FakeConductor(state=state, pool_results={"heater": _ok()})
        d = ConductorDispatcher(conductor=c)  # type: ignore[arg-type]
        with pytest.raises(ConductorStateError):
            await d.dispatch("heater", _cmd())
        # And we don't even reach the conductor's dispatch method.
        assert c.dispatch_calls == []

    async def test_unknown_device_in_running_state_raises(self) -> None:
        c = _FakeConductor(state=ConductorState.RUNNING, pool_results={})
        d = ConductorDispatcher(conductor=c)  # type: ignore[arg-type]
        with pytest.raises(UnknownDeviceError):
            await d.dispatch("missing", _cmd())


# ---------------------------------------------------------------------------
# ManualClient — UI-facing transparent-routing facade
# ---------------------------------------------------------------------------


class _FakeWorkerForCamera:
    """Minimal worker stand-in exposing the ``adapters`` mapping."""

    def __init__(self, adapters: dict[str, Any]) -> None:
        self.adapters = adapters


class _FakePoolWithCameras(_FakePool):
    """Pool stand-in that also fakes ``worker_for(name) -> worker.adapters``.

    Wired separately from the dispatch path so a manual test can assert the
    camera lookup without dragging the full pool API into the suite.
    """

    def __init__(
        self,
        workers_by_device: dict[str, _FakeWorkerForCamera] | None = None,
    ) -> None:
        super().__init__()
        self.workers_by_device = workers_by_device or {}

    def worker_for(self, device: str) -> _FakeWorkerForCamera:
        if device not in self.workers_by_device:
            from capa.runtime.errors import UnknownDeviceError as _RTUnknown

            raise _RTUnknown(device)
        return self.workers_by_device[device]


class TestManualClient:
    async def test_routes_to_pool_when_no_run_armed(self) -> None:
        pool = _FakePool(results={"heater": _ok()})
        client = ManualClient(pool=pool, conductor_provider=lambda: None)  # type: ignore[arg-type]
        result = await client.dispatch("heater", _cmd())
        assert result.accepted
        assert pool.seen[0][0] == "heater"

    async def test_routes_to_conductor_when_run_armed(self) -> None:
        pool = _FakePool(results={"heater": _ok()})  # would be a fallback
        cond = _FakeConductor(state=ConductorState.RUNNING, pool_results={"heater": _ok()})
        client = ManualClient(pool=pool, conductor_provider=lambda: cond)  # type: ignore[arg-type]
        result = await client.dispatch("heater", _cmd())
        assert result.accepted
        # Pool never saw the dispatch because the conductor handled it.
        assert pool.seen == []
        assert len(cond.dispatch_calls) == 1

    @pytest.mark.parametrize(
        "state",
        [
            ConductorState.DRAINING,
            ConductorState.FINALIZING,
            ConductorState.SEALED,
            ConductorState.FAILED,
        ],
    )
    async def test_falls_through_to_pool_when_conductor_not_dispatchable(self, state) -> None:
        # During DRAINING / FINALIZING / SEALED / FAILED the previous run's
        # conductor is "on the way out" — between-runs manual commands must
        # land on the pool, not on a refusing conductor.
        pool = _FakePool(results={"heater": _ok()})
        cond = _FakeConductor(state=state, pool_results={})
        client = ManualClient(pool=pool, conductor_provider=lambda: cond)  # type: ignore[arg-type]
        result = await client.dispatch("heater", _cmd())
        assert result.accepted
        assert pool.seen[0][0] == "heater"
        assert cond.dispatch_calls == []  # never touched

    async def test_unknown_device_raises_via_pool_path(self) -> None:
        pool = _FakePool(results={})
        client = ManualClient(pool=pool, conductor_provider=lambda: None)  # type: ignore[arg-type]
        with pytest.raises(UnknownDeviceError):
            await client.dispatch("missing", _cmd())

    async def test_unknown_device_raises_via_conductor_path(self) -> None:
        pool = _FakePool(results={})
        cond = _FakeConductor(state=ConductorState.RUNNING, pool_results={})
        client = ManualClient(pool=pool, conductor_provider=lambda: cond)  # type: ignore[arg-type]
        with pytest.raises(UnknownDeviceError):
            await client.dispatch("missing", _cmd())

    async def test_snapshot_uses_same_routing(self) -> None:
        # Snapshot vs dispatch share routing; one happy-path check is enough.
        from capa.devices.records import DeviceSnapshot

        snap = DeviceSnapshot(
            adapter="heater",
            device="heater",
            t_mono_ns=0,
            t_utc=datetime.now(UTC),
            healthy=True,
            health="ok",
            fields={},
        )

        # Extend the fake pool with a snapshot method that returns a future.
        class _PoolWithSnap(_FakePool):
            def snapshot(self, device: str):
                return _FakePoolFuture(snap).future()

        pool = _PoolWithSnap()
        client = ManualClient(pool=pool, conductor_provider=lambda: None)  # type: ignore[arg-type]
        out = await client.snapshot("heater")
        assert out is snap

    def test_camera_lookup_returns_none_for_unknown_device(self) -> None:
        pool = _FakePoolWithCameras()
        client = ManualClient(pool=pool, conductor_provider=lambda: None)  # type: ignore[arg-type]
        assert client.camera("missing") is None

    def test_camera_lookup_returns_none_for_non_camera_adapter(self) -> None:
        worker = _FakeWorkerForCamera(adapters={"heater": _FakeAdapter()})
        pool = _FakePoolWithCameras(workers_by_device={"heater": worker})
        client = ManualClient(pool=pool, conductor_provider=lambda: None)  # type: ignore[arg-type]
        assert client.camera("heater") is None

    async def test_camera_metadata_routes_through_pool(self) -> None:
        from capa.devices.camera.metadata import WebcamMetadata

        meta = WebcamMetadata(
            supported_resolutions=((640, 480), (1280, 720)),
            resolution_hint=(1280, 720),
        )

        class _PoolWithMeta(_FakePool):
            def __init__(self, payload: WebcamMetadata | None) -> None:
                super().__init__()
                self._payload = payload
                self.metadata_calls: list[str] = []

            def camera_metadata(self, device: str):
                import concurrent.futures as cf

                self.metadata_calls.append(device)
                fut: cf.Future[WebcamMetadata | None] = cf.Future()
                fut.set_result(self._payload)
                return fut

        pool = _PoolWithMeta(payload=meta)
        client = ManualClient(pool=pool, conductor_provider=lambda: None)  # type: ignore[arg-type]
        out = await client.camera_metadata("visible_cam0")
        assert out is meta
        assert pool.metadata_calls == ["visible_cam0"]

    async def test_camera_metadata_returns_none_for_non_webcam(self) -> None:
        # Worker resolves the name but the adapter has no snapshot — Pool
        # returns None and the client passes it through transparently.
        class _PoolReturningNone(_FakePool):
            def camera_metadata(self, device: str):
                import concurrent.futures as cf

                fut: cf.Future[Any] = cf.Future()
                fut.set_result(None)
                return fut

        pool = _PoolReturningNone()
        client = ManualClient(pool=pool, conductor_provider=lambda: None)  # type: ignore[arg-type]
        assert await client.camera_metadata("ir_cam0") is None

    async def test_camera_metadata_unknown_device_raises(self) -> None:
        # Mirrors the dispatch surface: an unknown device surfaces as
        # UnknownDeviceError (dispatcher-side), not as a silent None.
        # The card's _fetch_and_apply_metadata swallows exceptions
        # before they reach the UI, but the public API stays consistent
        # with dispatch / snapshot.
        class _PoolRaising(_FakePool):
            def camera_metadata(self, device: str):
                raise KeyError(device)

        pool = _PoolRaising()
        client = ManualClient(pool=pool, conductor_provider=lambda: None)  # type: ignore[arg-type]
        with pytest.raises(UnknownDeviceError):
            await client.camera_metadata("missing")
