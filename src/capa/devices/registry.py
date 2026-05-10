""":class:`DeviceRegistry` — shared connection-layer pool for devices + cameras.

Plan §5.2 separates the lifecycle of an adapter into two layers:

* ``open`` / ``close`` is the **connection** layer — opening a serial port,
  binding a USB device, identifying the firmware.
* ``start`` / ``stop`` is the **sampling** layer — arming hardware-clocked
  tasks, beginning a producer stream.

A run owns the *sampling* layer (only the engine knows when to start and stop
producers); the *connection* layer naturally outlives a single run. The
manual control panel needs adapter connections between runs, and re-opening
serial ports on every run-arm cycle pays a measurable cost (the Sartorius
cold-open race can take seconds and occasionally needs a retry).

This registry owns the connection layer. It is constructed once when an
:class:`~capa.experiment.config.ExperimentConfig` is loaded and shared
between the engine (for ``start``/``stop`` of producers) and the manual
control panel (for ``command(...)`` dispatch and one-shot reads). Neither
the engine nor the panel ``close`` adapters — the registry does, on
:meth:`aclose` (typically at config-reload or app-quit).

Idempotency contract:

* :meth:`acquire` returns the same live adapter for the same ``name`` across
  callers. First call constructs + opens; subsequent calls return the cached
  instance. Concurrent first calls are serialized by a per-name lock so a
  device is never opened twice.
* :meth:`release` closes one connection and drops it from the cache. The next
  :meth:`acquire` re-opens.
* :meth:`aclose` closes every cached connection in parallel and clears the
  cache. Safe to call multiple times.

The registry does **not** call ``adapter.start()`` / ``camera.start_recording()``
— those belong to the engine. It also does **not** call
``adapter.configure_channels()``; that is engine-specific wiring of the
channel registry to each adapter.
"""

from __future__ import annotations

from collections.abc import Mapping

import anyio
import structlog

from capa.core.clock import RunClock
from capa.core.errors import CapaError
from capa.devices.adapter import DeviceAdapter
from capa.devices.camera.base import Camera, CameraSpec
from capa.experiment.cameras import construct_cameras as _construct_cameras_list
from capa.experiment.config import DeviceConfig, ExperimentConfig

_logger = structlog.get_logger("capa.devices.registry")


class DeviceRegistryError(CapaError):
    """Raised on unknown device/camera names or on cache-state misuse.

    Adapter open failures bubble up as :class:`~capa.core.errors.AdapterError`
    from the adapter itself — this exception type is reserved for registry
    contract violations (unknown name, use-after-aclose).
    """


