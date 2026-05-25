"""Deterministic signal generators for sim adapters.

Each generator is a stateless callable ``(t_s) -> float`` keyed off
:class:`~capa.core.clock.RunClock` time. Tests use these to assert that
adapters emit the expected values; UI/integration runs use them to populate
plausible-looking traces.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Constant:
    """Constant signal: ``f(t) = value`` regardless of ``t``."""

    value: float
    kind: str = "constant"

    def __call__(self, t_s: float) -> float:
        return self.value


@dataclass(frozen=True, slots=True)
class Sine:
    """``offset + amplitude * sin(2π·frequency·t + phase)``."""

    amplitude: float
    frequency_hz: float
    offset: float = 0.0
    phase: float = 0.0
    kind: str = "sine"

    def __call__(self, t_s: float) -> float:
        return self.offset + self.amplitude * math.sin(
            2 * math.pi * self.frequency_hz * t_s + self.phase
        )


@dataclass(frozen=True, slots=True)
class Ramp:
    """Linear ramp from ``start`` toward ``end`` over ``duration_s``;
    holds at ``end`` after."""

    start: float
    end: float
    duration_s: float
    kind: str = "ramp"

    def __call__(self, t_s: float) -> float:
        if t_s <= 0:
            return self.start
        if t_s >= self.duration_s:
            return self.end
        return self.start + (self.end - self.start) * (t_s / self.duration_s)


@dataclass(frozen=True, slots=True)
class Step:
    """``before`` until ``at_s``, then ``after``."""

    before: float
    after: float
    at_s: float
    kind: str = "step"

    def __call__(self, t_s: float) -> float:
        return self.after if t_s >= self.at_s else self.before


SignalFn = Constant | Sine | Ramp | Step
"""Callable union; concrete sims accept any of these."""


# ---------------------------------------------------------------------------
# TOML-friendly factory.
#
# Sim-adapter configs in ``configs/hardware/*.toml`` cannot carry Python
# callables. Each adapter's ``from_params`` classmethod accepts a dict of
# signal specs (``{"kind": "sine", "amplitude": 5.0, ...}``) and uses
# :func:`signal_from_dict` to materialise the actual SignalFn instance.
# ---------------------------------------------------------------------------


_SIGNAL_KINDS: dict[str, type] = {
    "constant": Constant,
    "sine": Sine,
    "ramp": Ramp,
    "step": Step,
}


def signal_from_dict(spec: dict[str, object]) -> SignalFn:
    """Build a :class:`SignalFn` from a serialisable dict.

    The spec must include ``kind`` (``constant`` / ``sine`` / ``ramp`` /
    ``step``); the rest of the keys are forwarded as keyword arguments to
    the matching dataclass.

    Examples::

        signal_from_dict({"kind": "constant", "value": 100.0})
        signal_from_dict({"kind": "sine", "amplitude": 5.0,
                          "frequency_hz": 0.05, "offset": 400.0})
        signal_from_dict({"kind": "ramp", "start": 30, "end": 600,
                          "duration_s": 120})

    Unknown ``kind`` raises :class:`ValueError`; unexpected keys raise
    :class:`TypeError` (via the dataclass constructor)."""
    if not isinstance(spec, dict):
        raise TypeError(f"signal spec must be a dict, got {type(spec).__name__}")
    raw_kind = spec.get("kind")
    if not isinstance(raw_kind, str):
        raise ValueError(f"signal spec missing 'kind' string, got {raw_kind!r}")
    cls = _SIGNAL_KINDS.get(raw_kind)
    if cls is None:
        raise ValueError(f"unknown signal kind {raw_kind!r}; valid: {sorted(_SIGNAL_KINDS)}")
    fields = {k: v for k, v in spec.items() if k != "kind"}
    return cls(**fields)  # type: ignore[no-any-return]


def _materialise(spec: object) -> SignalFn:
    """Pass through an already-built :class:`SignalFn`; convert dicts via
    :func:`signal_from_dict`. Tests construct adapters directly with
    instances; the engine's TOML path passes dicts through ``from_params``."""
    if isinstance(spec, Constant | Sine | Ramp | Step):
        return spec
    if isinstance(spec, dict):
        return signal_from_dict(spec)
    raise TypeError(f"signal spec must be a dict or SignalFn, got {type(spec).__name__}")


def signals_from_mapping(mapping: Mapping[str, object]) -> dict[str, SignalFn]:
    """Convert a flat ``{name: spec}`` mapping into ``{name: SignalFn}``.

    ``spec`` may be a serialisable dict or an already-built
    :class:`SignalFn` instance — the latter so existing tests that pass
    realised ``Sine(...)`` callables continue to work."""
    return {name: _materialise(spec) for name, spec in mapping.items()}


def watlow_signals_from_mapping(
    mapping: dict[object, object],
) -> dict[tuple[str, int], SignalFn]:
    """Convert a TOML-friendly mapping into Watlow's
    ``{(parameter, instance): SignalFn}`` shape.

    String keys use the ``"<parameter>/<instance>"`` form
    (``"process_value/1"`` → ``("process_value", 1)``; bare
    ``"process_value"`` defaults instance to ``1``). Already-tupled keys
    pass through unchanged (existing tests use this form)."""
    out: dict[tuple[str, int], SignalFn] = {}
    for raw_key, spec in mapping.items():
        if isinstance(raw_key, tuple):
            parameter, instance = raw_key
            instance = int(instance)
        else:
            assert isinstance(raw_key, str)
            parameter, _, raw_instance = raw_key.partition("/")
            instance = int(raw_instance) if raw_instance else 1
        out[(parameter, instance)] = _materialise(spec)
    return out


__all__ = [
    "Constant",
    "Ramp",
    "SignalFn",
    "Sine",
    "Step",
    "signal_from_dict",
    "signals_from_mapping",
    "watlow_signals_from_mapping",
]
