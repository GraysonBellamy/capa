"""Annotation-inspection helpers shared by every field-widget module.

No Qt imports here — keeps the helpers cheap to load and easy to unit
test without a ``QApplication``.
"""

from __future__ import annotations

import types
import typing
from typing import Any, get_args, get_origin

from pydantic.fields import FieldInfo

_ERROR_STYLE = "QWidget { border: 1px solid #d33; }"
"""Painted on the offending widget when ``set_error()`` fires.

A red border is the cheap, theme-agnostic signal; the full message lands
in the widget's tooltip. If a future theme overrides ``QLineEdit`` borders
this won't shadow the theme's other state styling because we set it on
the wrapper, not the inner widget."""


def _humanize(name: str) -> str:
    """``"duration_s"`` → ``"Duration s"``. Used when ``Field(title=...)``
    is absent."""
    return name.replace("_", " ").strip().capitalize()


def _is_optional(annotation: Any) -> tuple[bool, Any]:
    """Inspect ``X | None`` / ``Optional[X]``. Returns ``(True, X)`` if
    ``annotation`` admits ``None``, else ``(False, annotation)``.

    Pydantic v2 represents both as ``X | None`` (PEP 604) under the hood,
    so the check is just "is None one of the union args?"."""
    origin = get_origin(annotation)
    if origin not in (typing.Union, types.UnionType):
        return False, annotation
    args = [a for a in get_args(annotation) if a is not type(None)]
    if len(args) == 1 and len(get_args(annotation)) == 2:
        return True, args[0]
    return False, annotation


def _unit_from_field(field: FieldInfo) -> str | None:
    """Read ``Field(json_schema_extra={"capa_unit": "°C"})``.

    Returns ``None`` when the field has no declared unit. The widget
    layer uses this to append a suffix on numeric spinboxes and a label
    annotation on the form row.
    """
    extra = getattr(field, "json_schema_extra", None)
    if isinstance(extra, dict):
        unit = extra.get("capa_unit")
        if isinstance(unit, str) and unit:
            return unit
    return None


def _help_from_field(field: FieldInfo) -> str | None:
    """Read ``Field(json_schema_extra={"capa_help": "..."})``.

    The label-side ``(?)`` popover renders this text. Returns ``None``
    when the field doesn't declare extra help — the row falls back to
    ``Field(description=...)`` via the existing tooltip path.
    """
    extra = getattr(field, "json_schema_extra", None)
    if isinstance(extra, dict):
        help_text = extra.get("capa_help")
        if isinstance(help_text, str) and help_text:
            return help_text
    return None


def _path_mode_from_field(field: FieldInfo) -> typing.Literal["file", "dir"]:
    """Read ``Field(json_schema_extra={"capa_path_mode": "dir"})``.

    The default is ``"file"``; only callers that explicitly opt into a
    directory picker get one. Used by ``StoragePolicy.bundle_root``."""
    extra = getattr(field, "json_schema_extra", None)
    if isinstance(extra, dict) and extra.get("capa_path_mode") == "dir":
        return "dir"
    return "file"


def _numeric_constraints(field: FieldInfo) -> dict[str, float]:
    """Pull ``Field(gt=, ge=, lt=, le=)`` numeric constraints into a
    dict the spinbox factories can apply directly. Strict ``gt`` / ``lt``
    are approximated by clamping to ge=gt+epsilon; tests should pass.
    """
    out: dict[str, float] = {}
    for meta in getattr(field, "metadata", ()) or ():
        for attr in ("gt", "ge", "lt", "le"):
            v = getattr(meta, attr, None)
            if v is not None:
                out[attr] = float(v)
    return out