class DeviceRegistry:
    """Shared, lazy connection pool for one :class:`ExperimentConfig`.

    Construction is sync and free of I/O — the registry only walks the
    config's device/camera specs and builds an empty cache. Adapters and
    cameras are constructed + opened on first :meth:`acquire` and stay open
    until :meth:`release` (single) or :meth:`aclose` (all).

    Thread-safety: not thread-safe; all callers must run on the same asyncio
    event loop. AnyIO locks serialize concurrent acquires of the same name.
    """

    def __init__(self, config: ExperimentConfig) -> None:
        self._config: ExperimentConfig = config
        self._device_specs: dict[str, DeviceConfig] = {d.name: d for d in config.hardware.devices}
        self._camera_specs: dict[str, CameraSpec] = {c.name: c for c in config.hardware.cameras}
        self._adapters: dict[str, DeviceAdapter] = {}
        self._cameras: dict[str, Camera] = {}
        self._device_locks: dict[str, anyio.Lock] = {}
        self._camera_locks: dict[str, anyio.Lock] = {}
        # A dedicated camera clock for manual-mode opens. Engine-owned
        # acquires pass an explicit clock that anchors to run start; the
        # registry's own clock is used for panel-driven opens between runs,
        # where there is no canonical "run start" yet.
        self._panel_clock: RunClock = RunClock.now()
        self._closed: bool = False

    # ------------------------------------------------------------------ specs

    @property
    def config(self) -> ExperimentConfig:
        return self._config

    @property
    def device_specs(self) -> Mapping[str, DeviceConfig]:
        """Configured devices keyed by name. Iteration order matches
        ``config.hardware.devices``."""
        return self._device_specs

    @property
    def camera_specs(self) -> Mapping[str, CameraSpec]:
        """Configured cameras keyed by name. Iteration order matches
        ``config.hardware.cameras``."""
        return self._camera_specs

    def is_device_open(self, name: str) -> bool:
        return name in self._adapters

    def is_camera_open(self, name: str) -> bool:
        return name in self._cameras

    def opened_device(self, name: str) -> DeviceAdapter | None:
        """Return the live adapter if already open, else ``None``. No I/O."""
        return self._adapters.get(name)

    def opened_camera(self, name: str) -> Camera | None:
        return self._cameras.get(name)

    # ------------------------------------------------------------------ acquire / release

    async def acquire_device(self, name: str) -> DeviceAdapter:
        """Return a live, opened adapter for ``name``.

        First call constructs the adapter via the spec's ``adapter`` import
        path and awaits :meth:`DeviceAdapter.open`. Subsequent calls return
        the cached adapter without re-opening. Concurrent first calls are
        serialized by a per-name lock.

        Raises :class:`DeviceRegistryError` if the registry has been
        :meth:`aclose`-d or if ``name`` is not configured. Adapter-side open
        failures (``AdapterError``) propagate to the caller; the failing
        adapter is *not* cached, so the next :meth:`acquire_device` retries
        from scratch.
        """
        self._require_open()
        if name not in self._device_specs:
            raise DeviceRegistryError(
                f"unknown device {name!r}; configured: {sorted(self._device_specs)}"
            )
        # Fast path: already open. Avoid even taking the lock.
        cached = self._adapters.get(name)
        if cached is not None:
            return cached
        lock = self._device_locks.setdefault(name, anyio.Lock())
        async with lock:
            # Re-check under the lock — a concurrent caller may have opened
            # while we were waiting.
            cached = self._adapters.get(name)
            if cached is not None:
                return cached
            spec = self._device_specs[name]
            adapter = _construct_one_adapter(spec)
            try:
                await adapter.open()
            except BaseException:
                # Never cache a half-opened adapter. The next acquire is
                # free to retry — operator may have unplugged + replugged
                # the device in the meantime.
                _logger.warning("registry.device_open_failed", name=name, adapter=spec.adapter)
                raise
            self._adapters[name] = adapter
            _logger.info("registry.device_opened", name=name, adapter=spec.adapter)
            return adapter

    async def acquire_camera(self, name: str, *, clock: RunClock | None = None) -> Camera:
        """Return a live, opened camera for ``name``.

        ``clock`` is the run clock used to stamp frame timestamps; if
        ``None``, the registry's panel-mode clock is used. The engine passes
        its own per-run clock so frame timestamps anchor to run start; the
        manual panel passes ``None`` because between runs there is no
        canonical run-start anchor — the panel never records, so the clock
        is only used for command timestamps.

        Cameras differ from devices: opening also returns identifying info
        (:class:`CameraInfo`). The registry discards the info return —
        callers wanting it should call :meth:`Camera.snapshot` after acquire.
        """
        self._require_open()
        if name not in self._camera_specs:
            raise DeviceRegistryError(
                f"unknown camera {name!r}; configured: {sorted(self._camera_specs)}"
            )
        cached = self._cameras.get(name)
        if cached is not None:
            return cached
        lock = self._camera_locks.setdefault(name, anyio.Lock())
        async with lock:
            cached = self._cameras.get(name)
            if cached is not None:
                return cached
            spec = self._camera_specs[name]
            camera = _construct_one_camera(spec, clock=clock or self._panel_clock)
            try:
                await camera.open()
            except BaseException:
                _logger.warning(
                    "registry.camera_open_failed",
                    name=name,
                    adapter=spec.adapter,
                )
                raise
            self._cameras[name] = camera
            _logger.info("registry.camera_opened", name=name, adapter=spec.adapter)
            return camera

    async def acquire_all_devices(self) -> dict[str, DeviceAdapter]:
        """Open every configured device in parallel and return the full map.

        Best-effort: if any open fails the partially-opened set stays in the
        cache (so an operator can use the ones that did open from the manual
        panel). The caller decides what to do with the exception — the engine
        treats partial failure as a fatal preflight error and calls
        :meth:`aclose`; the panel just surfaces it on the failing card.
        """
        self._require_open()
        async with anyio.create_task_group() as tg:
            for name in self._device_specs:
                if name in self._adapters:
                    continue
                tg.start_soon(self.acquire_device, name)
        # acquire_device cached as it went; build a stable-ordered dict.
        return {n: self._adapters[n] for n in self._device_specs if n in self._adapters}

    async def acquire_all_cameras(self, *, clock: RunClock | None = None) -> dict[str, Camera]:
        self._require_open()

        async def _one(n: str) -> None:
            await self.acquire_camera(n, clock=clock)

        async with anyio.create_task_group() as tg:
            for name in self._camera_specs:
                if name in self._cameras:
                    continue
                tg.start_soon(_one, name)
        return {n: self._cameras[n] for n in self._camera_specs if n in self._cameras}

    async def release_device(self, name: str) -> None:
        """Close one adapter and drop it from the cache. Idempotent."""
        adapter = self._adapters.pop(name, None)
        if adapter is None:
            return
        try:
            await adapter.close()
        except Exception as exc:
            _logger.warning("registry.device_close_failed", name=name, error=str(exc))

    async def release_camera(self, name: str) -> None:
        camera = self._cameras.pop(name, None)
        if camera is None:
            return
        try:
            await camera.close()
        except Exception as exc:
            _logger.warning("registry.camera_close_failed", name=name, error=str(exc))

    async def aclose(self) -> None:
        """Close every cached connection in parallel and mark the registry
        unusable for further :meth:`acquire_device` / :meth:`acquire_camera`
        calls. Idempotent."""
        if self._closed and not self._adapters and not self._cameras:
            return
        self._closed = True
        adapters = list(self._adapters.values())
        cameras = list(self._cameras.values())
        self._adapters.clear()
        self._cameras.clear()
        # Best-effort parallel close. A misbehaving adapter must not prevent
        # the rest from cleaning up.
        async with anyio.create_task_group() as tg:
            for adapter in adapters:
                tg.start_soon(_safe_close_adapter, adapter)
            for camera in cameras:
                tg.start_soon(_safe_close_camera, camera)

    # ------------------------------------------------------------------ internal

    def _require_open(self) -> None:
        if self._closed:
            raise DeviceRegistryError(
                "DeviceRegistry has been closed; construct a new registry after a config reload"
            )


