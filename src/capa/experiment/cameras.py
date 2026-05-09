"""Camera wiring inside the experiment engine (plan §12, P4 Stage D).

The engine treats cameras as peers of devices, not subtypes — they have
their own task-group entries, their own queues, and their own escalation
path through :attr:`CameraSpec.on_failure`. This module owns the wiring:

* :func:`construct_cameras` — instantiate :class:`~capa.devices.camera.base.Camera`
  objects from :attr:`HardwareProfile.cameras`. Mirrors
  :func:`capa.experiment.engine._construct_adapters` so the construction
  path is uniform (TOML-friendly ``from_params`` first, then plain ``__init__``).
* :func:`camera_output_path` — compute the container path for a camera given
  the bundle root and the §12.4 ``output_root`` escape hatch.
* :func:`disk_space_preflight_problems` — sum projected size across all
  cameras and return blocking :class:`Problem` records when free space falls
  below the configured margin.
* :func:`camera_task` — one async task per camera. Owns the
  ``open → start_recording → drain streams → stop_recording → close`` flow,
  routes frames into the bundle writer, and translates camera events
  (including stalls) into bundle events according to the spec's
  ``on_failure`` policy.
"""

from __future__ import annotations

import importlib
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import structlog

from capa.core.clock import RunClock
from capa.core.errors import CapaError
from capa.devices.camera.base import (
    Camera,
    CameraCapability,
    CameraEvent,
    CameraSpec,
)
from capa.experiment.config import ExperimentConfig
from capa.experiment.procedures.base import Problem
from capa.storage.bundle import RunBundleWriter


class CameraSetupError(CapaError):
    """Raised when a camera adapter can't be imported or constructed.

    Engine-level errors (the existing :class:`EngineError`) live in
    :mod:`capa.experiment.engine`; importing them here would cycle. The
    engine catches :class:`CapaError` at run-arm so this propagates the
    same way.
    """


DEFAULT_DISK_FREE_MARGIN: float = 1.5
"""Plan §12.6: required-free / projected ratio. Default is the plan's 1.5×."""

DEFAULT_FALLBACK_DURATION_S: float = 3600.0
"""Used when :meth:`Method.total_duration_s` returns ``None`` (free-runs,
methods with open-ended waits). 1 hour is a generous default that catches
"the bundle root is full" without false-blocking short test runs."""

VOLATILE_FILESYSTEM_TYPES: frozenset[str] = frozenset({"tmpfs", "ramfs"})
"""Filesystem types that lose contents on reboot or under memory pressure.
A camera bundle pointed at one of these gets a ``disk_target_volatile``
warning AND a tightened budget (free := min(reported, MemAvailable / 2))
because the reported free size is RAM, not disk."""


def _filesystem_type(path: Path) -> str | None:
    """Return the filesystem type of the mount holding ``path`` (Linux only).

    Walks ``/proc/mounts`` and returns the longest-prefix-matching mount's
    type field (``tmpfs``, ``ext4``, ``btrfs``, …). Returns ``None`` on
    non-Linux platforms or on read failure — callers must treat ``None``
    as "unknown, don't fire the volatile-fs warning."
    """
    mounts = Path("/proc/mounts")
    if not mounts.exists():
        return None
    try:
        absolute = str(path.resolve())
        best: tuple[int, str] | None = None
        for line in mounts.read_text().splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            mount, fs_type = parts[1], parts[2]
            if absolute == mount or absolute.startswith(mount.rstrip("/") + "/"):
                length = len(mount)
                if best is None or length > best[0]:
                    best = (length, fs_type)
        return best[1] if best else None
    except OSError:
        return None


