"""Load / save / diff helpers for :class:`CalibrationSet`.

The Setup tab's Calibration section uses these helpers to:

* read a set from a TOML file (``configs/calibrations/*.toml``);
* render a *diff* between the set and the channels currently in the
  draft, so the operator sees what would change before committing;
* write the current draft's per-channel calibrations back out as a
  reusable set (with an operator-supplied ``name`` + ``revision``).

The functions are pure-Python so they can be unit-tested without Qt.
The Qt dialog (``setup_calibration_set_diff.py``) consumes
:func:`diff_set_against_channels` and renders the result.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import TypeAdapter

from capa.channels.calibration import Calibration, CalibrationSet

# ---------------------------------------------------------------------------
# Diff model.
# ---------------------------------------------------------------------------


DiffKind = Literal[
    "override_identity",  # channel currently Identity; set provides a real curve
    "override_existing",  # channel currently has a non-Identity curve; set differs
    "matches",  # channel calibration already matches the set's entry
    "set_only",  # set has a curve for a channel the draft doesn't define
    "channel_only",  # draft has a channel the set doesn't cover
]
"""Five-class taxonomy expanded slightly for the actionable cases —
the dialog renders these into
checkbox rows with sensible defaults (override_identity is pre-
checked; override_existing and the missing-side rows are not)."""


@dataclass(frozen=True, slots=True)
class CalibrationDiffEntry:
    """One row in the apply-set diff dialog."""

    kind: DiffKind
    channel_name: str
    current_calibration: dict[str, Any] | None
    """``None`` for ``set_only`` rows — the draft has no such channel."""
    set_calibration: dict[str, Any] | None
    """``None`` for ``channel_only`` rows."""

    @property
    def actionable(self) -> bool:
        """Whether the row offers a meaningful "apply" toggle.

        ``matches`` and ``channel_only`` rows are informational — the
        dialog still shows them so operators see the full picture, but
        they don't produce a clone on accept."""
        return self.kind in ("override_identity", "override_existing")

    @property
    def recommended(self) -> bool:
        """Whether the apply-checkbox should be pre-checked.

        Identity-override is the safe default — the channel had no
        meaningful calibration before, so applying the set's value
        cannot make things *worse*. Overrides of existing non-Identity
        curves are not pre-checked because that is a destructive
        action (operators have lost characterisation work this way)."""
        return self.kind == "override_identity"


# ---------------------------------------------------------------------------
# Diff.
# ---------------------------------------------------------------------------


def diff_set_against_channels(
    *,
    set_curves: dict[str, Calibration],
    channels: list[dict[str, Any]],
) -> list[CalibrationDiffEntry]:
    """Classify every (set, channel) pair into a :class:`DiffKind`.

    Returns the entries in a deterministic order: actionable rows first
    (override_identity, then override_existing), then matches, then
    set_only, then channel_only. Within each group the channels are
    sorted by name so the dialog ordering stays stable across runs.
    """
    channels_by_name: dict[str, dict[str, Any]] = {}
    for ch in channels:
        name = ch.get("name")
        if isinstance(name, str):
            channels_by_name[name] = ch

    entries: list[CalibrationDiffEntry] = []
    set_curve_dicts = {name: _calibration_to_dict(cal) for name, cal in set_curves.items()}

    # Rows that exist in both.
    for name in sorted(channels_by_name.keys() & set_curves.keys()):
        current_dict = channels_by_name[name].get("calibration")
        set_dict = set_curve_dicts[name]
        if not isinstance(current_dict, dict):
            current_dict = None
        if _calibrations_equivalent(current_dict, set_dict):
            entries.append(
                CalibrationDiffEntry(
                    kind="matches",
                    channel_name=name,
                    current_calibration=current_dict,
                    set_calibration=set_dict,
                )
            )
        elif _is_identity(current_dict):
            entries.append(
                CalibrationDiffEntry(
                    kind="override_identity",
                    channel_name=name,
                    current_calibration=current_dict,
                    set_calibration=set_dict,
                )
            )
        else:
            entries.append(
                CalibrationDiffEntry(
                    kind="override_existing",
                    channel_name=name,
                    current_calibration=current_dict,
                    set_calibration=set_dict,
                )
            )

    for name in sorted(set_curves.keys() - channels_by_name.keys()):
        entries.append(
            CalibrationDiffEntry(
                kind="set_only",
                channel_name=name,
                current_calibration=None,
                set_calibration=set_curve_dicts[name],
            )
        )

    for name in sorted(channels_by_name.keys() - set_curves.keys()):
        current = channels_by_name[name].get("calibration")
        entries.append(
            CalibrationDiffEntry(
                kind="channel_only",
                channel_name=name,
                current_calibration=current if isinstance(current, dict) else None,
                set_calibration=None,
            )
        )

    # Order: override_identity, override_existing, matches, set_only, channel_only.
    kind_order = {
        "override_identity": 0,
        "override_existing": 1,
        "matches": 2,
        "set_only": 3,
        "channel_only": 4,
    }
    entries.sort(key=lambda e: (kind_order[e.kind], e.channel_name))
    return entries


def _is_identity(cal_dict: dict[str, Any] | None) -> bool:
    return isinstance(cal_dict, dict) and cal_dict.get("kind") == "identity"


def _calibration_to_dict(cal: Calibration) -> dict[str, Any]:
    return cal.model_dump(mode="python")


