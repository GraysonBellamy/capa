"""Adapter materialization — TOML rows → constructed :class:`ResolvedAdapter`\\ s.

The validation pipeline and the runtime share one materialization path:
both call :func:`materialize_adapters` to turn an
:class:`~capa.experiment.config.ExperimentConfig` into a tuple of
:class:`~capa.devices.resolved.ResolvedAdapter`\\ s plus the
``device → resource_id`` map.

This module owns:

* Adapter factory invocation (via :class:`~capa.devices.registry.AdapterDescriptor`).
* Channel binding (``configure_channels``) so streams emit
  :class:`~capa.devices.records.ChannelSample`\\ s alongside the raw
  :class:`~capa.devices.records.SourceRecord`\\ s.
* Camera wrapper construction (delegates to
  :func:`~capa.runtime.camera_adapter.make_camera_adapter`).
* Resource-conflict detection (DAQmx physical-channel uniqueness, webcam
  handle uniqueness) and global-SDK constraint logging.

Construction failures (unknown adapter id, factory ``TypeError``, camera
kind mismatch) are surfaced as :class:`ConfigMaterializationError`
carrying a list of :class:`~capa.config.problems.ConfigProblem`\\ s. The
validation pipeline catches and unwraps them so they appear alongside
other layer-4 problems; the runtime's :func:`~capa.runtime.build.build_workers`
catches and re-raises the first error so pool-open still fails fast.

Resource grouping conflicts (two adapters claim the same physical channel
or webcam handle) stay as :class:`~capa.runtime.errors.ResourceConflict`
at the raising boundary in :mod:`capa.runtime.build`; this module only
emits them as :class:`ConfigProblem`\\ s.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

import structlog
from pydantic import ValidationError

from capa.config.problems import ConfigProblem
from capa.core.errors import CapaError
from capa.devices.adapter import DeviceAdapter, FailurePolicy
from capa.devices.camera.base import Camera
from capa.devices.resolved import ResolvedAdapter

if TYPE_CHECKING:
    from collections.abc import Mapping

    from capa.experiment.config import ExperimentConfig

_logger = structlog.get_logger("capa.devices.materialize")


# ---------------------------------------------------------------------------
# Public dataclass and exception.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MaterializedHardware:
    """All adapters constructed from an :class:`ExperimentConfig`.

    Fields:

    * :attr:`adapters` — every constructed adapter wrapped in a
      :class:`ResolvedAdapter` carrying the operator-declared
      :attr:`resource_id` override (if any) and :attr:`on_failure` policy.
      Device adapters come first, cameras second; order otherwise matches
      the source config.
    * :attr:`device_to_resource` — flat name → ``resource_id`` map. The
      key namespace is shared between devices and cameras; the pool's
      :meth:`~capa.runtime.pool.WorkerPool.dispatch` reads this to route
      commands.
    * :attr:`hardware_name` — :attr:`HardwareProfile.name`, retained so
      resource-validation logging can attribute SDK constraints without
      re-reading the source config.
    """

    adapters: tuple[ResolvedAdapter, ...]
    device_to_resource: Mapping[str, str]
    hardware_name: str


class ConfigMaterializationError(CapaError):
    """One or more adapters could not be constructed from the config.

    Carries a list of :class:`ConfigProblem`\\ s so the validation
    pipeline can surface them all (Layer 4) and the runtime's
    :func:`~capa.runtime.build.build_workers` can raise the first error
    via :class:`~capa.runtime.errors.ResourceConflict` for pool-open.

    Distinct from :class:`~capa.runtime.errors.ResourceConflict`, which
    fires only on grouping conflicts (two adapters share a physical
    channel / webcam handle) — those are detected *after* materialization
    succeeds.
    """

    def __init__(self, problems: Sequence[ConfigProblem]) -> None:
        if not problems:
            raise ValueError("ConfigMaterializationError requires at least one problem")
        msg = "; ".join(p.message for p in problems)
        super().__init__(msg)
        self.problems: tuple[ConfigProblem, ...] = tuple(problems)


# ---------------------------------------------------------------------------
# Materialization — devices and cameras.
# ---------------------------------------------------------------------------


def materialize_adapters(config: ExperimentConfig) -> MaterializedHardware:
    """Construct every device and camera adapter declared in ``config``.

    Raises :class:`ConfigMaterializationError` if any adapter cannot be
    constructed (unknown adapter id, factory ``TypeError``, camera kind
    mismatch). The exception carries one :class:`ConfigProblem` per
    failure so callers that want the full list (validation Layer 4) can
    surface them all.

    Construction is **passive**: adapter ``__init__`` is documented to
    perform no I/O so the validation editor's dry-run path can call this
    safely. The contract is enforced by the
    :class:`~capa.devices.registry.AdapterDescriptor` registry.
    """
    problems: list[ConfigProblem] = []

    device_resolved, device_problems = _materialize_devices(config)
    problems.extend(device_problems)

    camera_resolved, camera_problems = _materialize_cameras(config)
    problems.extend(camera_problems)

    if problems:
        raise ConfigMaterializationError(problems)

    adapters: tuple[ResolvedAdapter, ...] = (*device_resolved, *camera_resolved)
    device_to_resource: dict[str, str] = {r.name: r.resource_id for r in adapters}
    return MaterializedHardware(
        adapters=adapters,
        device_to_resource=MappingProxyType(device_to_resource),
        hardware_name=getattr(config.hardware, "name", "<unknown>"),
    )


def _materialize_devices(
    config: ExperimentConfig,
) -> tuple[list[ResolvedAdapter], list[ConfigProblem]]:
    """Construct adapters declared in ``hardware.devices``.

    Resolution rules:

    * :attr:`ResolvedAdapter.resource_id` is the explicit
      :attr:`~capa.experiment.config.DeviceConfig.resource_id` override
      when set; otherwise the adapter's own
      :attr:`DeviceAdapter.resource_id` (the historical default).
    * :attr:`ResolvedAdapter.on_failure` is the operator-declared
      :class:`FailurePolicy` from the config.
    * :attr:`ResolvedAdapter.expected_rate_hz` is captured at resolve
      time so the bridge sizing in
      :func:`~capa.runtime.build.build_workers` does not have to re-probe
      each adapter.

    Returns ``(resolved, problems)``. A problem in the list means that
    adapter could not be constructed and is omitted from ``resolved``;
    downstream resource checks should still run on whatever did
    construct successfully so the operator sees the full picture.
    """
    from capa.devices.registry import require_descriptor  # noqa: PLC0415

    resolved: list[ResolvedAdapter] = []
    problems: list[ConfigProblem] = []
    channels = list(config.hardware.channels)
    for dev in config.hardware.devices:
        try:
            descriptor = require_descriptor(dev.adapter)
        except KeyError as exc:
            problems.append(
                ConfigProblem(
                    severity="error",
                    code="devices.unknown_adapter",
                    message=str(exc),
                    section="devices",
                    path=("devices", dev.name, "adapter"),
                )
            )
            continue
        factory = descriptor.adapter_factory
        from_params = getattr(factory, "from_params", None)
        try:
            if callable(from_params):
                adapter = from_params(name=dev.name, **dev.params)
            else:
                adapter = factory(name=dev.name, **dev.params)
        except (TypeError, ValidationError) as exc:
            problems.append(
                ConfigProblem(
                    severity="error",
                    code="devices.adapter_construction_failed",
                    message=(f"failed to construct adapter {dev.name!r} ({dev.adapter}): {exc}"),
                    section="devices",
                    path=("devices", dev.name),
                )
            )
            continue

        # Bind the adapter to its channel specs so streams emit
        # ChannelSamples alongside the raw SourceRecords. Done here,
        # before any worker is built, so the binding is immutable for
        # the worker's lifetime.
        configure = getattr(adapter, "configure_channels", None)
        if callable(configure):
            configure(channels)

        device_adapter = cast(DeviceAdapter, adapter)
        resource_id = dev.resource_id or device_adapter.resource_id
        expected_rate = getattr(device_adapter, "expected_emission_rate_hz", None)
        resolved.append(
            ResolvedAdapter(
                name=dev.name,
                adapter=device_adapter,
                resource_id=resource_id,
                on_failure=dev.on_failure,
                expected_rate_hz=expected_rate,
            )
        )
    return resolved, problems


def _materialize_cameras(
    config: ExperimentConfig,
) -> tuple[list[ResolvedAdapter], list[ConfigProblem]]:
    """Build a :class:`ResolvedAdapter` for each ``hardware.cameras`` entry.

    Cameras share the resolved-adapter shape with devices; the
    :class:`~capa.runtime.camera_adapter.CameraDeviceAdapter` wrapper makes
    the underlying :class:`Camera` look like a :class:`DeviceAdapter` to
    the worker. Camera spec lacks a ``resource_id`` override field today,
    so the resource id is always taken from the wrapper's
    :attr:`CameraDeviceAdapter.resource_id`. Camera failure policy lives
    on :attr:`CameraSpec.on_failure` (a separate safety-system-facing
    enum) and is not unified with the device :class:`FailurePolicy` here;
    cameras default to :attr:`FailurePolicy.ABORT` for runtime
    failure-policy metadata.
    """
    # Camera wrapper construction lives in the runtime layer because the
    # wrapper holds runtime-owned ThreadBridges. Importing here is one
    # carefully-bounded device → runtime edge; the runtime canary forbids
    # only `capa.runtime.build`, not all of runtime.
    from capa.runtime.camera_adapter import make_camera_adapter  # noqa: PLC0415

    resolved: list[ResolvedAdapter] = []
    problems: list[ConfigProblem] = []
    for spec in config.hardware.cameras:
        try:
            cls = _resolve_camera_class(spec.adapter)
        except ConfigMaterializationError as exc:
            # _resolve_camera_class raises with a single problem; lift it
            # into the per-camera list so the caller can keep going.
            problems.extend(exc.problems)
            continue
        try:
            wrapper = make_camera_adapter(camera_cls=cls, spec=spec)
        except ValueError as exc:
            # Camera kind mismatch between spec and adapter.
            problems.append(
                ConfigProblem(
                    severity="error",
                    code="cameras.kind_mismatch",
                    message=str(exc),
                    section="cameras",
                    path=("cameras", spec.name),
                )
            )
            continue
        except TypeError as exc:
            problems.append(
                ConfigProblem(
                    severity="error",
                    code="cameras.adapter_construction_failed",
                    message=(f"failed to construct camera {spec.name!r} ({spec.adapter}): {exc}"),
                    section="cameras",
                    path=("cameras", spec.name),
                )
            )
            continue
        expected_rate = getattr(wrapper, "expected_emission_rate_hz", None)
        resolved.append(
            ResolvedAdapter(
                name=spec.name,
                adapter=cast(DeviceAdapter, wrapper),
                resource_id=wrapper.resource_id,
                on_failure=FailurePolicy.ABORT,
                expected_rate_hz=expected_rate,
            )
        )
    return resolved, problems


def _resolve_camera_class(adapter_id: str) -> type[Camera]:
    """Look up the camera class for ``adapter_id`` via the registry.

    Raises :class:`ConfigMaterializationError` if the adapter id is
    unknown or the descriptor's factory is not a :class:`Camera`
    subclass.
    """
    from capa.devices.registry import require_descriptor  # noqa: PLC0415

    try:
        descriptor = require_descriptor(adapter_id)
    except KeyError as exc:
        raise ConfigMaterializationError(
            [
                ConfigProblem(
                    severity="error",
                    code="cameras.unknown_adapter",
                    message=str(exc),
                    section="cameras",
                    path=("cameras", adapter_id),
                )
            ]
        ) from exc
    factory = descriptor.adapter_factory
    if not isinstance(factory, type):
        raise ConfigMaterializationError(
            [
                ConfigProblem(
                    severity="error",
                    code="cameras.invalid_factory",
                    message=(
                        f"camera descriptor {adapter_id!r} adapter_factory must be a "
                        f"Camera class, got {type(factory).__name__}"
                    ),
                    section="cameras",
                    path=("cameras", adapter_id),
                )
            ]
        )
    return cast(type[Camera], factory)


# ---------------------------------------------------------------------------
# Resource-conflict collection.
# ---------------------------------------------------------------------------


def collect_resource_problems(
    materialized: MaterializedHardware,
) -> list[ConfigProblem]:
    """Layer-4 entry point: every resource-level :class:`ConfigProblem`.

    Active checks:

    1. **DAQmx physical-channel uniqueness.** Two adapters cannot claim
       the same physical channel (e.g. ``cDAQ1Mod1/ai0``) — DAQmx will
       throw ``-50103 resource reserved`` if attempted.
    2. **Webcam handle uniqueness.** Same ``webcam:<id>`` may not appear
       in two adapters.
    3. **Global SDK singletons** are recorded into the structlog stream
       for post-run diagnostics.

    Never raises — every conflict yields a :class:`ConfigProblem`. The
    raising boundary lives at
    :func:`~capa.runtime.build.build_workers`, which calls this and
    raises :class:`~capa.runtime.errors.ResourceConflict` on the first
    error so pool-open keeps its exception contract.
    """
    resolved = materialized.adapters
    problems: list[ConfigProblem] = []
    problems.extend(_check_daqmx_channel_uniqueness(resolved))
    problems.extend(_check_webcam_uniqueness(resolved))
    _record_global_sdk_constraints(resolved, materialized.hardware_name)
    return problems


def _check_daqmx_channel_uniqueness(
    resolved: Sequence[ResolvedAdapter],
) -> list[ConfigProblem]:
    """No two adapters may claim the same DAQmx physical channel."""
    problems: list[ConfigProblem] = []
    channel_to_adapter: dict[str, str] = {}
    for r in resolved:
        if not r.resource_id.startswith("daqmx:"):
            continue
        channels: Sequence[str] = getattr(r.adapter, "physical_channels", ())
        for ch in channels:
            prev = channel_to_adapter.get(ch)
            if prev is not None and prev != r.name:
                problems.append(
                    ConfigProblem(
                        severity="error",
                        code="devices.daqmx_channel_conflict",
                        message=(
                            f"DAQmx physical channel {ch!r} claimed by both {prev!r} and {r.name!r}"
                        ),
                        section="devices",
                        path=("conflict", prev, r.name, ch),
                    )
                )
            channel_to_adapter[ch] = r.name
    return problems


def _check_webcam_uniqueness(
    resolved: Sequence[ResolvedAdapter],
) -> list[ConfigProblem]:
    """Same ``webcam:<id>`` may not be claimed by two adapters."""
    problems: list[ConfigProblem] = []
    webcam_to_adapter: dict[str, str] = {}
    for r in resolved:
        if not r.resource_id.startswith("webcam:"):
            continue
        prev = webcam_to_adapter.get(r.resource_id)
        if prev is not None and prev != r.name:
            problems.append(
                ConfigProblem(
                    severity="error",
                    code="cameras.webcam_handle_conflict",
                    message=(
                        f"webcam handle {r.resource_id!r} claimed by both {prev!r} and {r.name!r}"
                    ),
                    section="cameras",
                    path=("conflict", prev, r.name, r.resource_id),
                )
            )
        webcam_to_adapter[r.resource_id] = r.name
    return problems


def _record_global_sdk_constraints(resolved: Sequence[ResolvedAdapter], hardware_name: str) -> None:
    """Note global-SDK singletons in the structlog stream.

    NI-DAQmx, PyAV, FLIR Spinnaker have process-singleton state that
    ``resource_id`` cannot isolate. We don't raise here; we emit a
    structured log event so an operator triaging a crash can see whether
    process-singleton state was involved.
    """
    constraints: list[dict[str, Any]] = []
    has_daqmx = any(r.resource_id.startswith("daqmx:") for r in resolved)
    if has_daqmx:
        constraints.append(
            {
                "sdk": "ni-daqmx",
                "note": "system handle is process-singleton; reset affects all tasks",
            }
        )
    has_webcam = any(r.resource_id.startswith("webcam:") for r in resolved)
    if has_webcam:
        constraints.append(
            {
                "sdk": "pyav",
                "note": "format registration is one-shot global init",
            }
        )
    if constraints:
        _logger.info(
            "devices.materialize.global_sdk_constraints",
            config_name=hardware_name,
            constraints=constraints,
        )


__all__ = [
    "ConfigMaterializationError",
    "MaterializedHardware",
    "collect_resource_problems",
    "materialize_adapters",
]
