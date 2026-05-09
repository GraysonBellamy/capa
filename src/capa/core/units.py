"""Pint-backed unit registry, UCUM-aligned canonicalization, and a Pydantic
:class:`UnitStr` annotation.

Operators may type natural strings ("kPa", "deg C", "g/min") in configs; the
loader normalizes them to canonical pint form. Both forms are recorded so the
manifest preserves what the operator typed *and* the canonical name.
"""

from __future__ import annotations

from typing import Annotated, Any

import pint
from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

from capa.core.errors import ConfigError


def _build_registry() -> pint.UnitRegistry:  # type: ignore[type-arg]
    """Build the shared registry.

    UCUM names that pint doesn't ship with are added here. Keep the additions
    minimal — pint already understands almost everything an analyzer cares
    about, including ``degC``, ``degK``, ``kPa``, ``slpm``, ``W/m**2``.
    """
    reg: pint.UnitRegistry = pint.UnitRegistry(  # type: ignore[type-arg]
        autoconvert_offset_to_baseunit=False
    )
    # Common operator aliases; pint accepts most of these natively but a few
    # benefit from explicit mapping.
    reg.define("@alias degC = deg_C = celsius")
    reg.define("@alias degF = deg_F = fahrenheit")
    reg.define("@alias degK = deg_K = kelvin")
    reg.define("@alias percent = pct = %")
    return reg


UNIT_REGISTRY: pint.UnitRegistry = _build_registry()  # type: ignore[type-arg]
"""Process-wide :class:`pint.UnitRegistry`. Tests and adapters share this so
that dimensional comparisons across the application are always against the
same registry instance (pint refuses comparisons between different registries).
"""


_TEMP_ALIASES = {
    "deg c": "degC",
    "deg f": "degF",
    "deg k": "degK",
    "degree c": "degC",
    "degree f": "degF",
    "degree k": "degK",
    "deg_c": "degC",
    "deg_f": "degF",
    "deg_k": "degK",
}


def _preprocess(value: str) -> str:
    """Map common operator strings to canonical pint-friendly forms before parsing.

    Pint parses ``"deg C"`` as ``degree * coulomb``. Operators consistently
    write ``"deg C"`` for Celsius, so map the common variants here. Case-
    insensitive on the alias keys; the rest of the string passes through.
    """
    lowered = value.strip().lower()
    return _TEMP_ALIASES.get(lowered, value)


def parse_unit(value: str) -> pint.Unit:
    """Parse ``value`` as a pint unit, raising :class:`ConfigError` on failure."""
    preprocessed = _preprocess(value)
    try:
        return UNIT_REGISTRY.parse_units(preprocessed)
    except (pint.UndefinedUnitError, pint.DimensionalityError, ValueError) as exc:
        raise ConfigError(f"unknown or ill-formed unit: {value!r}") from exc


def canonicalize_unit(value: str) -> str:
    """Return the canonical pint string for ``value``.

    Round-tripping through the registry collapses aliases to their base form
    (``"deg C" -> "degC"``, ``"kg/m^2" -> "kg / m ** 2"``). Used at config-load
    so manifests record a stable canonical name alongside the operator-typed
    form.
    """
    return str(parse_unit(value))


def units_compatible(a: str, b: str) -> bool:
    """Return ``True`` when ``a`` and ``b`` share dimensionality.

    Used by calibrations: a thermocouple cal that consumes ``V`` and emits ``K``
    is fine; one that consumes ``V`` and emits ``kg`` is a config error.
    """
    ua = parse_unit(a)
    ub = parse_unit(b)
    return ua.dimensionality == ub.dimensionality


def _validate(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"unit must be a string, got {type(value).__name__}")
    if not value.strip():
        raise ValueError("unit string is empty")
    # Validate but return the *original* string; canonicalization is exposed
    # via canonicalize_unit() so call sites that want both can ask explicitly.
    parse_unit(value)
    return value


def _pydantic_schema(_source: type[Any], _handler: GetCoreSchemaHandler) -> CoreSchema:
    return core_schema.no_info_plain_validator_function(
        _validate,
        serialization=core_schema.to_string_ser_schema(),
    )


class _UnitStrMarker:
    """Pydantic marker that runs :func:`parse_unit` on assignment."""

    __get_pydantic_core_schema__ = staticmethod(_pydantic_schema)


UnitStr = Annotated[str, _UnitStrMarker()]
"""A :class:`str` whose value is validated against :data:`UNIT_REGISTRY` at
Pydantic-construction time. Stored as the operator-typed string; pair with
:func:`canonicalize_unit` when persisting.
"""


__all__ = [
    "UNIT_REGISTRY",
    "UnitStr",
    "canonicalize_unit",
    "parse_unit",
    "units_compatible",
]