async def _safe_close_adapter(adapter: DeviceAdapter) -> None:
    try:
        await adapter.close()
    except Exception as exc:
        _logger.warning(
            "registry.aclose.adapter_failed",
            name=getattr(adapter, "name", "?"),
            error=str(exc),
        )


async def _safe_close_camera(camera: Camera) -> None:
    try:
        await camera.close()
    except Exception as exc:
        _logger.warning(
            "registry.aclose.camera_failed",
            name=getattr(getattr(camera, "spec", None), "name", "?"),
            error=str(exc),
        )


def _construct_one_adapter(spec: DeviceConfig) -> DeviceAdapter:
    """Instantiate one adapter from a :class:`DeviceConfig`.

    Mirrors :func:`capa.experiment.engine._construct_adapters` for a single
    entry. Kept here so the registry has no circular import on
    :mod:`capa.experiment.engine`.
    """
    from capa.experiment.engine import (  # noqa: PLC0415 — cycle-breaker
        _import_adapter_class,
    )

    cls = _import_adapter_class(spec.adapter)
    from_params = getattr(cls, "from_params", None)
    try:
        if callable(from_params):
            adapter: DeviceAdapter = from_params(name=spec.name, **spec.params)
        else:
            adapter = cls(name=spec.name, **spec.params)
    except TypeError as exc:
        raise DeviceRegistryError(
            f"failed to construct adapter {spec.name!r} ({spec.adapter}): {exc}"
        ) from exc
    return adapter


def _construct_one_camera(spec: CameraSpec, *, clock: RunClock) -> Camera:
    """Instantiate one camera. Reuses
    :func:`capa.experiment.cameras.construct_cameras` so the construction
    rules (``from_params`` precedence, kind cross-check) stay in one place.
    """
    # construct_cameras walks the whole config; build a tiny wrapper config
    # with a one-element cameras tuple to reuse it without copying logic.
    out = _construct_cameras_list(
        _SingleCameraConfig(spec),  # type: ignore[arg-type]
        clock=clock,
    )
    if not out:
        raise DeviceRegistryError(f"camera {spec.name!r}: construction returned no camera")
    return out[0]


class _SingleCameraConfig:
    """Minimal ExperimentConfig-shaped shim for reusing ``construct_cameras``
    on a single :class:`CameraSpec`.

    ``construct_cameras`` only reads ``config.hardware.cameras`` so the shim
    only needs that attribute. Keeping the duck-type narrow here avoids
    instantiating a full ExperimentConfig (which would require a procedure
    and other unrelated fields) just to open one camera.
    """

    __slots__ = ("hardware",)

    def __init__(self, spec: CameraSpec) -> None:
        self.hardware = _SingleCameraHardware(spec)


class _SingleCameraHardware:
    __slots__ = ("cameras",)

    def __init__(self, spec: CameraSpec) -> None:
        self.cameras: tuple[CameraSpec, ...] = (spec,)


__all__ = ["DeviceRegistry", "DeviceRegistryError"]
