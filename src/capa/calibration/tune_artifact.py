""":class:`HeatFluxTuneArtifact` — operational result of the heat-flux tune procedure.

A tune session converges the heater setpoint that delivers each target
radiant heat flux at the specimen surface. The accepted ``(target,
setpoint, measured)`` triples are persisted as a separate artifact —
*not* as entries in a :class:`~capa.channels.calibration.CalibrationSet`,
since a heater-setpoint to delivered-flux mapping is not a channel
calibration (no :class:`~capa.channels.spec.ChannelSpec` has commanded
°C as its raw unit).

Storage layout under ``configs/calibrations/flux/``::

    capa_flux_2026-05-17.toml    # one artifact per save
    capa_flux_2026-05-18.toml
    latest.toml                   # one-line pointer to the latest id

:func:`save_artifact` never silently overwrites. If ``latest.toml``
would otherwise be redirected to a new file, its previous target is
copied to ``<previous-id>.toml.bak-<YYYY-MM-DD>`` first.
"""

from __future__ import annotations

import shutil
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field

from capa.core.errors import CapaError


class TuneArtifactError(CapaError):
    """Raised when a tune artifact cannot be read, written, or parsed.

    Distinct from :class:`~capa.core.errors.CalibrationError` (which the
    channel-calibration family raises) so a tune-artifact problem is
    recognisable as an operational-tune issue rather than a per-channel
    calibration issue.
    """


AcceptReason = Literal["algorithm_converged", "operator_override", "warn_proceeded"]
"""Why a tune point was accepted.

* ``algorithm_converged``: the §7.5 convergence rule (two consecutive
  in-tolerance windows plus a verification soak) was satisfied.
* ``operator_override``: the operator pressed "Accept Current" — the
  point may not satisfy the convergence rule.
* ``warn_proceeded``: ``t_settle_max_s`` elapsed without the steady-state
  predicate firing; the procedure was configured to warn and proceed
  with the noisier reading rather than abort.
"""


