"""``ConfigDocument`` — source-tracking IO layer for experiment configs.

Distinct from :class:`~capa.experiment.config.ExperimentConfig`:

* :class:`ConfigDocument` knows where the draft came from (paths, formats,
  inline/external modes) and holds *raw* dict payloads suitable for editing.
* :class:`~capa.experiment.config.ExperimentConfig` is the validated, frozen
  runtime object built from those payloads at save / apply / validate
  boundaries.

The split keeps the IO concerns (atomic multi-file write, canonical
ordering, mode transitions) out of the runtime model, and lets the UI
edit mid-flight invalid state without fighting ``frozen=True``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ruamel.yaml import YAML

from capa.config.canonical import (
    write_toml_experiment,
    write_toml_hardware,
    write_yaml_canonical,
)
from capa.core.errors import CapaError, ConfigError

# ---------------------------------------------------------------------------
# Exceptions.
# ---------------------------------------------------------------------------


class SaveError(CapaError):
    """Raised when an atomic multi-file save fails.

    The exception carries the path that failed and any partial-write
    paths that were rolled back so callers can present an actionable
    message ("hardware file save failed; experiment file unchanged").
    """

    def __init__(
        self,
        message: str,
        *,
        failed_path: Path | None = None,
        rolled_back_paths: tuple[Path, ...] = (),
    ) -> None:
        super().__init__(message)
        self.failed_path = failed_path
        self.rolled_back_paths = rolled_back_paths


# ---------------------------------------------------------------------------
# SourceLayout — describes a "save as" target layout.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceLayout:
    """Target layout passed to :meth:`ConfigDocument.save_as`.

    Each field maps onto the same-named field on :class:`ConfigDocument`.
    Methods that mutate layout (e.g. extract-to-file) compose a new
    ``SourceLayout`` and call ``save_as``.
    """

    experiment_path: Path | None
    experiment_format: Literal["yaml", "toml"] | None
    hardware_path: Path | None
    hardware_format: Literal["toml"] | None
    hardware_mode: Literal["external", "inline"]
    method_path: Path | None
    method_format: Literal["toml"] | None
    method_mode: Literal["external", "inline", "none"]


# ---------------------------------------------------------------------------
# ConfigDocument.
# ---------------------------------------------------------------------------


_StructuredFormat = Literal["yaml", "toml"]


@dataclass
class ConfigDocument:
    """In-memory representation of a setup's on-disk layout.

    Payloads are raw ``dict[str, Any]`` so mid-edit invalid state can
    live here without fighting frozen Pydantic models. Promote
    to :class:`~capa.experiment.config.ExperimentConfig` via
    :meth:`build_config` at save / apply / validate boundaries.

    Convention: ``experiment_payload`` never contains ``hardware`` or
    ``method`` keys — those are tracked separately so save() can route
    them per-mode. :meth:`build_config` re-inlines them before
    validation.
    """

    experiment_path: Path | None = None
    hardware_path: Path | None = None
    method_path: Path | None = None
    experiment_format: _StructuredFormat | None = None
    hardware_format: Literal["toml"] | None = None
    method_format: Literal["toml"] | None = None
    hardware_mode: Literal["external", "inline"] = "external"
    method_mode: Literal["external", "inline", "none"] = "none"
    experiment_payload: dict[str, Any] = field(default_factory=dict)
    hardware_payload: dict[str, Any] = field(default_factory=dict)
    method_payload: dict[str, Any] | None = None

    # -- loading ------------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> ConfigDocument:
        """Open an experiment YAML/TOML; resolve hardware/method refs.

        Mirrors :meth:`ExperimentConfig.load`'s file-ref rules: when
        ``hardware:`` or ``method:`` is a string, treat it as a path
        relative to the experiment file's directory. The presence /
        absence of those refs determines ``hardware_mode`` /
        ``method_mode``.
        """
        source = Path(path).resolve()
        data = _load_structured_file(source)
        if not isinstance(data, dict):
            raise ConfigError(f"{source}: top-level must be a mapping")
        format_ = _detect_format(source)

        doc = cls(
            experiment_path=source,
            experiment_format=format_,
        )
        doc.experiment_payload = dict(data)

        # Hardware: required.
        hw_ref = doc.experiment_payload.pop("hardware", None)
        if isinstance(hw_ref, str):
            hw_path = _resolve(hw_ref, source.parent)
            doc.hardware_path = hw_path
            doc.hardware_format = _detect_hardware_format(hw_path)
            doc.hardware_mode = "external"
            doc.hardware_payload = dict(_load_structured_file(hw_path))
        elif isinstance(hw_ref, dict):
            doc.hardware_mode = "inline"
            doc.hardware_payload = dict(hw_ref)
        elif hw_ref is None:
            raise ConfigError(f"{source}: missing required field 'hardware'")
        else:
            raise ConfigError(
                f"{source}: 'hardware' must be a string ref or mapping, got {type(hw_ref).__name__}"
            )

        # Method: optional. Three modes (external / inline / none).
        method_ref = doc.experiment_payload.pop("method", None)
        if isinstance(method_ref, str):
            method_path = _resolve(method_ref, source.parent)
            doc.method_path = method_path
            doc.method_format = "toml"
            doc.method_mode = "external"
            doc.method_payload = dict(_load_structured_file(method_path))
        elif isinstance(method_ref, dict):
            doc.method_mode = "inline"
            doc.method_payload = dict(method_ref)
        else:
            doc.method_mode = "none"
            doc.method_payload = None

        # Strip the legacy source-path keys if they leaked into the file
        # (they're excluded from serialisation but defensive).
        doc.experiment_payload.pop("method_source_path", None)
        doc.experiment_payload.pop("hardware_source_path", None)

        return doc

    @classmethod
    def load_hardware_only(cls, path: str | Path) -> ConfigDocument:
        """Open a bare hardware TOML; produce a minimal experiment payload.

        Used by the Setup tab when the operator wants to author hardware
        in isolation. The experiment payload is left empty; the Setup
        tab fills in operator / sample / procedure stubs as the operator
        edits.
        """
        source = Path(path).resolve()
        data = _load_structured_file(source)
        if not isinstance(data, dict):
            raise ConfigError(f"{source}: top-level must be a mapping")
        return cls(
            hardware_path=source,
            hardware_format="toml",
            hardware_mode="external",
            hardware_payload=dict(data),
            method_mode="none",
        )

    # -- composition + validation ------------------------------------------

    def composed_payload(self) -> dict[str, Any]:
        """Re-inline hardware / method into a single experiment dict.

        The returned dict is what :class:`~capa.experiment.config.ExperimentConfig`
        :meth:`model_validate` expects: a single mapping with ``hardware``
        as a nested mapping and ``method`` either nested, absent, or
        nested-from-an-inline-method. Source-path bookkeeping is
        re-attached as the (excluded-from-serialisation) fields on the
        model so callers that still go through :meth:`load` see the same
        ``hardware_source_path`` / ``method_source_path`` they always did.
        """
        composed: dict[str, Any] = dict(self.experiment_payload)
        composed["hardware"] = dict(self.hardware_payload)
        if self.method_mode != "none" and self.method_payload is not None:
            composed["method"] = dict(self.method_payload)
        if self.method_path is not None:
            composed["method_source_path"] = self.method_path
        if self.hardware_path is not None:
            composed["hardware_source_path"] = self.hardware_path
        return composed

    def build_config(self) -> Any:
        """Validate payloads into an :class:`ExperimentConfig`.

        Local import to break the circular dep:
        ``capa.experiment.config`` imports nothing from ``capa.config``;
        this module imports from ``capa.experiment.config``.
        """
        from capa.experiment.config import ExperimentConfig  # noqa: PLC0415

        composed = self.composed_payload()
        try:
            return ExperimentConfig.model_validate(composed)
        except Exception as exc:
            src = self.experiment_path or self.hardware_path
            label = str(src) if src else "<unsaved>"
            raise ConfigError(f"{label}: {exc}") from exc

    # -- saving -------------------------------------------------------------

    def save(self) -> None:
        """Atomic multi-file save back to the loaded paths.

        For each target file: write ``<path>.tmp`` in the same directory,
        then ``os.replace()`` into place. On any failure, delete already-
        written ``.tmp`` files. Original files remain intact when any
        member of the save set fails.

        Pre-condition: every target path must already be set (use
        :meth:`save_as` for first-time writes).
        """
        plan = self._save_plan()
        self._execute_save_plan(plan)

    def save_as(self, layout: SourceLayout) -> None:
        """Save to a new layout; updates ``self`` in place on success.

        Layout transitions (inline ↔ external for hardware / method) are
        applied here; the document does not silently change them on
        :meth:`save`.
        """
        # Apply layout to a working copy of the document, then save.
        prev = (
            self.experiment_path,
            self.experiment_format,
            self.hardware_path,
            self.hardware_format,
            self.hardware_mode,
            self.method_path,
            self.method_format,
            self.method_mode,
        )
        try:
            self.experiment_path = layout.experiment_path
            self.experiment_format = layout.experiment_format
            self.hardware_path = layout.hardware_path
            self.hardware_format = layout.hardware_format
            self.hardware_mode = layout.hardware_mode
            self.method_path = layout.method_path
            self.method_format = layout.method_format
            self.method_mode = layout.method_mode
            plan = self._save_plan()
            self._execute_save_plan(plan)
        except Exception:
            (
                self.experiment_path,
                self.experiment_format,
                self.hardware_path,
                self.hardware_format,
                self.hardware_mode,
                self.method_path,
                self.method_format,
                self.method_mode,
            ) = prev
            raise

    def extract_hardware_inline_to_file(self, hardware_path: Path) -> None:
        """Move inline hardware to an external file (without writing yet).

        Only flips the mode and records the path; call :meth:`save` to
        commit.
        """
        if self.hardware_mode != "inline":
            raise ConfigError("extract_hardware_inline_to_file: hardware is already external")
        self.hardware_path = Path(hardware_path).resolve()
        self.hardware_format = "toml"
        self.hardware_mode = "external"

    def inline_hardware_from_file(self) -> None:
        """Inline a currently-external hardware file (without writing yet)."""
        if self.hardware_mode != "external":
            raise ConfigError("inline_hardware_from_file: hardware is already inline")
        self.hardware_mode = "inline"
        self.hardware_path = None
        self.hardware_format = None

    # -- internals ----------------------------------------------------------

    def _save_plan(self) -> list[_SavePlanItem]:
        """Compose the list of (path, bytes) writes needed for save().

        Computes the experiment-file representation per current mode:
        inline collapses hardware/method into the experiment dict;
        external writes them to separate files and references them by
        relative path.
        """
        if self.experiment_path is None and self.hardware_path is None:
            raise SaveError("save: no paths configured (call save_as first)")

        plan: list[_SavePlanItem] = []

        # Compose experiment dict for write.
        if self.experiment_path is not None:
            exp_out: dict[str, Any] = {}
            # Hardware first per canonical order.
            if self.hardware_mode == "external":
                if self.hardware_path is None:
                    raise SaveError("save: hardware_mode='external' but hardware_path is None")
                rel = _relative_to(self.hardware_path, self.experiment_path.parent)
                exp_out["hardware"] = rel.as_posix()
            else:
                exp_out["hardware"] = dict(self.hardware_payload)
            # Method.
            if self.method_mode == "external":
                if self.method_path is None:
                    raise SaveError("save: method_mode='external' but method_path is None")
                rel = _relative_to(self.method_path, self.experiment_path.parent)
                exp_out["method"] = rel.as_posix()
            elif self.method_mode == "inline":
                if self.method_payload is not None:
                    exp_out["method"] = dict(self.method_payload)
            # 'none' → omit the method key entirely.

            # Merge in the rest of the experiment payload (procedure, etc.).
            for key, value in self.experiment_payload.items():
                if key in ("hardware", "method"):
                    continue
                exp_out[key] = value

            fmt = self.experiment_format or _detect_format(self.experiment_path)
            plan.append(
                _SavePlanItem(
                    path=self.experiment_path,
                    payload=exp_out,
                    kind="experiment",
                    format=fmt,
                )
            )

        # Hardware file (when external).
        if self.hardware_mode == "external" and self.hardware_path is not None:
            plan.append(
                _SavePlanItem(
                    path=self.hardware_path,
                    payload=dict(self.hardware_payload),
                    kind="hardware",
                    format=self.hardware_format or "toml",
                )
            )

        # Method file (when external).
        if (
            self.method_mode == "external"
            and self.method_path is not None
            and self.method_payload is not None
        ):
            plan.append(
                _SavePlanItem(
                    path=self.method_path,
                    payload=dict(self.method_payload),
                    kind="method",
                    format=self.method_format or "toml",
                )
            )

        return plan

    def _execute_save_plan(self, plan: list[_SavePlanItem]) -> None:
        """Write each plan item atomically; roll back on any failure."""
        written_tmps: list[Path] = []
        try:
            for item in plan:
                tmp_path = item.path.with_suffix(item.path.suffix + ".tmp")
                item.path.parent.mkdir(parents=True, exist_ok=True)
                _write_one(item, tmp_path)
                written_tmps.append(tmp_path)
            # Replace each tmp into place. os.replace is atomic on
            # both POSIX and Windows.
            for tmp_path, item in zip(written_tmps, plan, strict=True):
                os.replace(tmp_path, item.path)
        except Exception as exc:
            rolled_back: list[Path] = []
            for tmp in written_tmps:
                if tmp.exists():
                    try:
                        tmp.unlink()
                        rolled_back.append(tmp)
                    except OSError:
                        pass
            failed_path = plan[len(rolled_back)].path if len(rolled_back) < len(plan) else None
            raise SaveError(
                f"save failed at {failed_path}: {exc}",
                failed_path=failed_path,
                rolled_back_paths=tuple(rolled_back),
            ) from exc


# ---------------------------------------------------------------------------
# Save-plan internals.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SavePlanItem:
    path: Path
    payload: dict[str, Any]
    kind: Literal["experiment", "hardware", "method"]
    format: Literal["yaml", "toml"]


def _write_one(item: _SavePlanItem, tmp_path: Path) -> None:
    if item.kind == "experiment":
        if item.format == "yaml":
            write_yaml_canonical(item.payload, tmp_path)
        else:
            write_toml_experiment(item.payload, tmp_path)
    elif item.kind == "hardware":
        write_toml_hardware(item.payload, tmp_path)
    elif item.kind == "method":
        # Method bodies are passed through as-is — the Method module
        # owns its own canonical ordering. Use plain tomli_w.
        import tomli_w  # noqa: PLC0415

        with open(tmp_path, "wb") as fp:
            tomli_w.dump(item.payload, fp)
    else:  # pragma: no cover - defensive
        raise SaveError(f"unknown plan item kind: {item.kind!r}")


# ---------------------------------------------------------------------------
# Module-level helpers (mirror the originals in capa.experiment.config so the
# two paths stay in lock-step).
# ---------------------------------------------------------------------------


def _detect_format(path: Path) -> _StructuredFormat:
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        return "yaml"
    if suffix == ".toml":
        return "toml"
    raise ConfigError(f"unsupported config suffix {suffix!r}: {path}")


def _detect_hardware_format(path: Path) -> Literal["toml"]:
    suffix = path.suffix.lower()
    if suffix == ".toml":
        return "toml"
    raise ConfigError(f"hardware files must be TOML (got {suffix!r}): {path}")


def _resolve(ref: str, base_dir: Path) -> Path:
    ref_path = Path(ref)
    if not ref_path.is_absolute():
        ref_path = base_dir / ref_path
    return ref_path.resolve()


def _relative_to(target: Path, base: Path) -> Path:
    """Return ``target`` expressed relative to ``base`` when possible.

    Falls back to the absolute path when no walk-up route exists (cross-
    drive paths on Windows, etc.). The string form is what gets written
    into the experiment file's ``hardware:`` / ``method:`` field.
    """
    target = target.resolve()
    base = base.resolve()
    try:
        return Path(os.path.relpath(target, base))
    except ValueError:
        return target


def _load_structured_file(path: Path) -> Any:
    """Load YAML or TOML based on suffix.

    Mirrors :func:`capa.experiment.config._load_structured_file`; kept
    here so :class:`ConfigDocument` doesn't reach into the experiment
    package's private helpers.
    """
    if not path.is_file():
        raise ConfigError(f"file not found: {path}")
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        yaml = YAML(typ="safe")
        with open(path, encoding="utf-8") as fp:
            return yaml.load(fp)
    if suffix == ".toml":
        with open(path, "rb") as fp:
            return tomllib.load(fp)
    raise ConfigError(f"unsupported config suffix {suffix!r}: {path}")


__all__ = ["ConfigDocument", "SaveError", "SourceLayout"]
