""":func:`build_workers` — group adapters by resource and validate the grouping.

The validation runs **synchronously, before any worker thread spawns**:
a misconfigured config fails fast with no hardware side-effects. Any
:class:`ResourceConflict` raised here propagates out of
:meth:`WorkerPool.open` before the pool's state moves out of CLOSED.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

import structlog

from capa.config.problems import ConfigProblem
from capa.devices.adapter import DeviceAdapter
from capa.devices.camera.base import Camera
from capa.experiment.config import ExperimentConfig
from capa.runtime.bridge import ThreadBridge
from capa.runtime.camera_adapter import CameraDeviceAdapter, make_camera_adapter
from capa.runtime.errors import ResourceConflict
from capa.runtime.preview import PreviewFrame
from capa.runtime.runner import ThreadedRunner, WorkerRunner
from capa.runtime.worker import Worker

_logger = structlog.get_logger("capa.runtime.build")


def build_workers(
    config: ExperimentConfig,
    *,
    runner_factory: Callable[..., WorkerRunner] | None = None,
    outbound_capacity: int = 64,
    preview_bridges: Mapping[str, ThreadBridge[PreviewFrame]] | None = None,
) -> tuple[dict[str, Worker], dict[str, str]]:
    """Build one :class:`Worker` per ``resource_id`` group.

    Returns ``(workers, device_to_resource)`` where:

    * ``workers`` keys are ``resource_id`` strings; values are unstarted
      :class:`Worker` instances.
    * ``device_to_resource`` maps each ``DeviceConfig.name`` / camera
      name to its ``resource_id`` — the lookup the pool uses to route
      :meth:`WorkerPool.dispatch` calls.

    Validation runs *before* any :class:`Worker` is instantiated. A
    :class:`ResourceConflict` raised mid-validation is observable to
    :meth:`WorkerPool.open` and surfaces with the offending adapter
    names, so the operator's TOML fix is unambiguous.

    Cameras are constructed alongside devices, wrapped in
    :class:`CameraDeviceAdapter` (which implements the
    :class:`DeviceAdapter` Protocol over a
    :class:`~capa.devices.camera.base.Camera`), and grouped into
    workers by their ``resource_id`` the same way device adapters are.
    The wrapper translates the camera's ``start_recording`` /
    ``stop_recording`` lifecycle onto the device-adapter
    ``start`` / ``stop`` calls the worker drives.

    ``runner_factory`` defaults to :class:`ThreadedRunner` — production
    use. Tests pass :class:`InlineRunner` (or a class compatible with the
    runner protocol) to get deterministic, single-loop behaviour.

    ``outbound_capacity`` is the worker outbound :class:`ThreadBridge`
    capacity. Currently a fixed value; a future change will derive
    per-worker capacities from ``config.runtime.bridge_capacity_factor
    * rate_hz``.
    """
    device_adapters = _construct_adapters_from_config(config)
    camera_adapters = _construct_camera_adapters_from_config(config)
    all_adapters: list[DeviceAdapter] = [
        *device_adapters,
        *(cast(DeviceAdapter, adapter) for adapter in camera_adapters),
    ]
    _validate_resources(all_adapters, config)

    by_resource: dict[str, list[DeviceAdapter]] = {}
    device_to_resource: dict[str, str] = {}
    for adapter in all_adapters:
        rid = adapter.resource_id
        by_resource.setdefault(rid, []).append(adapter)
        device_to_resource[adapter.name] = rid

    preview_bridges = preview_bridges or {}
    workers: dict[str, Worker] = {}
    for resource_id, group in by_resource.items():
        runner_name = f"worker-{resource_id}"
        runner: WorkerRunner = (
            runner_factory(name=runner_name)
            if runner_factory is not None
            else ThreadedRunner(name=runner_name)
        )
        # Partition preview bridges by adapter name onto each worker.
        # Non-camera workers receive an empty map; camera workers
        # receive the bridge keyed by their camera spec name.
        per_worker_previews = {
            a.name: preview_bridges[a.name] for a in group if a.name in preview_bridges
        }
        workers[resource_id] = Worker(
            resource_id=resource_id,
            adapters=group,
            runner=runner,
            outbound_capacity=outbound_capacity,
            preview_bridges=per_worker_previews,
        )

    _logger.info(
        "runtime.build_workers",
        worker_count=len(workers),
        device_adapter_count=len(device_adapters),
        camera_adapter_count=len(camera_adapters),
        resources=tuple(workers),
    )
    return workers, device_to_resource


def _construct_camera_adapters_from_config(
    config: ExperimentConfig,
) -> list[CameraDeviceAdapter]:
    """Build a :class:`CameraDeviceAdapter` for each ``hardware.cameras`` entry.

    The camera class is read from the adapter's :class:`AdapterDescriptor`
    in the registry (each camera module registers a descriptor whose
    ``adapter_factory`` is the camera class itself). The factory hands
    the wrapper a :class:`_ClockProxy` that the wrapper rebinds at
    :meth:`start` time; cameras themselves never learn that the clock
    is late-bound.
    """
    out: list[CameraDeviceAdapter] = []
    for spec in config.hardware.cameras:
        cls = _resolve_camera_class(spec.adapter)
        out.append(make_camera_adapter(camera_cls=cls, spec=spec))
    return out


def _resolve_camera_class(adapter_id: str) -> type[Camera]:
    """Look up the camera class for ``adapter_id`` via the registry."""
    from capa.devices.registry import require_descriptor  # noqa: PLC0415

    try:
        descriptor = require_descriptor(adapter_id)
    except KeyError as exc:
        raise ResourceConflict(str(exc)) from exc
    factory = descriptor.adapter_factory
    if not isinstance(factory, type):
        raise ResourceConflict(
            f"camera descriptor {adapter_id!r} adapter_factory must be a Camera "
            f"class, got {type(factory).__name__}"
        )
    return cast(type[Camera], factory)


def _construct_adapters_from_config(config: ExperimentConfig) -> list[DeviceAdapter]:
    """Walk ``config.hardware.devices`` and instantiate each declared adapter.

    Each :class:`DeviceConfig.adapter` resolves to an
    :class:`AdapterDescriptor` in the registry; the descriptor's
    ``adapter_factory`` is invoked as
    ``factory(name=..., **params)`` (or its ``from_params`` classmethod
    when present, which sim adapters use to materialise signal dicts).

    A :class:`TypeError` from either call is re-raised as
    :class:`ResourceConflict` so pool-open surfaces config-shaped
    failures alongside resource-ID collisions.
    """
    from capa.devices.registry import require_descriptor  # noqa: PLC0415

    out: list[DeviceAdapter] = []
    channels = list(config.hardware.channels)
    for dev in config.hardware.devices:
        try:
            descriptor = require_descriptor(dev.adapter)
        except KeyError as exc:
            raise ResourceConflict(str(exc)) from exc
        factory = descriptor.adapter_factory
        from_params = getattr(factory, "from_params", None)
        try:
            if callable(from_params):
                adapter = from_params(name=dev.name, **dev.params)
            else:
                adapter = factory(name=dev.name, **dev.params)
        except TypeError as exc:
            raise ResourceConflict(
                f"failed to construct adapter {dev.name!r} ({dev.adapter}): {exc}"
            ) from exc
        # Bind the adapter to its channel specs so streams emit
        # :class:`ChannelSample`\\ s alongside the raw
        # :class:`SourceRecord`\\ s. The conductor does it here, before
        # any worker is built, so the binding is immutable for the
        # worker's lifetime.
        configure = getattr(adapter, "configure_channels", None)
        if callable(configure):
            configure(channels)
        out.append(cast(DeviceAdapter, adapter))
    return out


# ---------------------------------------------------------------------------
# Resource validation.
# ---------------------------------------------------------------------------


def _validate_resources(adapters: Sequence[DeviceAdapter], config: ExperimentConfig) -> None:
    """Run resource-conflict checks; raise on first conflict.

    Boundary wrapper: collects :class:`ConfigProblem`s via
    :func:`collect_resource_problems`, then raises
    :class:`ResourceConflict` for the first error so existing call sites
    keep their exception contract. Setup-editor Layer-4 calls
    :func:`collect_resource_problems` directly.

    The active checks:

    1. **DAQmx physical-channel uniqueness.** Two adapters cannot claim the
       same physical channel (e.g. ``cDAQ1Mod1/ai0``) — DAQmx will throw
       ``-50103 resource reserved`` if attempted.
    2. **Webcam handle uniqueness.** Same ``webcam:N`` may not appear in
       two adapters.
    3. **Global SDK singletons** are recorded into a side log for the
       conductor to include in the bundle manifest.
    """
    problems = collect_resource_problems(adapters, config)
    errors = [p for p in problems if p.severity == "error"]
    if errors:
        first = errors[0]
        raise _resource_conflict_from_problem(first)


def collect_resource_problems(
    adapters: Sequence[DeviceAdapter], config: ExperimentConfig
) -> list[ConfigProblem]:
    """Layer-4 entry point: collect all resource problems without raising.

    Same checks as :func:`_validate_resources`, but each yields a
    :class:`ConfigProblem` instead of raising. The Setup editor's
    validation pipeline calls this; ``build_workers`` calls the raising
    boundary above.
    """
    problems: list[ConfigProblem] = []
    problems.extend(_check_daqmx_channel_uniqueness(adapters))
    problems.extend(_check_webcam_uniqueness(adapters))
    # Global SDK constraints are still logged for the bundle manifest,
    # not reported as problems.
    _record_global_sdk_constraints(adapters, config)
    return problems


def _resource_conflict_from_problem(problem: ConfigProblem) -> ResourceConflict:
    """Promote a resource :class:`ConfigProblem` back to ``ResourceConflict``.

    Preserves ``conflicting_names`` and ``resource_key`` payload so the
    runtime exception payload is unchanged from the pre-refactor shape.
    Auxiliary fields live under ``ConfigProblem.path`` and the message.
    """
    # The collecting checks below stash the conflict metadata into a
    # private extra-field on the dataclass-like model via the message
    # and the path; here we reconstruct ResourceConflict from the
    # problem's well-known fields. ``path`` is
    # ("conflicting_names", a, b) for the two-name pair check.
    names: tuple[str, ...] = ()
    resource_key: str | None = None
    # Convention: collecting helpers below put the names tuple at path[0:2]
    # and the resource key in path[-1].
    if len(problem.path) >= 3 and problem.path[0] == "conflict":
        # path = ("conflict", name_a, name_b, resource_key)
        names = (str(problem.path[1]), str(problem.path[2]))
        if len(problem.path) >= 4:
            resource_key = str(problem.path[3])
    return ResourceConflict(
        problem.message,
        conflicting_names=names,
        resource_key=resource_key,
    )


def _check_daqmx_channel_uniqueness(
    adapters: Sequence[DeviceAdapter],
) -> list[ConfigProblem]:
    """No two adapters may claim the same DAQmx physical channel."""
    problems: list[ConfigProblem] = []
    channel_to_adapter: dict[str, str] = {}
    for adapter in adapters:
        if not adapter.resource_id.startswith("daqmx:"):
            continue
        channels: Sequence[str] = getattr(adapter, "physical_channels", ())
        for ch in channels:
            prev = channel_to_adapter.get(ch)
            if prev is not None and prev != adapter.name:
                problems.append(
                    ConfigProblem(
                        severity="error",
                        code="devices.daqmx_channel_conflict",
                        message=(
                            f"DAQmx physical channel {ch!r} claimed by both "
                            f"{prev!r} and {adapter.name!r}"
                        ),
                        section="devices",
                        path=("conflict", prev, adapter.name, ch),
                    )
                )
            channel_to_adapter[ch] = adapter.name
    return problems


def _check_webcam_uniqueness(adapters: Sequence[DeviceAdapter]) -> list[ConfigProblem]:
    """Same ``webcam:<id>`` may not be claimed by two adapters."""
    problems: list[ConfigProblem] = []
    webcam_to_adapter: dict[str, str] = {}
    for adapter in adapters:
        rid = adapter.resource_id
        if not rid.startswith("webcam:"):
            continue
        prev = webcam_to_adapter.get(rid)
        if prev is not None and prev != adapter.name:
            problems.append(
                ConfigProblem(
                    severity="error",
                    code="cameras.webcam_handle_conflict",
                    message=(
                        f"webcam handle {rid!r} claimed by both {prev!r} and {adapter.name!r}"
                    ),
                    section="cameras",
                    path=("conflict", prev, adapter.name, rid),
                )
            )
        webcam_to_adapter[rid] = adapter.name
    return problems


def _record_global_sdk_constraints(
    adapters: Sequence[DeviceAdapter], config: ExperimentConfig
) -> None:
    """Note global-SDK singletons in the structlog stream.

    NI-DAQmx, PyAV, FLIR Spinnaker have process-singleton state that
    ``resource_id`` cannot isolate. We don't raise here — the bundle's
    ``diagnostics.runtime.global_sdk_constraints`` map records these so
    an operator triaging a crash can see at a glance whether
    process-singleton state was involved.
    """
    constraints: list[dict[str, Any]] = []
    has_daqmx = any(a.resource_id.startswith("daqmx:") for a in adapters)
    if has_daqmx:
        constraints.append(
            {
                "sdk": "ni-daqmx",
                "note": "system handle is process-singleton; reset affects all tasks",
            }
        )
    has_webcam = any(a.resource_id.startswith("webcam:") for a in adapters)
    if has_webcam:
        constraints.append(
            {
                "sdk": "pyav",
                "note": "format registration is one-shot global init",
            }
        )
    if constraints:
        _logger.info(
            "runtime.global_sdk_constraints",
            config_name=getattr(config.hardware, "name", "<unknown>"),
            constraints=constraints,
        )


__all__ = ["build_workers", "collect_resource_problems"]
