""":class:`CameraDeviceAdapter` — Camera → DeviceAdapter bridge.

Migration doc §6 (camera unification). Cameras and device adapters have
incompatible lifecycles by design:

* Devices: ``open / close / start / stop / stream / command / snapshot``,
  emit :data:`~capa.devices.records.DeviceEmission` from ``stream()``.
* Cameras: ``open / close / start_recording(path) / stop_recording``,
  emit :class:`~capa.devices.camera.base.FrameReceipt` from
  ``frame_stream()`` and :class:`~capa.devices.camera.base.CameraEvent`
  from ``event_stream()``, plus a separate preview stream and periodic
  :class:`CameraHealth` snapshots.

Rewriting every camera to fit the device protocol would change four
adapters (two real, two sim) AND the four-stream wiring. This module
takes the cheaper path: a thin wrapper that **implements**
:class:`~capa.devices.adapter.DeviceAdapter` over a
:class:`~capa.devices.camera.base.Camera`. The wrapper:

1. Multiplexes ``frame_stream`` + ``event_stream`` into the single
   ``stream()`` the worker iterates. Preview stays addressable on the
   underlying Camera for UI cards (migration doc §6.2: preview path
   unchanged).
2. Translates ``start(run_context)`` into the camera's
   ``start_recording(output_path)``, computing the path via
   :func:`~capa.experiment.cameras.camera_output_path`. The wrapper
   takes a :class:`RunContext` parameter rather than just a
   :class:`RunClock` so it can derive the bundle path + run id —
   :meth:`Worker._adapter_start` falls back from ``start(ctx)`` →
   ``start(clock)`` → ``start()`` for non-camera adapters.
3. Late-binds the run-authoritative :class:`RunClock`. Cameras are
   constructed at :meth:`WorkerPool.open`-time (before any run exists);
   the wrapper hands the underlying camera a :class:`_ClockProxy` at
   build time and rebinds it inside :meth:`start` when a real
   :class:`RunClock` arrives.

The Worker code stays generic — it never knows it's hosting a camera.
The Conductor's :meth:`~capa.runtime.conductor.Conductor._dispatch_emission`
dispatches by runtime type
(:class:`FrameReceipt`/:class:`CameraEvent`/anything-else) so the
wrapper's emissions land on the correct writer paths.

What the wrapper deliberately does NOT do:

* Multiplex preview into the worker outbound bridge. Preview is UI-only
  (one consumer, DROP_OLDEST semantics, never on the procedure bus). It
  rides a separate per-camera :class:`~capa.runtime.bridge.ThreadBridge`
  built by the pool, drained on the worker loop by
  :meth:`start_preview_channel` and consumed on the UI loop by
  :meth:`~capa.ui.state.RunController._drain_preview`.
* Periodically emit health snapshots. The Camera's
  :meth:`~capa.devices.camera.base.Camera.snapshot` is callable on
  demand (mapped onto :meth:`DeviceAdapter.snapshot`); periodic
  scraping is a Phase 5 nicety.
* Enforce ``on_failure`` policy. Camera events with
  ``severity="error"`` flow through the standard drain into the bundle;
  Phase 5 wires :class:`~capa.experiment.safety.SafetyMonitor` to act
  on them. The Conductor's saturation deadline still catches a wedged
  camera the same way it catches a wedged device.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

import anyio
import structlog

from capa.core.clock import RunClock
from capa.devices.adapter import Capability, CommandResult, DeviceCommand
from capa.devices.camera.base import (
    Camera,
    CameraCapability,
    CameraHealth,
    CameraSpec,
)
from capa.devices.camera.metadata import WebcamMetadata
from capa.devices.records import DeviceSnapshot
from capa.runtime.preview import PreviewFrame, run_preview_drain

if TYPE_CHECKING:
    from capa.runtime.bridge import ThreadBridge
    from capa.runtime.emissions import WorkerEmission
    from capa.runtime.runcontext import RunContext


_logger = structlog.get_logger("capa.runtime.camera_adapter")


_DEFAULT_BRIDGE_QUEUE_SIZE: Final[int] = 256
"""Internal multiplexer queue size. The Worker's outbound bridge
already provides cross-thread backpressure; this queue is purely
loop-local and only needs enough headroom to absorb the small burst
between ``stop_recording`` and ``run_pump`` exiting (typically 1–3
trailing frames + the ``recording_stopped`` event)."""

_IDLE_SOURCE_MAX_ATTEMPTS: Final[int] = 3
"""Wrapper-level retry budget for the idle preview source. The
camera's own ``_open_input_with_retry`` has an 8 s deadline; with 3
attempts and progressive backoff we cover up to ~30 s of post-disarm
DirectShow filter-graph hold-time. After this we give up — the
camera is genuinely held by another process."""

_IDLE_SOURCE_BACKOFF_S: Final[float] = 3.0
"""Per-attempt backoff multiplier. Attempt N waits ``N * 3 s`` before
retrying — long enough for DirectShow to release the graph without
being so long the operator notices preview gap > stale threshold."""


# ---------------------------------------------------------------------------
# Clock proxy — late-binds the RunClock onto a Camera constructed at pool-open.
# ---------------------------------------------------------------------------


class _ClockProxy:
    """Duck-typed :class:`RunClock` substitute with rebinding.

    Camera adapters take a :class:`RunClock` at construction and use it
    via ``.t_mono_ns()`` / ``.to_wall_ns(...)``. In the worker model,
    the real run clock is minted inside :meth:`RealRunSession.open`
    — long after :meth:`WorkerPool.open` has constructed the camera.

    The proxy is duck-typed (not a :class:`RunClock` subclass — that
    class is frozen). At construction it carries a "now" clock so any
    pre-arm health snapshot has sane timestamps; :meth:`rebind` swaps
    in the real run clock when :meth:`CameraDeviceAdapter.start`
    receives a :class:`RunContext`. Without rebinding, every frame
    receipt would carry a ``t_mono_ns`` relative to the pool-open
    monotonic origin, not the run-start origin — bundles would not
    match the engine baseline.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner: RunClock | None = None) -> None:
        self._inner = inner if inner is not None else RunClock.now()

    def rebind(self, clock: RunClock) -> None:
        """Swap the proxy's inner clock. Called from :meth:`start`.

        Idempotent: rebinding to the same clock is a no-op. Re-arming
        for a new run rebinds with the new run's clock, so frame
        timestamps reset to the new monotonic origin.
        """
        self._inner = clock

    @property
    def started_mono_ns(self) -> int:
        return self._inner.started_mono_ns

    @property
    def started_utc(self) -> datetime:
        return self._inner.started_utc

    def t_mono(self) -> float:
        return self._inner.t_mono()

    def t_mono_ns(self) -> int:
        return self._inner.t_mono_ns()

    def to_wall(self, t_mono_s: float) -> datetime:
        return self._inner.to_wall(t_mono_s)

    def to_wall_ns(self, t_mono_ns: int) -> datetime:
        return self._inner.to_wall_ns(t_mono_ns)