def _calibrations_equivalent(current: dict[str, Any] | None, target: dict[str, Any]) -> bool:
    """Compare a draft channel's raw calibration dict against a
    Pydantic-dumped set entry.

    Draft dicts often omit optional fields (``uncertainty``, etc.) that
    Pydantic's :meth:`model_dump` emits as ``None``. Normalise both
    sides through the same :class:`Calibration` adapter so equivalent
    curves compare equal regardless of which fields the operator
    happened to type in.
    """
    if current is None:
        return False
    adapter: TypeAdapter[Calibration] = TypeAdapter(Calibration)
    try:
        normalised_current: Calibration = adapter.validate_python(current)
    except Exception:
        return False
    try:
        normalised_target: Calibration = adapter.validate_python(target)
    except Exception:
        return False
    return normalised_current == normalised_target


# ---------------------------------------------------------------------------
# IO.
# ---------------------------------------------------------------------------


def load_calibration_set(path: Path) -> CalibrationSet:
    """Read a calibration-set TOML file."""
    text = path.read_bytes()
    payload = tomllib.loads(text.decode("utf-8"))
    return CalibrationSet.model_validate(payload)


def save_calibration_set(path: Path, cs: CalibrationSet) -> None:
    """Write a CalibrationSet to TOML.

    Uses :mod:`tomli_w` if available; otherwise falls back to a hand-
    rolled writer that produces deterministic output. The fallback
    handles the dataset shape capa actually ships (no nested arrays
    of tables beyond what :class:`CalibrationSet` declares).

    ``None``-valued optional fields are stripped before serialisation
    because TOML has no null literal and ``tomli_w`` raises on bare
    ``None``.
    """
    payload = _strip_none(cs.model_dump(mode="python"))
    try:
        import tomli_w  # noqa: PLC0415

        path.write_bytes(tomli_w.dumps(payload).encode("utf-8"))
        return
    except ImportError:
        pass
    path.write_text(_tomli_w_fallback(payload), encoding="utf-8")


def _strip_none(value: Any) -> Any:
    """Recursively drop ``None``-valued mapping entries (TOML has no
    null literal). Lists/tuples are left intact; only mappings get
    pruned."""
    if isinstance(value, dict):
        return {k: _strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_strip_none(v) for v in value)
    return value


def build_set_from_channels(
    *,
    name: str,
    revision: str,
    channels: list[dict[str, Any]],
) -> CalibrationSet:
    """Compose a :class:`CalibrationSet` from the channels in the draft.

    Channels whose ``calibration`` is missing or malformed are skipped.
    Useful for the Setup tab's "Export current as set…" action.
    """
    adapter: TypeAdapter[Calibration] = TypeAdapter(Calibration)
    curves: dict[str, Calibration] = {}
    for ch in channels:
        cname = ch.get("name")
        cal = ch.get("calibration")
        if not isinstance(cname, str) or not isinstance(cal, dict):
            continue
        try:
            curves[cname] = adapter.validate_python(cal)
        except Exception:
            continue
    return CalibrationSet(name=name, revision=revision, curves=curves)


def apply_diff_selection(
    *,
    channels: list[dict[str, Any]],
    entries: list[CalibrationDiffEntry],
    selected_names: set[str],
) -> int:
    """Mutate ``channels`` in place: for each entry whose channel name
    is in ``selected_names``, replace the channel's ``calibration``
    with the set's value.

    Returns the number of channels whose calibration was changed.
    ``set_only`` rows are skipped silently — the draft doesn't have a
    channel to attach the calibration to.
    """
    set_dict_by_name: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if entry.set_calibration is not None:
            set_dict_by_name[entry.channel_name] = entry.set_calibration

    changed = 0
    for ch in channels:
        cname = ch.get("name")
        if not isinstance(cname, str) or cname not in selected_names:
            continue
        new_cal = set_dict_by_name.get(cname)
        if new_cal is None:
            continue
        ch["calibration"] = dict(new_cal)
        changed += 1
    return changed


# ---------------------------------------------------------------------------
# tomli_w fallback (deliberately minimal — covers the CalibrationSet schema).
# ---------------------------------------------------------------------------


def _tomli_w_fallback(payload: dict[str, Any]) -> str:
    """Minimal TOML writer for the shape produced by
    :meth:`CalibrationSet.model_dump`.

    Used only when ``tomli_w`` isn't installed (capa-flir vs. capa
    core have slightly different deps). The fallback covers exactly
    what we emit: top-level scalars + a single nested ``curves``
    dict mapping channel names to calibration sub-dicts.
    """
    out: list[str] = []
    for key, value in payload.items():
        if isinstance(value, dict):
            continue
        out.append(f"{key} = {_to_toml_value(value)}")
    out.append("")
    curves = payload.get("curves", {})
    if isinstance(curves, dict):
        for channel_name in sorted(curves.keys()):
            cal = curves[channel_name]
            out.append(f"[curves.{_toml_key(channel_name)}]")
            if isinstance(cal, dict):
                for k, v in cal.items():
                    out.append(f"{k} = {_to_toml_value(v)}")
            out.append("")
    return "\n".join(out)


def _to_toml_value(value: Any) -> str:
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_to_toml_value(v) for v in value) + "]"
    return f'"{value}"'


def _toml_key(name: str) -> str:
    if all(c.isalnum() or c in "-_" for c in name) and name:
        return name
    return f'"{name}"'


__all__ = [
    "CalibrationDiffEntry",
    "DiffKind",
    "apply_diff_selection",
    "build_set_from_channels",
    "diff_set_against_channels",
    "load_calibration_set",
    "save_calibration_set",
]