class HeatFluxTunePoint(BaseModel):
    """One accepted (target flux, heater setpoint) point in a tune session.

    All measured fields are reported in the channel's calibrated units
    (kW/m² for flux, °C for the heater process variable). ``soak_s`` is
    the total dwell from the iteration's setpoint command to acceptance.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_flux_kw_m2: float = Field(gt=0)
    heater_setpoint_c: float
    measured_flux_mean_kw_m2: float
    measured_flux_std_kw_m2: float = Field(ge=0)
    measured_flux_slope_kw_m2_per_min: float
    heater_pv_mean_c: float
    soak_s: float = Field(ge=0)
    accepted: bool
    accept_reason: AcceptReason


class HeatFluxTuneArtifact(BaseModel):
    """Result of one tune session.

    The artifact is rig-specific (``rig``, ``heater_device``,
    ``heater_setpoint_channel``, ``heater_pv_channel``, ``flux_channel``)
    so a downstream consumer can refuse to apply it on a different rig.
    ``gauge_calibration_ref`` links to the wire-side V→kW/m² calibration
    that was active during the tune; if that changes, the artifact's
    setpoints no longer correspond to the same delivered flux.

    ``points`` is ordered by acceptance, which is the same as the order
    of the operator-supplied ``targets_kw_m2`` (ascending in the typical
    daily-tune workflow). A partial session that aborted on target #N of
    M still produces an artifact with N points — :func:`save_artifact`
    is called after each accepted point.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    rig: str = Field(min_length=1)
    heater_device: str = Field(min_length=1)
    heater_setpoint_channel: str = Field(min_length=1)
    heater_pv_channel: str = Field(min_length=1)
    flux_channel: str = Field(min_length=1)
    gauge_calibration_ref: str | None = None
    geometry: str = Field(min_length=1)
    accepted_at: datetime
    operator_id: str | None = None
    procedure_id: str = Field(min_length=1)
    procedure_version: str = Field(min_length=1)
    capa_git_sha: str | None = None
    points: tuple[HeatFluxTunePoint, ...] = ()

    def setpoint_for_target(self, target_kw_m2: float) -> float | None:
        """Linearly interpolate ``target_kw_m2`` against accepted points.

        Returns ``None`` when the artifact has no points or when the
        target falls outside the bracket of accepted targets and would
        require extrapolation. Extrapolation is refused because a
        flux↔setpoint curve outside the brackets is not characterised
        by this session.

        Used by the experiment-method UI to pre-populate
        ``HeaterProgram.heater_setpoint_c`` from
        ``HeaterProgram.target_heat_flux_kw_m2``.
        """
        accepted = [p for p in self.points if p.accepted]
        if not accepted:
            return None
        ordered = sorted(accepted, key=lambda p: p.target_flux_kw_m2)
        lows = [p.target_flux_kw_m2 for p in ordered]
        sps = [p.heater_setpoint_c for p in ordered]
        if target_kw_m2 < lows[0] or target_kw_m2 > lows[-1]:
            return None
        if len(ordered) == 1:
            return sps[0] if target_kw_m2 == lows[0] else None
        for i in range(1, len(ordered)):
            if lows[i - 1] <= target_kw_m2 <= lows[i]:
                if lows[i] == lows[i - 1]:
                    return sps[i - 1]
                frac = (target_kw_m2 - lows[i - 1]) / (lows[i] - lows[i - 1])
                return sps[i - 1] + frac * (sps[i] - sps[i - 1])
        return None

    def local_df_dt(self, target_kw_m2: float) -> float | None:
        """Local d(flux)/d(setpoint) around ``target_kw_m2``, kW/m² per °C.

        Returned as the secant slope of the bracketing accepted points.
        ``None`` when fewer than two points are accepted, or when the
        target sits outside any bracket. Used as the step-size prior for
        iteration 1 of a future tune session (§7.1).
        """
        accepted = sorted(
            [p for p in self.points if p.accepted],
            key=lambda p: p.target_flux_kw_m2,
        )
        if len(accepted) < 2:
            return None
        targets = [p.target_flux_kw_m2 for p in accepted]
        sps = [p.heater_setpoint_c for p in accepted]
        if target_kw_m2 < targets[0]:
            lo, hi = 0, 1
        elif target_kw_m2 > targets[-1]:
            lo, hi = len(accepted) - 2, len(accepted) - 1
        else:
            hi = next(i for i, t in enumerate(targets) if t >= target_kw_m2)
            lo = max(0, hi - 1)
            if lo == hi:
                hi = min(len(accepted) - 1, lo + 1)
        if sps[hi] == sps[lo]:
            return None
        return (targets[hi] - targets[lo]) / (sps[hi] - sps[lo])


# ---------------------------------------------------------------------------
# TOML I/O
# ---------------------------------------------------------------------------


def to_toml(artifact: HeatFluxTuneArtifact) -> str:
    """Serialize an artifact to TOML text.

    ``datetime`` is preserved as a native TOML datetime via ``tomli_w``,
    so a round-trip through :func:`from_toml` reconstructs an equal
    object. ``None``-valued optional fields are emitted as missing keys
    rather than as empty strings.
    """
    payload: dict[str, Any] = {
        "id": artifact.id,
        "rig": artifact.rig,
        "heater_device": artifact.heater_device,
        "heater_setpoint_channel": artifact.heater_setpoint_channel,
        "heater_pv_channel": artifact.heater_pv_channel,
        "flux_channel": artifact.flux_channel,
        "geometry": artifact.geometry,
        "accepted_at": artifact.accepted_at,
        "procedure_id": artifact.procedure_id,
        "procedure_version": artifact.procedure_version,
    }
    if artifact.gauge_calibration_ref is not None:
        payload["gauge_calibration_ref"] = artifact.gauge_calibration_ref
    if artifact.operator_id is not None:
        payload["operator_id"] = artifact.operator_id
    if artifact.capa_git_sha is not None:
        payload["capa_git_sha"] = artifact.capa_git_sha
    payload["points"] = [
        {
            "target_flux_kw_m2": p.target_flux_kw_m2,
            "heater_setpoint_c": p.heater_setpoint_c,
            "measured_flux_mean_kw_m2": p.measured_flux_mean_kw_m2,
            "measured_flux_std_kw_m2": p.measured_flux_std_kw_m2,
            "measured_flux_slope_kw_m2_per_min": p.measured_flux_slope_kw_m2_per_min,
            "heater_pv_mean_c": p.heater_pv_mean_c,
            "soak_s": p.soak_s,
            "accepted": p.accepted,
            "accept_reason": p.accept_reason,
        }
        for p in artifact.points
    ]
    return tomli_w.dumps(payload)