def _mem_available_bytes() -> int | None:
    """Return ``MemAvailable`` from ``/proc/meminfo`` in bytes (Linux only).

    Used to tighten the disk budget on tmpfs targets — the kernel-reported
    "free" on a tmpfs is RAM, but RAM under pressure can vanish.
    """
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return None
    try:
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                parts = line.split()
                if len(parts) >= 3 and parts[2].lower() == "kb":
                    return int(parts[1]) * 1024
    except (OSError, ValueError):
        return None
    return None


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def construct_cameras(
    config: ExperimentConfig,
    *,
    clock: RunClock,
) -> list[Camera]:
    """Walk ``config.hardware.cameras`` and instantiate each camera.

    Resolution mirrors :func:`capa.experiment.engine._construct_adapters`:

    1. If the class defines a ``from_params(*, spec=..., clock=..., **params)``
       classmethod, call it. Sim cameras and the webcam adapter both expose
       this so a hardware TOML can fully drive a camera-bearing run.
    2. Otherwise call ``cls(spec=spec, clock=clock, **params)`` directly.
    """
    out: list[Camera] = []
    for spec in config.hardware.cameras:
        cls = _import_camera_class(spec.adapter)
        from_params = getattr(cls, "from_params", None)
        try:
            if callable(from_params):
                cam = from_params(spec=spec, clock=clock, **spec.params)
            else:
                cam = cls(spec=spec, clock=clock, **spec.params)
        except TypeError as exc:
            raise CameraSetupError(
                f"failed to construct camera {spec.name!r} ({spec.adapter}): {exc}"
            ) from exc
        if cam.kind != spec.kind:
            raise CameraSetupError(
                f"camera {spec.name!r}: spec.kind={spec.kind!r} but adapter "
                f"reports kind={cam.kind!r}"
            )
        out.append(cam)
    return out


def _import_camera_class(module_path: str) -> type:
    """Resolve ``capa.devices.camera.webcam`` → ``WebcamAdapter``,
    ``capa.devices.sim.flir_ir_sim`` → ``FlirIrSim``.

    Convention mirrors :func:`capa.experiment.engine._import_adapter_class`:
    the module exports exactly one camera class whose name is a CamelCase
    of the leaf module, optionally suffixed with ``Adapter`` or ``Sim``.
    """
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise CameraSetupError(
            f"camera adapter module {module_path!r} not importable: {exc}"
        ) from exc
    leaf = module_path.rsplit(".", 1)[-1]
    base = _snake_to_camel(leaf)
    base_no_sim = _snake_to_camel(leaf.removesuffix("_sim"))
    candidates = [
        base,
        base + "Adapter",
        base_no_sim + "Sim",
        base_no_sim + "Adapter",
        base_no_sim,
    ]
    seen: list[str] = []
    for name in candidates:
        if name in seen:
            continue
        seen.append(name)
        cls = getattr(module, name, None)
        if isinstance(cls, type):
            return cls
    raise CameraSetupError(f"camera adapter module {module_path!r} does not expose any of {seen}")


def _snake_to_camel(name: str) -> str:
    return "".join(part.title() for part in re.split(r"[_\-]", name))


# ---------------------------------------------------------------------------
# Output path resolution (plan §12.4 escape hatch)
# ---------------------------------------------------------------------------


def camera_output_path(bundle_root: Path, spec: CameraSpec, *, run_id: str) -> Path:
    """Compute the container path for ``spec``.

    Default: ``<bundle_root>/video/<name>.<ext>``. With
    ``CameraSpec.output_root`` set: ``<output_root>/<run_id>/video/<name>.<ext>``.
    """
    ext = ".csq" if spec.kind == "ir" else ".mkv"
    if spec.output_root is not None:
        base = Path(spec.output_root).expanduser() / run_id / "video"
    else:
        base = bundle_root / "video"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{spec.name}{ext}"


# ---------------------------------------------------------------------------
# Disk-space preflight (plan §12.6)
# ---------------------------------------------------------------------------


