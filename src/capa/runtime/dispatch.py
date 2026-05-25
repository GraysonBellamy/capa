""":class:`CommandDispatcher` — the executor's outbound command surface.

Today's :class:`~capa.experiment.executor.MethodExecutor` holds a
``dict[str, DeviceAdapter]`` and calls ``await adapter.command(cmd)``
directly. That direct call is the single most contended thing in the
single-loop model — it serializes on whichever event loop the adapter was
constructed in, regardless of which thread the caller is on.

The :class:`CommandDispatcher` indirection lets the executor (and any
other procedure-side dispatcher) route a command through whichever
concurrency layer is appropriate:

* :class:`AdapterDispatcher` — direct, in-loop. Surviving as a thin
  helper for tests that don't want to spin up a full pool; production
  paths no longer use it.
* :class:`PoolDispatcher` — routes through a :class:`WorkerPool` (and
  therefore through a :class:`ThreadBridge` to the resource's worker).
  Used for manual control between runs and by tests that want to
  exercise the pool directly.
* :class:`ConductorDispatcher` — adds run-time state gating on top of
  :class:`PoolDispatcher`: commands are refused outside PREPARING /
  RUNNING. Used by the :class:`ProcedureRunner` so a procedure-issued
  command landing during DRAINING fails fast instead of silently racing
  with shutdown.
* :class:`ManualClient` — single sync facade for UI manual cards.
  Routes to :class:`Conductor.dispatch` when a run is armed (records
  into the bundle, gates by conductor state), else to
  :class:`WorkerPool.dispatch` (no gating, no bundle event). Cards take
  a :class:`ManualClient` reference and remain unaware of which side is
  in use.

All implementations satisfy the same async :meth:`dispatch` contract,
so the executor code path stays uniform across run-armed and idle states.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

from capa.runtime.camera_adapter import CameraDeviceAdapter
from capa.runtime.conductor import Conductor
from capa.runtime.errors import ConductorStateError, UnknownDeviceError

if TYPE_CHECKING:
    from capa.devices.adapter import CommandResult, DeviceAdapter, DeviceCommand
    from capa.devices.camera.base import Camera
    from capa.devices.camera.metadata import WebcamMetadata
    from capa.devices.records import DeviceEmission
    from capa.runtime.pool import WorkerPool


_logger = structlog.get_logger("capa.runtime.dispatch")


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CommandDispatcher(Protocol):
    """Executor-facing async command dispatch.

    The single method is loop-agnostic at the contract level: an
    implementation may run the command in the caller's loop (direct adapter
    call), on a worker thread loop (pool / conductor), or in a future
    subprocess. The caller only sees an awaitable that resolves to a
    :class:`CommandResult` or raises.
    """

    async def dispatch(self, device: str, cmd: DeviceCommand) -> CommandResult:
        """Route ``cmd`` to ``device`` and return its result.

        Failures surface as the underlying exception (e.g. an adapter
        protocol error, a worker-side ``WorkerStateError``, or a
        :class:`ConductorStateError` for state-gated paths).
        """
        ...


# ---------------------------------------------------------------------------
# AdapterDispatcher — direct in-loop call
# ---------------------------------------------------------------------------


class AdapterDispatcher:
    """Direct ``adapter.command(cmd)`` dispatch.

    The adapter map is built at run-start and stays alive for the run;
    the dispatcher holds it by reference and does **not** mutate.
    """

    __slots__ = ("_adapters",)

    def __init__(self, adapters: Mapping[str, DeviceAdapter]) -> None:
        self._adapters = adapters

    async def dispatch(self, device: str, cmd: DeviceCommand) -> CommandResult:
        """Call ``adapter.command(cmd)`` directly on the adapter's own loop.

        Raises:
            UnknownDeviceError: ``device`` is not in the adapter map.
        """
        try:
            adapter = self._adapters[device]
        except KeyError as exc:
            raise UnknownDeviceError(device, configured_names=tuple(self._adapters)) from exc
        return await adapter.command(cmd)


# ---------------------------------------------------------------------------
# PoolDispatcher — routes through the worker pool
# ---------------------------------------------------------------------------


class PoolDispatcher:
    """Dispatch a command through a :class:`WorkerPool`.

    The pool's :meth:`WorkerPool.dispatch` returns a
    :class:`concurrent.futures.Future` (the worker schedules the command
    on its own loop and resolves the future when done). We wrap it with
    :func:`asyncio.wrap_future` so the caller's loop can await it
    cleanly — this is the cross-thread bridge.

    No engine-state gating. Suitable for manual-control-between-runs
    and for tests; production run-time dispatch should use
    :class:`ConductorDispatcher` so state-after-stop commands are
    refused promptly.
    """

    __slots__ = ("_pool",)

    def __init__(self, pool: WorkerPool) -> None:
        self._pool = pool

    async def dispatch(self, device: str, cmd: DeviceCommand) -> CommandResult:
        """Submit through the worker pool and await the worker's result.

        Raises:
            UnknownDeviceError: ``device`` is not owned by any worker in
                the pool.
        """
        try:
            fut = self._pool.dispatch(device, cmd)
        except KeyError as exc:
            raise UnknownDeviceError(device) from exc
        # Loop-agnostic: if the caller is on the worker loop's thread (which
        # is impossible by construction — workers own their own thread), this
        # would still work; in practice the worker is on another loop and
        # wrap_future handles the cross-loop signalling.
        return await asyncio.wrap_future(fut)


# ---------------------------------------------------------------------------
# ConductorDispatcher — pool dispatch + conductor-state gating
# ---------------------------------------------------------------------------


class ConductorDispatcher:
    """Dispatch through a :class:`Conductor` (which routes through the pool).

    Adds the run-time state gate: commands are refused outside PREPARING /
    RUNNING. This is what protects a procedure from issuing a command into
    a worker that's already disarming, which would race with
    ``adapter.stop()``.

    The conductor's own :meth:`Conductor.dispatch` already enforces the
    gate; we layer the state check on the dispatcher side too so the
    failure is raised by the time the executor's ``await`` resumes,
    instead of being deferred until the worker side decides.
    """

    __slots__ = ("_conductor",)

    def __init__(self, conductor: Conductor) -> None:
        self._conductor = conductor

    async def dispatch(self, device: str, cmd: DeviceCommand) -> CommandResult:
        """Submit through the conductor, gated by conductor state.

        Raises:
            ConductorStateError: The conductor is not in a state that
                permits dispatch (i.e. not in PREPARING or RUNNING).
            UnknownDeviceError: ``device`` is not owned by any worker.
        """
        # Early-fail: conductor.dispatch ALSO checks, but we want the
        # ConductorStateError to surface here before incurring a cross-thread
        # round-trip.
        if not self._conductor.state.permits_dispatch():
            raise ConductorStateError(
                f"dispatch refused in state {self._conductor.state}",
                current=self._conductor.state,
            )
        try:
            fut = self._conductor.dispatch(device, cmd)
        except KeyError as exc:
            raise UnknownDeviceError(device) from exc
        return await asyncio.wrap_future(fut)


# ---------------------------------------------------------------------------
# ManualClient — UI-facing facade with transparent run-state routing
# ---------------------------------------------------------------------------


class ManualClient:
    """Single async facade for UI manual cards.

    Routes :meth:`dispatch` and :meth:`snapshot` to a live :class:`Conductor`
    when a run is armed (records into the bundle, gates by conductor state),
    else to the :class:`WorkerPool` directly. Cards take a :class:`ManualClient`
    reference at construction and remain unaware of which side is in use.

    The conductor reference is fetched through a callable rather than stored
    by reference because the conductor's lifetime is per-run: there is no
    conductor before the first :meth:`Conductor.start`, no conductor between
    runs once the previous one has sealed, and a freshly-constructed one
    on every new run. The provider closes over the
    :class:`~capa.ui.state.RunController`'s ``_conductor`` field and resolves
    at call time.

    A returned conductor is only used if its
    :meth:`~capa.runtime.state.ConductorState.permits_dispatch` is true (i.e.
    PREPARING or RUNNING). DRAINING / FINALIZING / SEALED / FAILED conductors
    are treated as absent — the manual command falls through to the pool, which
    is correct: the run is on the way out, the pool is still open, and a
    between-runs manual command should not be refused because the *previous*
    run's conductor is still finalizing.
    """

    __slots__ = ("_get_conductor", "_pool")

    def __init__(
        self,
        pool: WorkerPool,
        conductor_provider: Callable[[], Conductor | None],
    ) -> None:
        self._pool = pool
        self._get_conductor = conductor_provider

    async def dispatch(self, device: str, cmd: DeviceCommand) -> CommandResult:
        """Issue ``cmd`` against ``device``.

        Routes through the conductor when a run is armed and the
        conductor is dispatchable, else through the pool. The future
        returned by either side is bridged into the caller's loop via
        :func:`asyncio.wrap_future`.
        """
        conductor = self._active_conductor()
        try:
            if conductor is not None:
                fut = conductor.dispatch(device, cmd)
            else:
                fut = self._pool.dispatch(device, cmd)
        except KeyError as exc:
            raise UnknownDeviceError(device) from exc
        return await asyncio.wrap_future(fut)

    async def snapshot(self, device: str) -> DeviceEmission:
        """One-shot snapshot of ``device``.

        Same routing rules as :meth:`dispatch`.
        """
        conductor = self._active_conductor()
        try:
            if conductor is not None:
                fut = conductor.snapshot(device)
            else:
                fut = self._pool.snapshot(device)
        except KeyError as exc:
            raise UnknownDeviceError(device) from exc
        return await asyncio.wrap_future(fut)

    def camera(self, device_name: str) -> Camera | None:
        """Return the underlying :class:`Camera` handle for ``device_name``.

        Preview JPEGs are NOT consumed through this surface — they ride
        a per-camera :class:`~capa.runtime.bridge.ThreadBridge` owned by
        the pool. Device-probe metadata (UVC ranges, supported resolutions,
        per-resolution fps caps) is NOT consumed through this surface
        either — use :meth:`camera_metadata` for a worker-loop-safe
        snapshot.

        Retained for tests that want to assert the wrapper hosts the
        expected handle; UI code should not introduce new callers. The
        returned handle lives on the worker loop, so touching its methods
        from the qasync loop violates the worker-owns-the-handle invariant.

        Returns ``None`` if the device name is unknown or refers to a
        non-camera adapter.
        """
        try:
            worker = self._pool.worker_for(device_name)
        except Exception:
            return None
        adapter: object | None = worker.adapters.get(device_name)
        if not isinstance(adapter, CameraDeviceAdapter):
            return None
        return adapter.camera

    async def camera_metadata(self, device_name: str) -> WebcamMetadata | None:
        """Probe a camera's metadata across loops without touching the handle.

        Submits the read to the worker that owns ``device_name``; the
        worker runs ``camera.snapshot_metadata()`` on its own loop and
        signals the resulting future. :func:`asyncio.wrap_future` bridges
        back to the caller's loop — same pattern as :meth:`dispatch` and
        :meth:`snapshot`.

        Returns ``None`` for adapters whose underlying camera does not
        expose a metadata surface (IR cameras today, plus the safety
        net for any future non-webcam adapter routed here by mistake).
        Card code treats ``None`` as "fall back to static widget defaults".

        Raises :class:`UnknownDeviceError` for names that aren't configured
        — same surface as :meth:`dispatch`. Does **not** route through the
        conductor: metadata is a pool-resident probe with no run-state
        semantics, so the same call path applies whether or not a run is
        armed.
        """
        try:
            fut = self._pool.camera_metadata(device_name)
        except KeyError as exc:
            raise UnknownDeviceError(device_name) from exc
        return await asyncio.wrap_future(fut)

    async def device_readback(self, device_name: str) -> object:
        """Probe a device's ``read_state_snapshot()`` across loops.

        Returns the adapter-specific snapshot (e.g.
        :class:`~capa.devices.watlow.WatlowStateSnapshot`) or ``None`` for
        adapters that don't implement it. Card code treats ``None`` as
        "fall back to widget defaults".

        Does **not** route through the conductor: this is a pool-resident
        read used by manual-control cards to prefill their widgets, with no
        run-state semantics. Submits to the worker that owns ``device_name``
        and bridges the future back to the caller's loop.
        """
        try:
            fut = self._pool.device_readback(device_name)
        except KeyError as exc:
            raise UnknownDeviceError(device_name) from exc
        return await asyncio.wrap_future(fut)

    def _active_conductor(self) -> Conductor | None:
        """Return the conductor only when it can accept a dispatch.

        See class docstring for why DRAINING / FINALIZING / SEALED / FAILED
        are treated as "no conductor" — the pool is still the right
        destination for those.
        """
        conductor = self._get_conductor()
        if conductor is None:
            return None
        if not conductor.state.permits_dispatch():
            return None
        return conductor


__all__ = [
    "AdapterDispatcher",
    "CommandDispatcher",
    "ConductorDispatcher",
    "ManualClient",
    "PoolDispatcher",
]
