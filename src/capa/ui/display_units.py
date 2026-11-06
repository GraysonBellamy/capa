"""Display-only unit conversions for live readouts.

Channels are acquired and recorded in their declared unit; a few of those
units read badly live. An ambient pressure in Pa is a six-digit number the
numerics dock prints as ``1.008e+05``. The live views show those channels
in a friendlier unit instead, scaling the value at display time only. Ring
buffers, recorded samples and exports keep the declared unit.
"""

from __future__ import annotations

from typing import Final

from capa.core.units import UNIT_REGISTRY, parse_unit

_DISPLAY_UNITS: Final[dict[str, str]] = {"pascal": "kPa"}
"""Canonical pint unit → the unit live views show it in."""


def display_unit(unit: str) -> tuple[str, float]:
    """Return ``(shown_unit, scale)`` for a channel declared in ``unit``.

    Multiply a value in ``unit`` by ``scale`` to get it in ``shown_unit``.
    Units with no display preference come back unchanged with scale 1.
    """
    shown = _DISPLAY_UNITS.get(str(parse_unit(unit)))
    if shown is None:
        return unit, 1.0
    return shown, float(UNIT_REGISTRY.Quantity(1.0, unit).to(shown).magnitude)


__all__ = ["display_unit"]
