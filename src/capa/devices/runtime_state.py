"""Per-adapter runtime-state bookkeeping shared across real polling adapters.

Lifted out of
:mod:`capa.devices.watlow` / :mod:`capa.devices.alicat` /
:mod:`capa.devices.nidaq` / :mod:`capa.devices.sartorius` once the same
fields and methods showed up in two independent pilot refactors.

Each real adapter owns one :class:`AdapterRuntimeState` instance and
delegates lifecycle/snapshot/staleness bookkeeping to it. The struct
covers:

* the :class:`AdapterLifecycle` state machine,
* the captured run :class:`RunClock` (set at ``start()``, cleared on
  ``close()``),
* the per-record sequence counter ``seq``,
* the cadence-bounded ``last_snapshot_t_mono_ns`` tracker for periodic
  :class:`DeviceSnapshot` emission,
* the :class:`LastSampleTracker` consumed by adapter health checks,
* the ``stop_requested`` flag used by ``stream()`` loops to break out
  cooperatively,
* an optional ``recoverable_error_count`` for adapters with
  ``auto_reconnect`` (Alicat, NI-DAQ polled, Sartorius). Adapters
  without auto-reconnect (Watlow) leave the counter at ``0`` — the
  slot is cheap and the alternative (subtyping) would cost more in
  indirection than the unused integer costs in memory.

Vendor-specific state (Watlow's drift quarantine, NI-DAQ's task spec,
Sartorius's wire-spacing tracking, Alicat's device-info refresh) stays
on the adapter. This helper covers only the lifecycle/snapshot bones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from capa.devices._helpers import LastSampleTracker, WatchdogState
from capa.devices.adapter import AdapterLifecycle
from capa.devices.records import DeviceHealth

if TYPE_CHECKING:
    from capa.core.clock import RunClock


@dataclass(slots=True)
class AdapterRuntimeState:
    """Bundle of per-run bookkeeping shared by every polling adapter.

    Constructed once at adapter ``__init__``; reset at each ``start()``
    via :meth:`on_start`. The adapter mutates ``seq`` and
    ``last_snapshot_t_mono_ns`` directly inside its ``stream()`` loop
    and reads them via the property surface below.

    Sentinel for ``last_snapshot_t_mono_ns``: ``-(2**62)``. Forces the
    first stream iteration to satisfy :meth:`snapshot_due` regardless
    of the configured cadence, so the manifest's equipment block sees
    a device-health row before any data lands.
    """

    lifecycle: AdapterLifecycle = field(default_factory=AdapterLifecycle)
    clock: RunClock | None = None
    seq: int = 0
    last_snapshot_t_mono_ns: int = -(2**62)
    last_sample: LastSampleTracker = field(default_factory=LastSampleTracker)
    stop_requested: bool = False
    recoverable_error_count: int = 0

    # ----------------------------------------------- lifecycle transitions

    def on_start(self, clock: RunClock) -> None:
        """Reset per-run bookkeeping and arm the lifecycle.

        Called from :meth:`DeviceAdapter.start` after the adapter has
        captured the run :class:`AdapterStartContext`. Resets all
        per-run state so a re-armed adapter starts clean.
        """
        self.lifecycle.start()
        self.clock = clock
        self.stop_requested = False
        self.last_sample.reset()
        self.recoverable_error_count = 0
        self.last_snapshot_t_mono_ns = -(2**62)

    def request_stop(self) -> bool:
        """Signal the stream loop to exit and transition the lifecycle.

        Returns ``True`` if the request was honored (the adapter was in
        the ``running`` state); ``False`` for an idempotent no-op call
        on a non-running adapter.
        """
        if self.lifecycle.state != "running":
            return False
        self.stop_requested = True
        self.lifecycle.stop()
        return True

    # --------------------------------------------------- snapshot cadence

    def snapshot_due(self, *, period_s: float) -> bool:
        """``True`` when the configured snapshot cadence has elapsed.

        Returns ``False`` when no run clock has been captured (i.e.
        before ``start()``). The sentinel ``last_snapshot_t_mono_ns``
        ensures the first call after ``start()`` always returns ``True``.
        """
        if self.clock is None:
            return False
        elapsed_ns = self.clock.t_mono_ns() - self.last_snapshot_t_mono_ns
        return elapsed_ns >= int(period_s * 1e9)

    # ------------------------------------------------------ silence view

    def watchdog(self, *, device: str, rate_hz: float) -> WatchdogState:
        """Build the :class:`WatchdogState` silence view.

        ``rate_hz`` is the adapter's configured polling cadence; the
        :class:`WatchdogState` derives the silence threshold from it
        (2× the period by default — see :meth:`WatchdogState.is_silent`).
        """
        return WatchdogState(
            device=device,
            last_t_mono_ns=self.last_sample.last_t_mono_ns,
            expected_period_ns=int(1e9 / rate_hz),
            lifecycle_state=self.lifecycle.state,
        )

    # ----------------------------------------------------- health derivation

    def compute_health(
        self,
        *,
        clock: RunClock,
        rate_hz: float,
        stale_multiple: float = 3.0,
    ) -> DeviceHealth:
        """Derive the operator-facing health pill from runtime state.

        Used by adapters with auto-reconnect (Alicat, NI-DAQ polled,
        Sartorius). Watlow inlines a simpler check (it has no
        recoverable-error counter), so it doesn't call this.

        * ``"down"`` — adapter is closed.
        * ``"ok"`` — adapter is open but not yet streaming (no staleness window yet).
        * ``"degraded"`` — auto-reconnect retries have fired *or* the last
          sample is older than ``stale_multiple × (1/rate_hz)``.
        * ``"ok"`` — otherwise.
        """
        if self.lifecycle.state == "closed":
            return "down"
        if self.lifecycle.state == "open":
            return "ok"
        if self.recoverable_error_count > 0:
            return "degraded"
        age_ns = self.last_sample.age_ns(now_t_mono_ns=clock.t_mono_ns())
        if age_ns is not None:
            stale_threshold_ns = int(stale_multiple * (1e9 / rate_hz))
            if age_ns > stale_threshold_ns:
                return "degraded"
        return "ok"


__all__ = ["AdapterRuntimeState"]