# ---------------------------------------------------------------------------
# Multiplexer sentinel — internal-only, never crosses the bridge.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _MuxClosed:
    """Sentinel value pushed onto the internal queue when both source
    streams have ended. The wrapper's :meth:`stream` returns on receipt.
    Frozen so identity comparison ``is _MUX_CLOSED`` is safe."""


_MUX_CLOSED: Final[_MuxClosed] = _MuxClosed()


# ---------------------------------------------------------------------------
# The wrapper itself.
# ---------------------------------------------------------------------------


class CameraDeviceAdapter:
    """:class:`DeviceAdapter`-shaped wrapper over a :class:`Camera`.

    Construction is cheap and does no I/O: the underlying camera is
    constructed (its ``__init__`` is documented to be I/O-free), then
    held until :meth:`open`. :meth:`open` calls through to
    :meth:`Camera.open`; the worker runs this on the worker loop, so
    the camera handle binds to the right thread.

    Lifecycle inside one run:

    1. ``open()`` → camera.open() → device handle opens (e.g. PyAV
       input, FLIR system enumeration).
    2. ``start(ctx)`` → rebind clock; compute output_path from ctx;
       camera.start_recording(path); spawn the multiplexer task group.
    3. ``stream()`` yields :class:`FrameReceipt` and :class:`CameraEvent`
       interleaved from the camera's two source streams.
    4. ``stop()`` → camera.stop_recording(); multiplexer drains and
       closes; stream() exits naturally.
    5. ``close()`` → camera.close().

    The cycle 2→4 can repeat for multiple runs against the same wrapper;
    1 and 5 are paid once per pool open/close.
    """

    __slots__ = (
        "_camera",
        "_channel_task",
        "_clock_proxy",
        "_mux_queue",
        "_mux_scope",
        "_mux_task",
        "_preview_bridge",
        "_recording",
        "_run_id",
        "_source_task",
        "_spec",
    )

    # The wrapper itself has no special capabilities — the Camera's flags
    # live on a different enum (CameraCapability). Procedures don't probe
    # this attribute for cameras today, so an empty set is correct.
    capabilities: frozenset[Capability] = frozenset()

    def __init__(
        self,
        *,
        camera: Camera,
        spec: CameraSpec,
        clock_proxy: _ClockProxy,
    ) -> None:
        """Build a wrapper around an already-constructed :class:`Camera`.

        :param camera: The underlying camera instance. Must have been
            constructed with ``clock=clock_proxy`` so rebinding works.
        :param spec: The :class:`CameraSpec` (carries name, kind,
            output_root, on_failure, params). Held for output-path
            computation and bundle attribution.
        :param clock_proxy: The same proxy the camera was constructed
            with. The wrapper rebinds this on :meth:`start`.
        """
        self._camera = camera
        self._spec = spec
        self._clock_proxy = clock_proxy
        self._mux_queue: asyncio.Queue[WorkerEmission | _MuxClosed] | None = None
        self._mux_scope: anyio.CancelScope | None = None
        self._mux_task: asyncio.Task[None] | None = None
        self._recording = False
        self._run_id: str | None = None
        # Preview channel (drainer) — open()..close() lifetime.
        self._preview_bridge: ThreadBridge[PreviewFrame] | None = None
        self._channel_task: asyncio.Task[None] | None = None
        # Idle preview source (pump) — IDLE-only lifetime.
        self._source_task: asyncio.Task[None] | None = None

    # ---- DeviceAdapter Protocol surface --------------------------------

    @property
    def name(self) -> str:
        """Adapter-assigned device name; matches :attr:`CameraSpec.name`."""
        return self._spec.name

    @property
    def resource_id(self) -> str:
        """Delegate to :attr:`Camera.resource_id`.

        Sim cameras use ``sim:<name>``; the webcam adapter uses
        ``webcam:<serial>`` or ``webcam:<name>``. This is what
        :func:`build_workers` groups on — two adapters sharing this
        string land in one worker.
        """
        return self._camera.resource_id

    @property
    def expected_emission_rate_hz(self) -> float | None:
        """Hint for outbound-bridge sizing.

        Cameras at 30 fps + a sprinkle of CameraEvents work out to ~30
        emissions/sec — the existing default capacity (64) covers this
        with 2 s of headroom. We expose a numeric hint anyway so future
        per-rate capacity tuning works uniformly across devices and
        cameras.
        """
        fps_raw = self._spec.params.get("fps")
        if isinstance(fps_raw, int | float) and fps_raw > 0:
            return float(fps_raw)
        return None

    @property
    def camera(self) -> Camera:
        """Read-only access to the underlying camera.

        Retained for the few UI paths that need a typed handle for
        read-only attribute probes (e.g. :meth:`WebcamCard._refresh_controls_from_probe`
        reads cached UVC ranges populated at adapter ``open()``). Preview
        no longer flows through this surface — it rides the pool-owned
        :class:`~capa.runtime.bridge.ThreadBridge` drained by
        :meth:`start_preview_channel`.
        """
        return self._camera

    @property
    def spec(self) -> CameraSpec:
        """The originating :class:`CameraSpec`. Read-only."""
        return self._spec

    async def open(self) -> None:
        """Open the camera handle. Idempotent (Camera.open is documented
        as idempotent for the success path).

        For cameras with a long-lived input pump (the visible
        :class:`WebcamAdapter` advertises ``start_input_pump``), this
        also spawns the pump so the live preview tile stays current for
        the entire pool lifetime — across runs and between them.
        Cameras without that surface (IR sim, FLIR Atlas, …) keep their
        per-run pump driven by the multiplexer in :meth:`stream`.
        """
        await self._camera.open()
        start_pump = getattr(self._camera, "start_input_pump", None)
        if callable(start_pump):
            await start_pump()

    async def close(self) -> None:
        """Close the camera handle. Idempotent.

        If recording is somehow still active when this lands (the
        worker.close() path requires IDLE, but defensive code is cheap
        here), the camera's own :meth:`close` flips ``stop_recording``
        before releasing the handle (see [flir_ir_sim.py:305-314]) and
        — for cameras with a long-lived input pump — also stops the
        pump.
        """
        await self._camera.close()

    async def start(self, run_context: RunContext) -> None:
        """Begin recording for the named run.

        Wraps ``Camera.start_recording(path)`` with the path resolution
        from :func:`camera_output_path`. The clock proxy is rebound
        here so every :class:`FrameReceipt`'s ``t_mono_ns`` is
        relative to the new run's :class:`RunClock` origin.

        :param run_context: The conductor's per-run context. Carries
            the bundle path (via :attr:`bundle.root`), the run id, and
            the authoritative clock.

        Raises if recording is already active — the worker state
        machine guarantees ``start`` only runs on ARMED→SAMPLING.
        """
        if self._recording:
            raise RuntimeError(
                f"CameraDeviceAdapter[{self._spec.name!r}]: start() called while already recording"
            )
        self._clock_proxy.rebind(run_context.clock)
        self._run_id = run_context.run_id
        output_path = self._resolve_output_path(run_context)
        await self._camera.start_recording(output_path)
        self._recording = True
        # Build the mux queue on the worker loop so it binds to the
        # right loop. The stream() coroutine will be entered next by
        # the worker; the mux task is started lazily on stream() entry
        # so the producer-side task lifetime nests cleanly inside the
        # worker's _stream_task scope (which knows when to exit).

    async def stop(self) -> None:
        """Stop recording and signal the multiplexer to drain.

        Camera.stop_recording flips the recording flag inside the
        camera; ``run_pump`` (for cameras that have one) exits its
        ``while self._recording`` loop. The wrapper's multiplexer
        sees both the frame stream and the event stream end naturally,
        pushes ``_MUX_CLOSED`` onto its queue, and exits. The worker's
        ``_stream_task`` sees iterator-exhaustion and returns.
        """
        if not self._recording:
            return
        self._recording = False
        try:
            await self._camera.stop_recording()
        finally:
            # Signal the multiplexer cancel scope so a wedged
            # frame_stream (e.g. a Camera that doesn't honour
            # stop_recording promptly) doesn't keep the worker in
            # SAMPLING past disarm grace. The worker's disarm path
            # awaits the stream task with a grace timeout and forces
            # cancellation past it (DisarmResult.FORCED); the mux
            # cancel scope here is the cooperative signal.
            scope = self._mux_scope
            if scope is not None:
                scope.cancel()

    async def stream(self) -> AsyncIterator[WorkerEmission]:
        """Yield interleaved frame receipts and camera events.

        Spawns producer tasks on first entry: one drains
        :meth:`Camera.frame_stream`, one drains :meth:`Camera.event_stream`,
        and (for cameras that expose ``run_pump``) one drives the pump.
        Producers feed into an internal :class:`asyncio.Queue` that this
        iterator consumes. A coordinator awaits all producers and pushes
        ``_MUX_CLOSED`` when the last one exits.

        Exits cleanly when:

        * Both source streams have ended (camera was stopped and closed) —
          the coordinator pushes ``_MUX_CLOSED`` and this iterator
          returns at the next loop iteration.
        * The wrapper's :meth:`stop` cancels the mux scope — producers
          unwind via :class:`anyio.get_cancelled_exc_class`; the
          coordinator finally-pushes the sentinel; this iterator returns.
        """
        if not self._recording:
            raise RuntimeError(
                f"CameraDeviceAdapter[{self._spec.name!r}]: stream() called while not recording"
            )
        queue: asyncio.Queue[WorkerEmission | _MuxClosed] = asyncio.Queue(
            maxsize=_DEFAULT_BRIDGE_QUEUE_SIZE,
        )
        self._mux_queue = queue

        # Producers run under a sub-task-group spawned by the
        # coordinator. The coordinator runs as ONE task spawned on this
        # loop (not as part of the wrapper's task group) so that the
        # async-generator's lifecycle is decoupled from the task group's
        # join semantics — yielding from inside an `async with` task
        # group works, but exit semantics get fragile when the consumer
        # iterates lazily. Keeping the coordinator standalone simplifies
        # cancellation: stop() cancels coordinator → tasks unwind →
        # coordinator's finally pushes sentinel.
        loop = asyncio.get_running_loop()

        async def _coordinator() -> None:
            try:
                async with anyio.create_task_group() as tg:
                    self._mux_scope = tg.cancel_scope
                    tg.start_soon(self._drain_frames_into, queue)
                    tg.start_soon(self._drain_events_into, queue)
                    tg.start_soon(self._run_pump_if_present)
            except BaseException as exc:
                if not isinstance(exc, anyio.get_cancelled_exc_class()):
                    _logger.warning(
                        "camera_adapter.coordinator_failed",
                        camera=self._spec.name,
                        error=str(exc),
                    )
            finally:
                # Always push the sentinel so the consumer exits even on
                # a coordinator-side crash. The sentinel never crosses
                # the worker bridge — _MuxClosed is consumed inside this
                # iterator.
                with contextlib.suppress(Exception):
                    queue.put_nowait(_MUX_CLOSED)

        coord_task = loop.create_task(
            _coordinator(),
            name=f"camera-mux-{self._spec.name}",
        )
        self._mux_task = coord_task
        try:
            while True:
                item = await queue.get()
                if isinstance(item, _MuxClosed):
                    break
                yield item
        finally:
            # Caller stopped iterating (worker disarm, exception, etc).
            # Ensure the coordinator unwinds — cancel its scope and
            # await its completion so we don't leak a task.
            scope = self._mux_scope
            if scope is not None and not coord_task.done():
                scope.cancel()
            if not coord_task.done():
                with contextlib.suppress(BaseException):
                    await coord_task
            self._mux_queue = None
            self._mux_scope = None
            self._mux_task = None

    async def _drain_frames_into(self, queue: asyncio.Queue[WorkerEmission | _MuxClosed]) -> None:
        try:
            async for receipt in self._camera.frame_stream():
                await queue.put(receipt)
        except anyio.get_cancelled_exc_class():
            raise
        except BaseException as exc:
            _logger.warning(
                "camera_adapter.frame_drain_failed",
                camera=self._spec.name,
                error=str(exc),
            )

    async def _drain_events_into(self, queue: asyncio.Queue[WorkerEmission | _MuxClosed]) -> None:
        try:
            async for event in self._camera.event_stream():
                await queue.put(event)
        except anyio.get_cancelled_exc_class():
            raise
        except BaseException as exc:
            _logger.warning(
                "camera_adapter.event_drain_failed",
                camera=self._spec.name,
                error=str(exc),
            )

    async def _run_pump_if_present(self) -> None:
        pump = getattr(self._camera, "run_pump", None)
        if not callable(pump):
            return
        try:
            await pump()
        except anyio.get_cancelled_exc_class():
            raise
        except BaseException as exc:
            _logger.warning(
                "camera_adapter.pump_failed",
                camera=self._spec.name,
                error=str(exc),
            )

    async def snapshot(self) -> DeviceSnapshot:
        """Translate :class:`CameraHealth` into a :class:`DeviceSnapshot`.

        The two have overlapping but distinct field sets. We map:

        * ``healthy`` → :attr:`DeviceSnapshot.healthy` and
          :attr:`health` (``"ok"`` / ``"degraded"`` based on the
          health flag + presence of an error).
        * Carry ``frame_count``, ``file_size_bytes``,
          ``last_frame_t_mono_ns``, ``dropped_frames``,
          ``recording`` into :attr:`fields` so a downstream consumer
          can reconstruct what a snapshot of the camera looked like
          at this moment.

        Used by the procedure's status checks; not the periodic
        bundle-status path (which still goes through the Camera's
        own snapshot path in Phase 5).
        """
        health: CameraHealth = await self._camera.snapshot()
        return DeviceSnapshot(
            adapter="camera",
            device=health.name,
            t_mono_ns=health.t_mono_ns,
            t_utc=health.t_utc,
            healthy=health.healthy,
            health="ok" if health.healthy else "degraded",
            fields={
                "recording": health.recording,
                "frame_count": health.frame_count,
                "file_size_bytes": health.file_size_bytes,
                "last_frame_t_mono_ns": (
                    health.last_frame_t_mono_ns if health.last_frame_t_mono_ns is not None else 0
                ),
                "dropped_frames": health.dropped_frames,
                "error": health.error or "",
            },
        )

    async def command(self, cmd: DeviceCommand) -> CommandResult:
        """Forward to :meth:`Camera.command`.

        The worker's :meth:`~capa.runtime.worker.Worker._dispatch_impl`
        wraps this call in :func:`asyncio.shield` — the same
        cancellation-shield rule that prevents Watlow ReadResponse
        corruption applies to camera commands too. Cancelling the
        caller's future doesn't interrupt an in-flight NUC trigger or
        ``set_emissivity`` mid-transaction.
        """
        return await self._camera.command(cmd)

    def camera_metadata(self) -> WebcamMetadata | None:
        """Build a probe snapshot for cards that need cross-loop metadata.

        Capability-style probe: cameras that expose ``snapshot_metadata``
        (only :class:`WebcamAdapter` today) return their
        :class:`WebcamMetadata`; others (FLIR Atlas, IR sim) return
        ``None`` and the consumer falls back to its static widget set.

        Runs on whichever loop calls it; the worker dispatcher submits
        this onto the worker loop via :meth:`Worker.camera_metadata` so
        the read happens on the same loop that owns the camera handle
        (migration doc §3.11 invariant 2). Synchronous because the
        underlying probe is a pure attribute read — there is no I/O.
        """
        snapshot = getattr(self._camera, "snapshot_metadata", None)
        if not callable(snapshot):
            return None
        result = snapshot()
        if not isinstance(result, WebcamMetadata):
            return None
        return result

    # ---- preview lifecycle ---------------------------------------------
    #
    # Two independent task scopes:
    #
    # * channel (drainer): camera.preview_stream() → bridge. Lifetime =
    #   open()..close(); survives IDLE/ARMED/SAMPLING/DRAINING. Forwards
    #   JPEGs from the camera's preview stream onto the pool-owned
    #   ThreadBridge regardless of run state.
    # * source (pump): retained as no-op hooks for cameras with a long-
    #   lived input pump owned by the camera itself (the visible
    #   :class:`WebcamAdapter` runs one continuous av.open for the entire
    #   adapter-open lifetime; see :meth:`open`). For legacy cameras that
    #   exposed ``run_preview_pump`` / ``start_preview`` as separate IDLE-
    #   only routines this still drives them via getattr-probing — kept
    #   so future adapters can opt into the old shape without redesigning
    #   the worker calls.

    async def start_preview_channel(self, bridge: ThreadBridge[PreviewFrame]) -> None:
        """Start the long-lived drainer that pumps
        :meth:`Camera.preview_stream` onto ``bridge``.

        Called by :meth:`Worker._open_all_impl` once per pool open, after
        :meth:`Camera.open` returns. The drainer keeps running across
        every subsequent arm/sample/disarm cycle; preview JPEGs arriving
        from the recording pump during SAMPLING flow through the same
        channel as JPEGs from ``run_preview_pump`` during IDLE.

        Early-out: if :attr:`CameraCapability.LIVE_PREVIEW` is not in
        ``camera.capabilities``, this method is a no-op and no task is
        spawned. Avoids spinning a dead task for cameras that don't emit
        previews.

        Idempotent — a second call when the drainer is already running
        is a no-op.
        """
        if CameraCapability.LIVE_PREVIEW not in self._camera.capabilities:
            return
        if self._channel_task is not None and not self._channel_task.done():
            return
        loop = asyncio.get_running_loop()
        # The bridge's producer-loop binding is one-shot per pool open.
        # The pool guarantees this method is invoked exactly once per
        # (adapter, bridge) pair on each open cycle.
        bridge.attach_producer(loop)
        self._preview_bridge = bridge
        self._channel_task = loop.create_task(
            run_preview_drain(camera=self._camera, bridge=bridge),
            name=f"preview-channel-{self._spec.name}",
        )

    async def stop_preview_channel(self) -> None:
        """Cancel the drainer task; await its exit. Idempotent.

        Called by :meth:`Worker._close_all_impl` exactly once, on the
        IDLE→CLOSED edge, BEFORE :meth:`Camera.close`. Does NOT touch
        the idle source — that must already have been stopped on the
        ARMED→SAMPLING transition or by :meth:`stop_idle_preview_source`
        on the close path.
        """
        task = self._channel_task
        self._channel_task = None
        self._preview_bridge = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(BaseException):
            await task

    async def start_idle_preview_source(self) -> None:
        """Start the IDLE-only preview source for adapters that have one.

        For :class:`WebcamAdapter`: ``await camera.start_preview()``;
        spawn ``run_preview_pump()``. For adapters without a separate
        preview pump (FLIR Atlas, IR sim) this is a no-op.

        Called by :meth:`Worker` on every entry into IDLE:

        * CLOSED → IDLE (right after :meth:`start_preview_channel`), and
        * DRAINING → IDLE (after the recording pump has released the
          input container).

        Idempotent: a second call while the source is running returns
        without action.
        """
        if self._source_task is not None and not self._source_task.done():
            return
        camera = self._camera
        pump = getattr(camera, "run_preview_pump", None)
        if not callable(pump):
            return
        start_preview = getattr(camera, "start_preview", None)
        if callable(start_preview):
            try:
                await start_preview()
            except anyio.get_cancelled_exc_class():
                raise
            except BaseException as exc:
                _logger.warning(
                    "camera_adapter.start_preview_failed",
                    camera=self._spec.name,
                    error=str(exc),
                )
                return

        async def _run_source() -> None:
            # Retry the pump on failure: after a DRAINING→IDLE
            # transition, Windows DirectShow holds the filter graph
            # for several seconds after the recording pump's
            # ``in_container.close``. The camera's own
            # ``_open_input_with_retry`` is bounded (~8 s); on a slow
            # release the first ``pump()`` call hits its deadline and
            # raises. Without a wrapper-level retry the operator would
            # see "preview unavailable" until they reload the config.
            #
            # The backoff lets the OS settle between attempts and
            # cumulatively covers the worst-case observed C930e
            # release latency (~15 s end-to-end). A cancellation from
            # ``stop_idle_preview_source`` interrupts either the pump
            # or the sleep and propagates cleanly.
            attempts = 0
            while True:
                try:
                    await pump()
                    return
                except anyio.get_cancelled_exc_class():
                    raise
                except BaseException as exc:
                    attempts += 1
                    if attempts >= _IDLE_SOURCE_MAX_ATTEMPTS:
                        _logger.warning(
                            "camera_adapter.preview_pump_failed",
                            camera=self._spec.name,
                            attempts=attempts,
                            error=str(exc),
                        )
                        return
                    backoff = _IDLE_SOURCE_BACKOFF_S * attempts
                    _logger.debug(
                        "camera_adapter.preview_pump_retry",
                        camera=self._spec.name,
                        attempt=attempts,
                        backoff_s=backoff,
                        error=str(exc),
                    )
                    await anyio.sleep(backoff)

        loop = asyncio.get_running_loop()
        self._source_task = loop.create_task(
            _run_source(),
            name=f"preview-source-{self._spec.name}",
        )

    async def stop_idle_preview_source(self) -> None:
        """Cancel the source task; await its exit; call
        :meth:`Camera.stop_preview` if the camera exposes it.

        Called on the IDLE→ARMED edge (before :meth:`_adapter_start`
        runs :meth:`Camera.start_recording`) and on the IDLE→CLOSED edge
        before :meth:`stop_preview_channel`.

        Mutually-exclusive guarantee: when this returns, the input
        container is released. The recording pump can safely claim it.
        Idempotent.
        """
        task = self._source_task
        self._source_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        stop_preview = getattr(self._camera, "stop_preview", None)
        if callable(stop_preview):
            try:
                await stop_preview()
            except anyio.get_cancelled_exc_class():
                raise
            except BaseException as exc:
                _logger.warning(
                    "camera_adapter.stop_preview_failed",
                    camera=self._spec.name,
                    error=str(exc),
                )

    # ---- internals -----------------------------------------------------

    def _resolve_output_path(self, run_context: RunContext) -> Path:
        """Compute the camera's container path.

        Mirrors :func:`capa.experiment.cameras.camera_output_path`
        without importing it (avoids the experiment → runtime cycle the
        rest of build.py already dances around). Layout:

        * Default: ``<bundle_root>/video/<name>.<ext>``
        * With ``spec.output_root``: ``<output_root>/<run_id>/video/<name>.<ext>``

        ``<ext>`` is ``.csq`` for IR (FLIR sequence container), ``.mkv``
        for visible (matroska H.264).
        """
        ext = ".csq" if self._spec.kind == "ir" else ".mkv"
        if self._spec.output_root is not None:
            base = Path(self._spec.output_root).expanduser() / run_context.run_id / "video"
        else:
            root = run_context.bundle.root
            if not isinstance(root, Path):
                # Tests sometimes pass a stub bundle ref with a string
                # root. Coerce here so the rest of the path arithmetic
                # works uniformly.
                root = Path(str(root))
            base = root / "video"
        base.mkdir(parents=True, exist_ok=True)
        return base / f"{self._spec.name}{ext}"


