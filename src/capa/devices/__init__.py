"""Device adapters: uniform Protocol over the heterogeneous device libraries.

The :data:`ADAPTER_REGISTRY` map is the single source of truth for which
adapter modules ship in the box. ``capa devices discover``, ``capa
validate --strict``, and the engine's adapter dispatch all consume it so the
three views agree without drift.

Out-of-tree adapters can still be referenced by their full module path in
:attr:`HardwareProfile.devices[*].adapter`; the registry just provides the
short-form aliases for the bundled ones.
"""

from __future__ import annotations

from typing import Final

#: Built-in adapter module paths, keyed by the short adapter id used in
#: ``capa devices discover`` output and by the dispatch in plan §5.2. Real
#: adapters live at ``capa.devices.<family>``; sim adapters live at
#: ``capa.devices.sim.<family>_sim``.
ADAPTER_REGISTRY: Final[dict[str, str]] = {
    "watlow": "capa.devices.watlow",
    "alicat": "capa.devices.alicat",
    "sartorius": "capa.devices.sartorius",
    "nidaq": "capa.devices.nidaq",
    "watlow_sim": "capa.devices.sim.watlow_sim",
    "alicat_sim": "capa.devices.sim.alicat_sim",
    "sartorius_sim": "capa.devices.sim.sartorius_sim",
    "nidaq_polled_sim": "capa.devices.sim.nidaq_polled_sim",
    "nidaq_block_sim": "capa.devices.sim.nidaq_block_sim",
}

#: Subset of :data:`ADAPTER_REGISTRY` whose modules expose a real
#: ``discover()`` and ``handshake(params)`` hook (plan §14:
#: ``capa devices discover``, ``capa validate --strict``). Sim adapters are
#: omitted because their discovery is a no-op.
REAL_ADAPTERS: Final[tuple[str, ...]] = ("watlow", "alicat", "sartorius", "nidaq")


__all__ = ["ADAPTER_REGISTRY", "REAL_ADAPTERS"]
