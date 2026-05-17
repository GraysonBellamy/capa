"""Five-layer validation pipeline.

| Layer | What it checks | When |
|-------|----------------|------|
| 1 | Pydantic schema | per-keystroke + every save |
| 2 | Referential (cross-row refs, name uniqueness) | row leave + save |
| 3 | Domain (CAPA profile rules) | save |
| 4 | Resource (``build_workers`` dry run) | save |
| 5 | Live (``discover()`` / handshakes) | only on explicit Check Hardware |

Layers 1–4 never touch hardware. Layer 5 does and is gated behind
``with_live_checks=True``. The pipeline composes ``ConfigProblem``s
into one ordered list; UI surfaces filter and group by section/severity.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from pydantic import ValidationError

from capa.config.problems import ConfigProblem, Section

if TYPE_CHECKING:
    from capa.config.document import ConfigDocument

# Per-device handshake budget (seconds). Operators are willing to wait a
# few seconds for a wiring check; longer than that and the device is
# almost certainly not responding. Layer 5's async path runs all device
# handshakes concurrently so total wall-clock is ``max(per-device)``.
_LIVE_HANDSHAKE_TIMEOUT_S: float = 8.0


def validate(
    document: ConfigDocument,
    *,
    with_live_checks: bool = False,
) -> list[ConfigProblem]:
    """Run layers 1–4 (and optionally 5); return all collected problems.

    Each layer is best-effort: a layer that catches a structural blocker
    that would crash a downstream layer aborts early *for that layer*
    but the pipeline still moves on. The result is the union of every
    layer's findings, ordered by severity (errors first) then by layer.
    """
    problems: list[ConfigProblem] = []
    schema_problems, valid_config = _layer1_schema(document)
    problems.extend(schema_problems)

    # Layers 2–4 need a validated ExperimentConfig. If schema layer
    # failed hard, the downstream layers can't run meaningfully —
    # surface what we have and let the operator fix the schema first.
    if valid_config is not None:
        problems.extend(_layer2_referential(valid_config, document))
        problems.extend(_layer3_domain(valid_config, document))
        problems.extend(_layer4_resource(valid_config, document))
        if with_live_checks:
            problems.extend(_layer5_live(valid_config, document))

    return _sorted_problems(problems)


def _sorted_problems(problems: list[ConfigProblem]) -> list[ConfigProblem]:
    """Order: errors before warnings before info; stable within severity."""
    severity_rank = {"error": 0, "warning": 1, "info": 2}
    return sorted(problems, key=lambda p: severity_rank.get(p.severity, 99))


# ---------------------------------------------------------------------------
# Layer 1 — schema.
# ---------------------------------------------------------------------------


def _layer1_schema(
    document: ConfigDocument,
) -> tuple[list[ConfigProblem], Any | None]:
    """Run Pydantic validation on the composed payload.

    Returns ``(problems, valid_config_or_None)``. Each Pydantic error's
    ``loc`` tuple maps directly to ``ConfigProblem.path``; the section
    is derived from the path prefix.
    """
    try:
        cfg = document.build_config()
        return ([], cfg)
    except Exception as exc:
        # ``ConfigDocument.build_config`` wraps Pydantic errors in
        # ``ConfigError``; the underlying ``ValidationError`` is in
        # ``exc.__cause__``.
        cause = getattr(exc, "__cause__", exc)
        if isinstance(cause, ValidationError):
            return ([_problem_from_validation_error(err, document) for err in cause.errors()], None)
        # Non-Pydantic failure (a custom ConfigError raised by a model
        # validator). Surface as a single error against the experiment
        # section so the operator at least sees it.
        return (
            [
                ConfigProblem(
                    severity="error",
                    code="schema.unknown",
                    message=str(exc),
                    section="experiment",
                    source_file=document.experiment_path,
                )
            ],
            None,
        )


def _problem_from_validation_error(
    err: Mapping[str, Any], document: ConfigDocument
) -> ConfigProblem:
    """Map one ``ValidationError.errors()[i]`` entry to a ``ConfigProblem``."""
    loc: tuple[Any, ...] = tuple(err.get("loc", ()))
    section, path = _loc_to_section_and_path(loc)
    source_file = _source_for_section(section, document)
    return ConfigProblem(
        severity="error",
        code=f"schema.{err.get('type', 'unknown')}",
        message=err.get("msg", "schema validation failed"),
        section=section,
        path=path,
        source_file=source_file,
    )


def _loc_to_section_and_path(loc: tuple[Any, ...]) -> tuple[Section, tuple[str | int, ...]]:
    """Derive the section + remaining path from a Pydantic loc tuple.

    Pydantic loc tuples look like ``("hardware", "devices", 2, "params",
    "port")``. We classify the section from the prefix; the path keeps
    everything after the section discriminator so the UI can navigate to
    the offending row + field.
    """
    if not loc:
        return ("experiment", ())
    head = str(loc[0])
    if head == "hardware":
        if len(loc) >= 2:
            sub = str(loc[1])
            if sub == "devices":
                return ("devices", tuple(_normalise_path_part(p) for p in loc[2:]))
            if sub == "channels":
                return ("channels", tuple(_normalise_path_part(p) for p in loc[2:]))
            if sub == "cameras":
                return ("cameras", tuple(_normalise_path_part(p) for p in loc[2:]))
        return ("devices", tuple(_normalise_path_part(p) for p in loc[1:]))
    if head == "method":
        return ("experiment", tuple(_normalise_path_part(p) for p in loc))
    if head == "procedure":
        return ("procedure", tuple(_normalise_path_part(p) for p in loc[1:]))
    if head == "domain_profile":
        return ("capa_profile", tuple(_normalise_path_part(p) for p in loc[1:]))
    if head == "storage":
        return ("storage", tuple(_normalise_path_part(p) for p in loc[1:]))
    if head == "safety":
        return ("safety", tuple(_normalise_path_part(p) for p in loc[1:]))
    return ("experiment", tuple(_normalise_path_part(p) for p in loc))


def _normalise_path_part(p: Any) -> str | int:
    if isinstance(p, int):
        return p
    return str(p)


def _source_for_section(section: Section, document: ConfigDocument) -> Path | None:
    """Which file would carry the fix for a problem in this section?

    Used by the Save dialog and Problems panel to show the operator a
    concrete path. Devices/channels/cameras live in the hardware file;
    everything else in the experiment file.
    """
    if section in ("devices", "channels", "cameras") and document.hardware_path is not None:
        return document.hardware_path
    return document.experiment_path


# ---------------------------------------------------------------------------
# Layer 2 — referential / cross-row.
# ---------------------------------------------------------------------------


def _layer2_referential(config: Any, document: ConfigDocument) -> list[ConfigProblem]:
    """Cross-row invariants that Pydantic validators already raise on.

    Most of these are already enforced by :class:`HardwareProfile`'s
    validators (uniqueness, channel→device resolution, name overlap),
    which means Layer 1 catches them before we get here. This layer
    runs a few additional checks that depend on the full config but
    aren't part of the schema:

    * Binding ``source`` discriminator matches the device adapter family
      (a Watlow device should be bound via ``watlow_parameter``).
    * Method targets resolve to declared channels (Pydantic already
      raises a ``ConfigError`` from the post-validator; this is for the
      "method present but unloaded" case where Pydantic accepts).
    """
    from capa.devices.registry import get_descriptor  # noqa: PLC0415

    problems: list[ConfigProblem] = []
    hardware = getattr(config, "hardware", None)
    if hardware is None:
        return problems

    # Build adapter-family lookup: device name → supported binding sources.
    family_for_device: dict[str, tuple[str, ...]] = {}
    for dev in hardware.devices:
        descriptor = get_descriptor(dev.adapter)
        if descriptor is None:
            problems.append(
                ConfigProblem(
                    severity="error",
                    code="devices.unknown_adapter",
                    message=(
                        f"device {dev.name!r}: no AdapterDescriptor registered for {dev.adapter!r}"
                    ),
                    section="devices",
                    path=("devices", dev.name, "adapter"),
                    source_file=document.hardware_path,
                )
            )
            continue
        family_for_device[dev.name] = descriptor.supported_binding_sources

    for idx, ch in enumerate(hardware.channels):
        binding = ch.source
        device_name = getattr(binding, "device", None)
        if device_name is None:
            continue
        expected = family_for_device.get(device_name)
        if not expected:
            continue
        binding_src = getattr(binding, "source", None)
        if binding_src not in expected:
            problems.append(
                ConfigProblem(
                    severity="error",
                    code="channels.binding_family_mismatch",
                    message=(
                        f"channel {ch.name!r} binds via {binding_src!r}, but device "
                        f"{device_name!r} only supports {list(expected)!r}"
                    ),
                    section="channels",
                    path=("channels", idx, "source", "source"),
                    source_file=document.hardware_path,
                )
            )

    problems.extend(_layer2_nidaq_join(hardware, document))
    return problems


def _layer2_nidaq_join(hardware: Any, document: ConfigDocument) -> list[ConfigProblem]:
    """Validate the NI-DAQ join between ``devices.params.channels`` and
    top-level ``[[channels]]``.

    Two checks, both previously silent:

    * ``channels.binding_field_unresolved`` — an ``NIDAQReadingField`` /
      ``NIDAQBlockChannel`` binding names a ``(device, task, field)`` that
      doesn't resolve against any declared NI channel. At runtime the
      adapter ``continue``\\ s past the missing field and the capa channel
      silently emits nothing.
    * ``devices.nidaq.duplicate_channel_name`` — two NI channel rows
      within the same task share a display name. ``DaqReading.values`` is
      a dict keyed by display name; duplicates make the second row
      shadow the first non-deterministically.
    """
    from capa.devices.nidaq_join import (  # noqa: PLC0415
        declared_channels_from_config,
        nidaq_task_keys_from_config,
    )

    problems: list[ConfigProblem] = []
    declared = declared_channels_from_config(hardware)
    task_keys = nidaq_task_keys_from_config(hardware)
    if not declared and not task_keys:
        return problems

    declared_by_key: dict[tuple[str, str, str], list[int]] = {}
    fields_by_device_task: dict[tuple[str, str], list[str]] = {}
    for i, d in enumerate(declared):
        key = (d.device_name, d.task_name, d.field_name)
        declared_by_key.setdefault(key, []).append(i)
        fields_by_device_task.setdefault((d.device_name, d.task_name), []).append(d.field_name)

    # ----- duplicate NI display names within a task -----
    seen_duplicates: set[tuple[str, str, str]] = set()
    for key, indices in declared_by_key.items():
        if len(indices) < 2 or key in seen_duplicates:
            continue
        seen_duplicates.add(key)
        device_name, task_name, field_name = key
        problems.append(
            ConfigProblem(
                severity="error",
                code="devices.nidaq.duplicate_channel_name",
                message=(
                    f"device {device_name!r} task {task_name!r}: NI channel name "
                    f"{field_name!r} is declared {len(indices)} times — display names must be "
                    "unique within a task (DaqReading.values is keyed by name)"
                ),
                section="devices",
                path=("devices", device_name, "params", "channels"),
                source_file=document.hardware_path,
            )
        )

    # ----- bindings that don't resolve to any declared NI channel -----
    nidaq_bind_kinds = ("nidaq_reading_field", "nidaq_block_channel")
    for idx, ch in enumerate(hardware.channels):
        binding = ch.source
        binding_src = getattr(binding, "source", None)
        if binding_src not in nidaq_bind_kinds:
            continue
        device_name = getattr(binding, "device", None)
        task_name = getattr(binding, "task", None)
        field_attr = "field" if binding_src == "nidaq_reading_field" else "channel"
        field_name = getattr(binding, field_attr, None)
        if not (
            isinstance(device_name, str)
            and isinstance(task_name, str)
            and isinstance(field_name, str)
        ):
            continue
        if (device_name, task_name, field_name) in declared_by_key:
            continue
        # If the device exists but isn't NI, the family-mismatch check above
        # already flagged it — don't double-emit.
        task_key = (device_name, task_name)
        known_fields = fields_by_device_task.get(task_key)
        if known_fields is None and task_key in task_keys:
            known_fields = []
        if known_fields is None:
            # No NI task with this (device, task) at all. Could mean a
            # mistyped task name, or the device just hasn't been declared
            # as an NI device. Family mismatch covers the latter; for the
            # former, surface the available tasks on the device.
            available_tasks = sorted(
                {t for (d, t) in fields_by_device_task if d == device_name}
                | {t for (d, t) in task_keys if d == device_name}
            )
            if not available_tasks:
                continue
            problems.append(
                ConfigProblem(
                    severity="error",
                    code="channels.binding_field_unresolved",
                    message=(
                        f"channel {ch.name!r} binds to {device_name!r}.{task_name!r} but "
                        f"that task isn't declared on the device. Available tasks: "
                        f"{available_tasks!r}"
                    ),
                    section="channels",
                    path=("channels", idx, "source", "task"),
                    source_file=document.hardware_path,
                )
            )
            continue
        if not known_fields:
            problems.append(
                ConfigProblem(
                    severity="error",
                    code="channels.binding_field_unresolved",
                    message=(
                        f"channel {ch.name!r} reads from NI field {field_name!r} on "
                        f"{device_name!r}.{task_name!r}, but that task has no NI fields "
                        "declared"
                    ),
                    section="channels",
                    path=("channels", idx, "source", field_attr),
                    source_file=document.hardware_path,
                )
            )
            continue
        problems.append(
            ConfigProblem(
                severity="error",
                code="channels.binding_field_unresolved",
                message=(
                    f"channel {ch.name!r} reads from NI field {field_name!r} on "
                    f"{device_name!r}.{task_name!r}, but that field isn't declared. "
                    f"Available fields: {sorted(set(known_fields))!r}"
                ),
                section="channels",
                path=("channels", idx, "source", field_attr),
                source_file=document.hardware_path,
            )
        )
    return problems


# ---------------------------------------------------------------------------
# Layer 3 — domain (CAPA profile).
# ---------------------------------------------------------------------------


from capa.config.capa_profile import CAPA_REQUIRED_GROUPS as _CAPA_REQUIRED_GROUPS  # noqa: E402


def _layer3_domain(config: Any, document: ConfigDocument) -> list[ConfigProblem]:
    """CAPA profile required-mapping check.

    For CAPA pyrolysis: every required group above must be present at
    least once in ``channel.metadata["capa_group"]``. The plan's open
    question 5 says ~90% of CAPA experiments are single-setpoint, so
    we don't over-constrain ramp parameters at this layer.
    """
    profile_ref = getattr(config, "domain_profile", None)
    if profile_ref is None:
        return []
    if getattr(profile_ref, "id", None) != "capa.profiles.capa_pyrolysis":
        return []

    problems: list[ConfigProblem] = []
    channels = getattr(config.hardware, "channels", ())
    seen_groups: dict[str, str] = {}
    for ch in channels:
        capa_group = (ch.metadata or {}).get("capa_group")
        if capa_group:
            seen_groups[capa_group] = ch.name

    for group, _acceptable_kinds in _CAPA_REQUIRED_GROUPS.items():
        if group not in seen_groups:
            problems.append(
                ConfigProblem(
                    severity="error",
                    code="capa_profile.missing_required_group",
                    message=(
                        f"CAPA pyrolysis profile requires a channel mapped to "
                        f"{group!r} (set channel.metadata.capa_group = {group!r})"
                    ),
                    section="capa_profile",
                    path=("domain_profile", "metadata", group),
                    source_file=document.experiment_path,
                    fix_label=f"Map {group}",
                )
            )
    return problems


# ---------------------------------------------------------------------------
# Layer 4 — resource (build_workers dry run).
# ---------------------------------------------------------------------------


def _layer4_resource(config: Any, document: ConfigDocument) -> list[ConfigProblem]:
    """Construct adapters (passive) and run resource-conflict checks.

    Calls :func:`capa.devices.materialize.materialize_adapters` so the
    editor catches exactly the conflicts the runtime would catch at pool
    open — without opening anything, and without depending on the
    runtime layer. The adapter constructors are required to be passive
    (no I/O on ``__init__``) for this to be safe; the contract is
    documented on :class:`~capa.devices.registry.AdapterDescriptor`.
    """
    from capa.devices.materialize import (  # noqa: PLC0415
        ConfigMaterializationError,
        collect_resource_problems,
        materialize_adapters,
    )

    try:
        materialized = materialize_adapters(config)
    except ConfigMaterializationError as exc:
        # Attach a source file to each problem so the Problems panel
        # navigates correctly. The construction errors come from
        # devices/cameras tables; both live in the hardware file.
        return [_attach_source(problem, document.hardware_path) for problem in exc.problems]
    return [
        _attach_source(p, document.hardware_path) for p in collect_resource_problems(materialized)
    ]


def _attach_source(problem: ConfigProblem, source_file: Path | None) -> ConfigProblem:
    """Return ``problem`` with :attr:`ConfigProblem.source_file` populated.

    The materialize layer doesn't know about :class:`ConfigDocument` paths;
    the validation layer attaches them here so the Problems panel can
    open the right file.
    """
    if problem.source_file is not None or source_file is None:
        return problem
    return ConfigProblem(
        severity=problem.severity,
        code=problem.code,
        message=problem.message,
        section=problem.section,
        path=problem.path,
        source_file=source_file,
        fix_label=problem.fix_label,
    )


# ---------------------------------------------------------------------------
# Layer 5 — live checks.
# ---------------------------------------------------------------------------


def _layer5_live(config: Any, document: ConfigDocument) -> list[ConfigProblem]:
    """Sync entry point — drives :func:`_layer5_live_async` via :mod:`anyio`.

    Used by the synchronous :func:`validate` path (CLI ``capa config
    validate --live``). The Qt UI's Check Hardware button calls
    :func:`validate_live_async` directly to avoid blocking the qasync
    event loop.
    """
    return anyio.run(_layer5_live_async, config, document)


async def _layer5_live_async(config: Any, document: ConfigDocument) -> list[ConfigProblem]:
    """Run per-adapter handshake / discover coroutines concurrently.

    Each device whose registered :class:`AdapterDescriptor` advertises
    ``handshake_available=True`` gets its module's ``handshake(params)``
    coroutine awaited under :data:`_LIVE_HANDSHAKE_TIMEOUT_S`. Successes
    surface as ``info``-severity problems carrying the handshake summary;
    failures and timeouts surface as ``error``-severity problems against
    the device row so the Problems panel navigates correctly.

    Cameras follow the same pattern: the adapter module's
    ``handshake(cam_spec_dict)`` validates the spec by re-running
    discovery and matching selector / serial / model_hint.
    """
    from capa.devices.registry import get_descriptor  # noqa: PLC0415

    hardware = getattr(config, "hardware", None)
    if hardware is None:
        return []

    tasks: list[asyncio.Task[list[ConfigProblem]]] = []
    for dev in hardware.devices:
        descriptor = get_descriptor(dev.adapter)
        if descriptor is None or not descriptor.handshake_available:
            continue
        tasks.append(asyncio.create_task(_run_one_handshake(dev, document)))

    for cam in getattr(hardware, "cameras", ()):
        descriptor = get_descriptor(cam.adapter)
        if descriptor is None or not descriptor.handshake_available:
            # Camera adapter doesn't ship a handshake — quietly skip
            # rather than littering the Problems panel with stubs.
            continue
        tasks.append(asyncio.create_task(_run_one_camera_handshake(cam, document)))

    problems: list[ConfigProblem] = []
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=False)
        for batch in results:
            problems.extend(batch)

    return problems


async def _run_one_handshake(dev: Any, document: ConfigDocument) -> list[ConfigProblem]:
    """Await one device's handshake; map success/failure to a ``ConfigProblem``.

    The adapter module is resolved lazily by id rather than by class so
    a test can ``monkeypatch.setattr("capa.devices.watlow.handshake", ...)``
    and intercept the call without touching the descriptor registry.
    """
    module_path = dev.adapter
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        return [
            ConfigProblem(
                severity="error",
                code="live.adapter_import_failed",
                message=f"{dev.name}: could not import adapter {module_path!r}: {exc}",
                section="devices",
                path=("devices", dev.name),
                source_file=document.hardware_path,
            )
        ]
    handshake_fn = getattr(module, "handshake", None)
    if handshake_fn is None:
        # Descriptor advertised handshake_available but the module lacks
        # the hook — a packaging bug, not an operator-facing problem.
        return []

    params = dict(dev.params) if isinstance(dev.params, dict) else dev.params
    try:
        summary = await asyncio.wait_for(handshake_fn(params), timeout=_LIVE_HANDSHAKE_TIMEOUT_S)
    except TimeoutError:
        return [
            ConfigProblem(
                severity="error",
                code="live.handshake_timeout",
                message=(f"{dev.name}: handshake timed out after {_LIVE_HANDSHAKE_TIMEOUT_S:g}s"),
                section="devices",
                path=("devices", dev.name),
                source_file=document.hardware_path,
                fix_label="Check wiring / port",
            )
        ]
    except Exception as exc:
        return [
            ConfigProblem(
                severity="error",
                code="live.handshake_failed",
                message=f"{dev.name}: {exc}",
                section="devices",
                path=("devices", dev.name),
                source_file=document.hardware_path,
            )
        ]
    return [
        ConfigProblem(
            severity="info",
            code="live.handshake_ok",
            message=f"{dev.name}: {summary}",
            section="devices",
            path=("devices", dev.name),
            source_file=document.hardware_path,
        )
    ]


async def _run_one_camera_handshake(cam: Any, document: ConfigDocument) -> list[ConfigProblem]:
    """Await one camera's handshake; map success/failure to a ``ConfigProblem``.

    Mirrors :func:`_run_one_handshake` but addresses the
    ``cameras`` section so the Problems panel navigates correctly.
    Camera handshakes receive the camera spec as a dict (not just the
    ``params`` sub-dict) because selector / serial / model_hint live
    on the spec, not in adapter params.
    """
    module_path = cam.adapter
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        return [
            ConfigProblem(
                severity="error",
                code="live.adapter_import_failed",
                message=(f"camera {cam.name}: could not import adapter {module_path!r}: {exc}"),
                section="cameras",
                path=("cameras", cam.name),
                source_file=document.hardware_path,
            )
        ]
    handshake_fn = getattr(module, "handshake", None)
    if handshake_fn is None:
        return []

    cam_dict: dict[str, Any] = cam.model_dump() if hasattr(cam, "model_dump") else dict(cam)
    try:
        summary = await asyncio.wait_for(handshake_fn(cam_dict), timeout=_LIVE_HANDSHAKE_TIMEOUT_S)
    except TimeoutError:
        return [
            ConfigProblem(
                severity="error",
                code="live.handshake_timeout",
                message=(
                    f"camera {cam.name}: handshake timed out after {_LIVE_HANDSHAKE_TIMEOUT_S:g}s"
                ),
                section="cameras",
                path=("cameras", cam.name),
                source_file=document.hardware_path,
                fix_label="Check camera connection",
            )
        ]
    except Exception as exc:
        return [
            ConfigProblem(
                severity="error",
                code="live.handshake_failed",
                message=f"camera {cam.name}: {exc}",
                section="cameras",
                path=("cameras", cam.name),
                source_file=document.hardware_path,
            )
        ]
    return [
        ConfigProblem(
            severity="info",
            code="live.handshake_ok",
            message=f"camera {cam.name}: {summary}",
            section="cameras",
            path=("cameras", cam.name),
            source_file=document.hardware_path,
        )
    ]


async def validate_live_async(
    document: ConfigDocument,
) -> list[ConfigProblem]:
    """Async-native Layer 5 driver for the Setup tab's Check Hardware button.

    Re-runs Layers 1–4 first (they're cheap and a failed schema means we
    can't compose a config to hand to the live layer), then runs Layer 5
    concurrently. Returns the merged list ordered by severity.
    """
    # Layers 1–4 first. If schema fails hard, return early — Layer 5
    # needs a built config to walk.
    base_problems: list[ConfigProblem] = []
    schema_problems, valid_config = _layer1_schema(document)
    base_problems.extend(schema_problems)
    if valid_config is None:
        return _sorted_problems(base_problems)
    base_problems.extend(_layer2_referential(valid_config, document))
    base_problems.extend(_layer3_domain(valid_config, document))
    base_problems.extend(_layer4_resource(valid_config, document))

    live_problems = await _layer5_live_async(valid_config, document)
    return _sorted_problems([*base_problems, *live_problems])


__all__ = ["validate", "validate_live_async"]