def disk_space_preflight_problems(
    config: ExperimentConfig,
    *,
    bundle_root: Path,
    margin: float = DEFAULT_DISK_FREE_MARGIN,
    fallback_duration_s: float = DEFAULT_FALLBACK_DURATION_S,
    free_bytes_override: int | None = None,
) -> list[Problem]:
    """Return preflight problems for camera disk-space.

    Computes ``projected = duration_s * sum(estimated_bps)`` and compares
    against free space at ``bundle_root`` (or each camera's
    ``output_root`` if any are external — the worst-case mount is checked).
    Blocks the run when ``free / projected < margin``.

    ``free_bytes_override`` is a test hook; production passes ``None`` and
    the function calls :func:`shutil.disk_usage`.
    """
    cameras = config.hardware.cameras
    if not cameras:
        return []

    duration_s: float | None = None
    if config.method is not None and hasattr(config.method, "total_duration_s"):
        duration_s = config.method.total_duration_s()
    if duration_s is None:
        # Free-runs (and any other method-less procedure) carry their bound
        # in ``procedure.config["duration_s"]`` — read it before falling back
        # to the unbounded-run estimate. Without this, a 30 s free-run was
        # projected against the 3600 s fallback and inflated estimated_bps
        # × duration past free disk on tmpfs (hardware-day §3 anomaly).
        candidate = config.procedure.config.get("duration_s")
        if isinstance(candidate, int | float) and candidate > 0:
            duration_s = float(candidate)
    if duration_s is None:
        duration_s = fallback_duration_s
        duration_inferred = True
    else:
        duration_inferred = False

    projected = sum(int(spec.estimated_bps * duration_s) for spec in cameras)
    if projected <= 0:
        return []

    # Walk every distinct mount point we'll write to. Bundle root + each
    # camera that overrides via output_root.
    targets: dict[Path, list[CameraSpec]] = {}
    targets.setdefault(bundle_root, []).extend(s for s in cameras if s.output_root is None)
    for spec in cameras:
        if spec.output_root is None:
            continue
        target = Path(spec.output_root).expanduser()
        targets.setdefault(target, []).append(spec)

    problems: list[Problem] = []
    for target, specs_at_target in targets.items():
        if not specs_at_target:
            continue
        # Walk up to a directory that exists. If the override target doesn't
        # exist yet, ``disk_usage`` raises; check the closest existing parent.
        check_root = target
        while not check_root.exists() and check_root.parent != check_root:
            check_root = check_root.parent
        if not check_root.exists():
            problems.append(
                Problem(
                    code="disk_target_unreachable",
                    message=f"camera output target {target} has no existing parent",
                    severity="error",
                    blocking=True,
                    metadata={"target": str(target)},
                )
            )
            continue
        if free_bytes_override is not None:
            free = free_bytes_override
        else:
            free = shutil.disk_usage(str(check_root)).free
        fs_type = _filesystem_type(check_root)
        if fs_type in VOLATILE_FILESYSTEM_TYPES:
            mem_available = _mem_available_bytes()
            tightened_free = min(free, mem_available // 2) if mem_available is not None else free
            problems.append(
                Problem(
                    code="disk_target_volatile",
                    message=(
                        f"camera target {check_root} is on {fs_type} — bytes evaporate on "
                        f"reboot and under memory pressure; configure runs_root on persistent "
                        f"storage for production runs"
                    ),
                    severity="warning",
                    blocking=False,
                    metadata={
                        "target": str(check_root),
                        "filesystem_type": fs_type,
                        "free_bytes_reported": free,
                        "free_bytes_tightened": tightened_free,
                        "mem_available_bytes": mem_available,
                    },
                )
            )
            free = tightened_free
        local_projected = sum(int(spec.estimated_bps * duration_s) for spec in specs_at_target)
        local_required = int(local_projected * margin)
        if free < local_required:
            problems.append(
                Problem(
                    code="disk_space_insufficient",
                    message=(
                        f"projected camera writes ({local_projected:_} bytes × {margin}× = "
                        f"{local_required:_} bytes required) exceed free space at "
                        f"{check_root} ({free:_} bytes available)"
                    ),
                    severity="error",
                    blocking=True,
                    metadata={
                        "target": str(check_root),
                        "free_bytes": free,
                        "projected_bytes": local_projected,
                        "required_bytes": local_required,
                        "margin": margin,
                        "duration_s": duration_s,
                        "duration_inferred": duration_inferred,
                        "filesystem_type": fs_type,
                        "cameras": [s.name for s in specs_at_target],
                    },
                )
            )
    return problems


# ---------------------------------------------------------------------------
# Camera task
# ---------------------------------------------------------------------------


async def camera_task(
    camera: Camera,
    *,
    writer: RunBundleWriter,
    output_path: Path,
    clock: RunClock,
    external_stop: anyio.Event,
    logger: structlog.stdlib.BoundLogger,
    on_failure_callback: Callable[[CameraSpec, CameraEvent], Any] | None = None,
    preview_callback: Callable[[str, bytes], None] | None = None,
    camera_event_callback: Callable[[CameraEvent], None] | None = None,
) -> None:
    """Run one camera through the recording lifecycle inside the run task group.

    Sub-tasks under an inner task group:

    * ``run_pump`` (when the camera exposes it) — drives the frame source.
      Pumps exit naturally when ``stop_recording`` flips the camera's
      internal recording flag.
    * Frame drain — :class:`FrameReceipt` → :meth:`writer.record_frame`.
    * Event drain — :class:`CameraEvent` → events.sqlite + on_failure
      escalation.

    Shutdown sequence (deliberate order so the audit trail is complete):

    1. ``external_stop`` fires.
    2. Call ``camera.stop_recording`` — emits the ``recording_stopped``
       event onto the still-open event stream and unblocks the pump's
       ``while self._recording`` loop.
    3. Wait for the pump task to exit naturally.
    4. Call ``camera.close`` — closes the send streams; drain tasks see
       :class:`anyio.EndOfStream` and return.
    5. The inner task group joins.

    Cancellation is reserved for hard aborts only; the normal path drains
    every queued event so ``recording_stopped`` lands in ``events.sqlite``.
    """
    spec = camera.spec
    logger = logger.bind(camera=spec.name)
    has_pump = hasattr(camera, "run_pump") and callable(camera.run_pump)
    pump_done = anyio.Event()
    if not has_pump:
        pump_done.set()

    try:
        await camera.open()
        await camera.start_recording(output_path)
        logger.info(
            "engine.camera.recording",
            adapter=spec.adapter,
            output_path=str(output_path),
        )

        async with anyio.create_task_group() as tg:
            tg.start_soon(_drain_frames, camera, writer, logger)
            tg.start_soon(
                _drain_events,
                camera,
                writer,
                clock,
                spec,
                on_failure_callback,
                camera_event_callback,
                logger,
            )
            if (
                preview_callback is not None
                and CameraCapability.LIVE_PREVIEW in camera.capabilities
            ):
                tg.start_soon(_drain_preview, camera, preview_callback, logger)
            if has_pump:
                tg.start_soon(_run_pump_then_signal, camera, pump_done, logger)

            await external_stop.wait()

            # Stop recording first — flips the camera's internal flag, the
            # pump loop exits, and the recording_stopped event is emitted
            # while the event stream is still open for the drain task.
            try:
                await camera.stop_recording()
            except Exception as exc:
                logger.warning("engine.camera.stop_failed", error=str(exc))

            # Wait for the pump to flush its last frame + the
            # recording_stopped event before closing the send streams.
            with anyio.move_on_after(5.0):
                await pump_done.wait()

            # close() closes the send streams; drain tasks see EndOfStream
            # and return naturally. The inner task group then joins.
            try:
                await camera.close()
            except Exception as exc:
                logger.warning("engine.camera.close_failed", error=str(exc))
    finally:
        # Hard-abort safety net: if we got here via cancellation rather
        # than the normal stop sequence, make sure the .csq is sealed.
        with anyio.CancelScope(shield=True):
            try:
                await camera.stop_recording()
            except Exception as exc:
                logger.warning("engine.camera.stop_failed", error=str(exc))
            try:
                await camera.close()
            except Exception as exc:
                logger.warning("engine.camera.close_failed", error=str(exc))
        logger.info("engine.camera.closed")


async def _run_pump_then_signal(
    camera: Camera,
    done: anyio.Event,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Run the pump and signal completion when it returns."""
    try:
        await _run_pump(camera, logger)
    finally:
        done.set()


async def _drain_frames(
    camera: Camera,
    writer: RunBundleWriter,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Pump frame receipts into the bundle writer's frame-index sink."""
    try:
        async for receipt in camera.frame_stream():
            writer.record_frame(receipt)
    except anyio.get_cancelled_exc_class():
        raise
    except Exception as exc:
        logger.warning("engine.camera.frame_drain_failed", error=str(exc))


async def _drain_events(
    camera: Camera,
    writer: RunBundleWriter,
    clock: RunClock,
    spec: CameraSpec,
    on_failure_callback: Callable[[CameraSpec, CameraEvent], Any] | None,
    camera_event_callback: Callable[[CameraEvent], None] | None,
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Drain camera events into events.sqlite and apply the on_failure policy.

    A camera event with severity ``"error"`` triggers the configured
    on_failure escalation; ``"warning"`` is logged + recorded only.

    The events also fan out to ``camera_event_callback`` (when set) so the
    UI can surface ``pump_warning`` / ``pump_failed`` / ``recording_stopped``
    onto camera-preview tiles. ``events.sqlite`` remains the durable record;
    the callback is a side channel for live UI consumers.
    """
    try:
        async for event in camera.event_stream():
            writer.write_event(
                kind=f"camera.{event.kind}",
                message=event.message,
                severity=event.severity,
                source=f"camera:{spec.name}",
                t_mono_ns=event.t_mono_ns,
                t_utc=event.t_utc,
                metadata={"adapter": spec.adapter, **event.metadata},
            )
            if camera_event_callback is not None:
                try:
                    camera_event_callback(event)
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning("engine.camera.event_callback_failed", error=str(exc))
            if event.severity == "error":
                logger.error(
                    "engine.camera.failure",
                    kind=event.kind,
                    on_failure=spec.on_failure,
                    message=event.message,
                )
                if on_failure_callback is not None:
                    on_failure_callback(spec, event)
    except anyio.get_cancelled_exc_class():
        raise
    except Exception as exc:
        logger.warning("engine.camera.event_drain_failed", error=str(exc))


async def _drain_preview(
    camera: Camera,
    callback: Callable[[str, bytes], None],
    logger: structlog.stdlib.BoundLogger,
) -> None:
    """Forward JPEG preview bytes to the UI via ``callback``.

    DROP_OLDEST is enforced upstream by the adapter's bounded preview stream;
    the engine just relays. Exceptions are swallowed so a flaky preview
    consumer never escalates into a recording failure.
    """
    try:
        async for jpeg in camera.preview_stream():
            try:
                callback(camera.spec.name, jpeg)
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning("engine.camera.preview_callback_failed", error=str(exc))
    except anyio.get_cancelled_exc_class():
        raise
    except Exception as exc:
        logger.warning("engine.camera.preview_drain_failed", error=str(exc))


async def _run_pump(camera: Camera, logger: structlog.stdlib.BoundLogger) -> None:
    """Run the camera's frame pump until cancellation or natural exit."""
    try:
        await camera.run_pump()  # type: ignore[attr-defined]
    except anyio.get_cancelled_exc_class():
        raise
    except Exception as exc:
        logger.warning("engine.camera.pump_failed", error=str(exc))


async def _wait_stop(external_stop: anyio.Event, scope: anyio.CancelScope) -> None:
    """Cancel the camera task group when the engine signals stop."""
    await external_stop.wait()
    scope.cancel()


__all__ = [
    "DEFAULT_DISK_FREE_MARGIN",
    "DEFAULT_FALLBACK_DURATION_S",
    "camera_output_path",
    "camera_task",
    "construct_cameras",
    "disk_space_preflight_problems",
]