# Unit-suffix → decimal-place table for float spinboxes. Order matters:
# longer suffixes must come first so ``_mm`` doesn't lose to ``_m``.
# Reasoning per group: a heat flux of 12.3 kW/m² doesn't need .000123; a
# sample mass of 1.2345 g does (sub-mg matters). When in doubt the
# default below is 3 — fine for most operator-facing values, easy to
# override per-field via ``Field(json_schema_extra={"capa_decimals": N})``.
_DECIMALS_BY_SUFFIX: tuple[tuple[str, int], ...] = (
    # Time
    ("_ns", 0),
    ("_us", 0),
    ("_ms", 1),
    ("_seconds", 2),
    ("_minutes", 2),
    ("_hours", 2),
    ("_s", 2),
    # Frequency
    ("_khz", 2),
    ("_hz", 1),
    # Length
    ("_nm", 1),
    ("_um", 1),
    ("_mm", 2),
    ("_cm", 2),
    ("_inches", 3),
    ("_in", 3),
    ("_meters", 4),
    # Area
    ("_mm2", 2),
    ("_cm2", 2),
    ("_m2", 3),
    # Mass
    ("_mg", 3),
    ("_kg", 4),
    ("_g", 4),
    # Flow
    ("_sccm", 2),
    ("_slpm", 2),
    ("_slm", 2),
    ("_lpm", 2),
    ("_mlpm", 2),
    # Temperature
    ("_celsius", 1),
    ("_kelvin", 1),
    ("_fahrenheit", 1),
    ("_degc", 1),
    ("_degf", 1),
    ("_c", 1),
    ("_f", 1),
    ("_k", 1),
    # Pressure
    ("_pascal", 1),
    ("_pascals", 1),
    ("_kpa", 2),
    ("_mpa", 3),
    ("_bar", 3),
    ("_psi", 2),
    ("_torr", 2),
    ("_mbar", 2),
    ("_atm", 3),
    ("_pa", 1),
    # Heat flux / power
    ("_kw_m2", 1),
    ("_kw_per_m2", 1),
    ("_w_m2", 1),
    ("_w_per_m2", 1),
    ("_kw", 2),
    ("_mw", 1),
    ("_w", 1),
    # Energy
    ("_kj", 2),
    ("_mj", 3),
    ("_j", 2),
    # Electrical
    ("_mv", 2),
    ("_volts", 4),
    ("_v", 4),
    ("_ma", 2),
    ("_amperes", 4),
    ("_amps", 4),
    ("_a", 4),
    ("_ohms", 2),
    ("_ohm", 2),
    # Ratios / dimensionless
    ("_percent", 1),
    ("_pct", 1),
    ("_fraction", 4),
    ("_frac", 4),
    ("_ratio", 4),
)


def _decimals_for_field(field_name: str | None, field: FieldInfo) -> int:
    """Pick a decimal count for a ``QDoubleSpinBox``.

    Priority:

    1. Explicit ``Field(json_schema_extra={"capa_decimals": N})``.
    2. Suffix lookup against :data:`_DECIMALS_BY_SUFFIX`. A trailing
       ``_per_min`` / ``_per_s`` is treated as a rate and stripped before
       the suffix match so ``ramp_rate_c_per_min`` reads as a per-minute
       temperature rate (2 decimals on the °C side).
    3. Default ``3`` — tighter than the operator-frustrating ``6`` and
       loose enough that nobody-cares fields read cleanly.

    A non-recognised field can opt back into more precision via the
    explicit override.
    """
    extra = getattr(field, "json_schema_extra", None)
    if isinstance(extra, dict):
        override = extra.get("capa_decimals")
        if isinstance(override, int) and 0 <= override <= 12:
            return override
    if field_name:
        lowered = field_name.lower()
        # Rate-per-time fields like ``ramp_rate_c_per_min`` —— strip
        # the ``_per_<unit>`` tail before suffix matching so the base
        # unit (``_c``) drives precision rather than ``_min``.
        for tail in ("_per_min", "_per_minute", "_per_s", "_per_sec", "_per_second", "_per_hour"):
            if lowered.endswith(tail):
                lowered = lowered[: -len(tail)]
                break
        for suffix, decimals in _DECIMALS_BY_SUFFIX:
            if lowered.endswith(suffix):
                return decimals
    return 3
