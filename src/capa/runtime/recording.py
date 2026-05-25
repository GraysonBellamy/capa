"""Materialised recording-plan types + resolution pipeline.

A run can choose to persist only a subset of the channels/cameras declared
in the hardware profile — useful for calibration procedures (heat-flux
tune, MFC zero, etc.) where most of the rig is idle and recording the
full-rig output is wasted storage.

Two-type split (the operator-intent half lives in
:mod:`capa.experiment.config` so that module doesn't have to import this
one and trigger the :mod:`capa.runtime` package init):

* :class:`~capa.experiment.config.RecordingPolicy` is **operator intent**
  — a short enum carried in :class:`~capa.experiment.config.RunOptions`.
  The Run-tab override checkbox mutates it. Snapshotted into the bundle
  manifest as the audit trail.
* :class:`ResolvedRecordingPlan` is **materialised state** — concrete
  channel/camera names the runtime actually filters against. Resolved
  before workers arm by :func:`resolve_recording_plan`, attached to
  :class:`~capa.runtime.runcontext.RunContext`, immutable for the run.

Filtering happens at two enforcement points:

1. :meth:`capa.runtime.camera_adapter.CameraDeviceAdapter.start` skips
   opening the output file when ``recording_enabled=False``. No 0-byte
   ``video/{name}.csq`` lands in the bundle.
2. :meth:`capa.runtime.conductor.Conductor._dispatch_emission` gates
   :class:`ChannelSample` and :class:`FrameReceipt` against the plan
   before the writer call. The :class:`DataBus` publish and UI mirror
   are **never** gated — safety, procedure subscribers, and operator
   visibility all read live samples.

``SourceRecord`` and ``CameraEvent`` pass through unfiltered in v1
(small payloads, invasive to filter).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from capa.experiment.config import RecordingPolicy
    from capa.experiment.procedures.base import Procedure


class _HardwareNames(Protocol):
    """Surface :func:`default_recording_plan` reads from the rig.

    The full :class:`~capa.experiment.config.HardwareProfile` satisfies
    this; test stubs only need to expose these two readers.
    """

    def channel_names(self) -> tuple[str, ...]: ...

    def camera_names(self) -> tuple[str, ...]: ...


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


PlanSource = Literal["procedure_default", "operator_override"]
"""Audit trail for how the :class:`ResolvedRecordingPlan` was produced.

* ``procedure_default``: came from :meth:`Procedure.plan_capture` (or the
  full-rig fall-through when the procedure didn't override).
* ``operator_override``: the operator selected ``record_all`` on the Run
  tab.
"""


SuppressedReason = Literal["recording_policy"]
"""Why a channel or camera was excluded from the recording. v1 has one
value; future reasons (e.g. ``"device_failed"``) extend the union."""


class ResolvedRecordingPlan(BaseModel):
    """Concrete allowlists the runtime enforces.

    Resolved once at arm time by :func:`resolve_recording_plan` and
    attached to :class:`~capa.runtime.runcontext.RunContext`. Immutable
    for the run.

    ``channel_mode="all"`` means "every channel the hardware declares is
    recorded"; ``"only"`` means "only the names in
    :attr:`recorded_channels`". Same shape for cameras.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel_mode: Literal["all", "only"]
    recorded_channels: tuple[str, ...] = Field(default_factory=tuple)
    """Names included when ``channel_mode="only"``; informational copy of
    the hardware list when ``channel_mode="all"``."""

    camera_mode: Literal["all", "none"]
    recorded_cameras: tuple[str, ...] = Field(default_factory=tuple)
    """Names included when ``camera_mode="all"``; empty when
    ``camera_mode="none"``. v1 has no per-camera allowlist — cameras are
    all-or-nothing. Extend the enum when a real "record 1 of 2 cameras"
    case appears."""

    source: PlanSource
    """How this plan was produced — for the manifest audit trail."""

    native_device_records: Literal["all"] = "all"
    """``SourceRecord`` filtering is deferred to a future version; the
    manifest declares this explicitly so a reader doesn't have to infer
    why a ``device_records/watlow.parquet`` exists in a tune bundle."""

    def allows_channel(self, name: str) -> bool:
        """``True`` if channel ``name`` is in scope for this run's recording plan."""
        if self.channel_mode == "all":
            return True
        return name in self.recorded_channels

    def allows_camera(self, name: str) -> bool:
        """``True`` if camera ``name`` is recorded under this run's plan."""
        if self.camera_mode == "all":
            return True
        return name in self.recorded_cameras


# ---------------------------------------------------------------------------
# Resolution pipeline
# ---------------------------------------------------------------------------


def default_recording_plan(hardware: _HardwareNames) -> ResolvedRecordingPlan:
    """Build the "record everything declared in the rig" plan.

    The procedure's :meth:`plan_capture` receives this as the starting
    point and may return a narrower plan; when ``RecordingPolicy.mode``
    is ``"record_all"``, this is the final plan with
    ``source="operator_override"``.
    """
    return ResolvedRecordingPlan(
        channel_mode="all",
        recorded_channels=hardware.channel_names(),
        camera_mode="all",
        recorded_cameras=hardware.camera_names(),
        source="procedure_default",
    )


def resolve_recording_plan(
    *,
    hardware: _HardwareNames,
    procedure: Procedure | None,
    policy: RecordingPolicy,
) -> ResolvedRecordingPlan:
    """Run the recording-plan resolution pipeline.

    Order:

    1. Build the full-rig default from ``hardware``.
    2. If ``policy.mode == "record_all"``, return the default with
       ``source="operator_override"`` (this skips ``plan_capture``
       intentionally — the operator's override beats the procedure's
       opinion).
    3. Otherwise call :meth:`Procedure.plan_capture`. ``None`` means
       "use the default"; a returned plan is honoured.

    ``procedure`` may be ``None`` for non-procedure-runner paths
    (engine tests, ``NoOpRunner``) — those always get the default plan.
    """
    default = default_recording_plan(hardware)
    if policy.mode == "record_all":
        return default.model_copy(update={"source": "operator_override"})
    if procedure is None:
        return default
    plan_capture = getattr(procedure, "plan_capture", None)
    if not callable(plan_capture):
        return default
    capture = cast(
        Callable[[ResolvedRecordingPlan], ResolvedRecordingPlan | None],
        plan_capture,
    )
    narrowed = capture(default)
    if narrowed is None:
        return default
    return narrowed


__all__ = [
    "PlanSource",
    "ResolvedRecordingPlan",
    "SuppressedReason",
    "default_recording_plan",
    "resolve_recording_plan",
]