# ---------------------------------------------------------------------------
# Factory — used by build_workers to construct a wrapper from a CameraSpec.
# ---------------------------------------------------------------------------


def make_camera_adapter(
    *,
    camera_cls: type[Camera],
    spec: CameraSpec,
    extra_params: dict[str, Any] | None = None,
) -> CameraDeviceAdapter:
    """Build a :class:`CameraDeviceAdapter` from class + spec.

    Mirrors :func:`capa.experiment.cameras.construct_cameras`'s
    resolution rules but produces a wrapper instead of a bare camera:

    1. A :class:`_ClockProxy` is minted first so the camera's
       constructor receives something clock-shaped.
    2. The camera class is instantiated via ``from_params`` if it
       exposes one (sim cameras + webcam adapter both do), else via
       ``__init__`` directly.
    3. The wrapper is built around the camera + spec + proxy.

    Keeping the factory here (rather than at the build_workers call
    site) means tests can construct a wrapper without going through a
    full :class:`ExperimentConfig`.
    """
    proxy = _ClockProxy()
    params = dict(spec.params)
    if extra_params:
        params.update(extra_params)
    factory = cast(Any, camera_cls)
    from_params = getattr(camera_cls, "from_params", None)
    if callable(from_params):
        camera = cast(Camera, from_params(spec=spec, clock=proxy, **params))
    else:
        camera = cast(Camera, factory(spec=spec, clock=proxy, **params))
    if camera.kind != spec.kind:
        raise ValueError(
            f"camera {spec.name!r}: spec.kind={spec.kind!r} but adapter "
            f"reports kind={camera.kind!r}"
        )
    return CameraDeviceAdapter(camera=camera, spec=spec, clock_proxy=proxy)


__all__ = [
    "CameraDeviceAdapter",
    "make_camera_adapter",
]
