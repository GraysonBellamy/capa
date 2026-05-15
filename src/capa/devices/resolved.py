""":class:`ResolvedAdapter` — constructed adapter + runtime metadata.

The runtime stack receives :class:`ResolvedAdapter` instances rather
than raw :class:`~capa.devices.adapter.DeviceAdapter`\\ s. The wrapper
carries the metadata the runtime needs but the adapter Protocol does
not own: an authoritative :attr:`resource_id` (which may override the
adapter's default), the operator-declared :attr:`on_failure` policy,
and the producer-side :attr:`expected_rate_hz` used to size the
outbound bridge.

Materialising :class:`ResolvedAdapter`\\ s from
:class:`~capa.experiment.config.ExperimentConfig` lives in
:mod:`capa.devices.materialize` so the config-validation pipeline and
the runtime share one path.
"""

from __future__ import annotations

from dataclasses import dataclass

from capa.devices.adapter import DeviceAdapter, FailurePolicy


@dataclass(frozen=True, slots=True)
class ResolvedAdapter:
    """A constructed :class:`DeviceAdapter` plus the metadata the runtime
    grouped / sized / policed it with.

    Fields:

    * :attr:`name` — adapter-assigned device name. Stable id used by the
      worker pool's ``device_to_resource`` map and by manual dispatch.
    * :attr:`adapter` — the underlying :class:`DeviceAdapter` instance.
      The worker calls into this for ``open`` / ``start`` / ``stream`` /
      etc.; runtime metadata code reads the other fields here instead of
      reaching into the adapter.
    * :attr:`resource_id` — authoritative resource id used for worker
      grouping. Either the explicit
      :attr:`~capa.experiment.config.DeviceConfig.resource_id` override
      or, when that is ``None``, the adapter's declared
      :attr:`DeviceAdapter.resource_id`.
    * :attr:`on_failure` — failure policy from the operator's
      :class:`~capa.experiment.config.DeviceConfig`. It is resolved
      metadata today; runtime enforcement will attach in a future
      per-device failure policy pass.
    * :attr:`expected_rate_hz` — adapter's declared
      :attr:`DeviceAdapter.expected_emission_rate_hz`, captured here so
      ``build_workers`` can size the outbound bridge without re-probing
      every adapter.
    """

    name: str
    adapter: DeviceAdapter
    resource_id: str
    on_failure: FailurePolicy
    expected_rate_hz: float | None


__all__ = ["ResolvedAdapter"]