def from_toml(text: str) -> HeatFluxTuneArtifact:
    """Parse TOML text into a :class:`HeatFluxTuneArtifact`.

    Raises :class:`TuneArtifactError` on syntactic or schema errors.
    The Pydantic validator enforces required fields and the
    ``accept_reason`` literal.
    """
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TuneArtifactError(f"tune artifact TOML is malformed: {exc}") from exc
    try:
        return HeatFluxTuneArtifact.model_validate(raw)
    except Exception as exc:
        raise TuneArtifactError(f"tune artifact failed validation: {exc}") from exc


def load_artifact(path: Path) -> HeatFluxTuneArtifact:
    """Read and validate a single tune artifact file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TuneArtifactError(f"cannot read tune artifact {path}: {exc}") from exc
    return from_toml(text)


def load_latest(directory: Path) -> HeatFluxTuneArtifact | None:
    """Return the artifact pointed to by ``<directory>/latest.toml``.

    Returns ``None`` when the directory does not exist, when
    ``latest.toml`` is missing, or when its ``id`` pointer references a
    file that no longer exists. Any *parse* failure on the pointed-at
    file raises :class:`TuneArtifactError` — a corrupt artifact is
    different from an absent one.
    """
    pointer_path = directory / "latest.toml"
    if not pointer_path.is_file():
        return None
    try:
        pointer = tomllib.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise TuneArtifactError(f"latest pointer at {pointer_path} unreadable: {exc}") from exc
    target_id = pointer.get("id")
    if not isinstance(target_id, str) or not target_id:
        raise TuneArtifactError(f"latest pointer at {pointer_path} is missing a string 'id'")
    target_path = directory / f"{target_id}.toml"
    if not target_path.is_file():
        return None
    return load_artifact(target_path)


def save_artifact(artifact: HeatFluxTuneArtifact, directory: Path) -> Path:
    """Persist ``artifact`` under ``directory`` and update ``latest.toml``.

    Writes ``<directory>/<artifact.id>.toml``. Refuses to overwrite an
    existing file with that id — a clash means the caller picked a
    non-unique id and the operator deserves to see it rather than have
    yesterday's tune silently clobbered.

    Updates ``latest.toml`` to point at the new id. If ``latest.toml``
    previously pointed at a different artifact, its target file is
    copied to ``<old-id>.toml.bak-<YYYY-MM-DD>`` first; that file is
    *not* removed (the on-disk record stays available under its own
    name, the ``.bak-…`` copy is the audit ledger of "this was the
    latest on this date").
    """
    directory.mkdir(parents=True, exist_ok=True)

    target_path = directory / f"{artifact.id}.toml"
    if target_path.exists():
        raise TuneArtifactError(
            f"tune artifact already exists at {target_path}; refusing to overwrite "
            f"(pick a unique id or remove the existing file deliberately)"
        )

    pointer_path = directory / "latest.toml"
    previous_id: str | None = None
    if pointer_path.is_file():
        try:
            previous = tomllib.loads(pointer_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise TuneArtifactError(
                f"existing latest pointer at {pointer_path} is unreadable; "
                f"resolve manually before saving a new tune: {exc}"
            ) from exc
        prev = previous.get("id")
        if isinstance(prev, str) and prev and prev != artifact.id:
            previous_id = prev

    if previous_id is not None:
        previous_target = directory / f"{previous_id}.toml"
        if previous_target.is_file():
            backup_name = f"{previous_id}.toml.bak-{date.today().isoformat()}"
            backup_path = directory / backup_name
            if not backup_path.exists():
                shutil.copy2(previous_target, backup_path)

    target_path.write_text(to_toml(artifact), encoding="utf-8")
    pointer_path.write_text(
        tomli_w.dumps({"id": artifact.id, "updated_at": datetime.now(UTC)}),
        encoding="utf-8",
    )
    return target_path


__all__ = [
    "AcceptReason",
    "HeatFluxTuneArtifact",
    "HeatFluxTunePoint",
    "TuneArtifactError",
    "from_toml",
    "load_artifact",
    "load_latest",
    "save_artifact",
    "to_toml",
]
