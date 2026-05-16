""":class:`AdapterDescriptor` registry — one source of truth ().

Each adapter contributes one curated record. The Setup editor, the
runtime, and the CLI all read from :data:`ADAPTERS` so that *adding an
adapter* means defining one Pydantic params model + one descriptor —
no duplicate lookup tables.

Built-in descriptors live next to each adapter module (the module
exports a module-level ``DESCRIPTOR``); the module's bottom-of-file
``register(DESCRIPTOR)`` populates this registry at import time.
Plugin adapters discovered via the ``capa.adapters`` /
``capa.cameras`` entry-point groups register the same way through
:func:`load_plugin_descriptors`.

The plan's :class:`ChannelTemplate` ships a few canonical pre-canned
channels per adapter family () so the Setup tab's "Add
channel" menu covers the 90% case without operators typing 40 fields.
"""

from __future__ import annotations

import importlib
import importlib.metadata as importlib_metadata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Channel templates ().
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChannelTemplate:
    """Pre-canned channel definition surfaced in the "Add Channel" menu.

    The Setup editor reads this and produces a draft :class:`ChannelSpec`
    populated for the operator's currently-selected device. ``capa_group``
    is the metadata key that ties the channel to a CAPA profile slot
    (``"heater_pv"``, ``"sample_temperature"``, etc.); ``plot_group`` is
    the UI-side bucket the Numerics dock uses to group plots.

    ``source_factory`` builds the :class:`SourceBinding` variant for the
    template given a device name. Templates that depend on
    discriminated-union variants (Watlow parameter, Alicat frame field,
    etc.) construct them here so the editor doesn't have to know binding
    internals.
    """

    id: str
    label: str
    kind: str
    """Channel kind discriminator: ``"process_var"``, ``"setpoint"``,
    ``"tc"``, ``"mfc_flow"``, ``"mass"``, etc. See
    :class:`capa.channels.spec.ChannelKind`."""
    source_factory: Callable[[str], dict[str, Any]]
    """Given a device name, return the raw ``source`` mapping that
    :class:`SourceBinding` will discriminate on."""
    default_unit: str
    default_derived_unit: str | None = None
    default_calibration: dict[str, Any] | None = None
    capa_group: str | None = None
    plot_group: str | None = None
    metadata_defaults: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# AdapterDescriptor.
# ---------------------------------------------------------------------------


AdapterFamily = Literal[
    "sim",
    "watlow",
    "alicat",
    "sartorius",
    "nidaq",
    "camera_visible",
    "camera_ir",
    "plugin",
]


@dataclass(frozen=True)
class AdapterDescriptor:
    """One curated record per adapter — params model + UI hints + factory.

    Lives next to the adapter module so the descriptor and
    the adapter cannot drift. The runtime resolves
    :class:`DeviceConfig.adapter` (a module-path string) to an
    :class:`AdapterDescriptor` via :func:`get_descriptor` / :data:`ADAPTERS`.

    The :attr:`adapter_factory` is the callable the runtime uses to
    instantiate the adapter; it must remain **passive** (no I/O on
    ``__init__``) so the Setup editor's Layer-4 resource validation
    can construct adapters without opening hardware.
    """

    id: str
    """Stable adapter id (the same string used as
    :class:`DeviceConfig.adapter`). Typically the module path, e.g.
    ``"capa.devices.watlow"``."""

    label: str
    """Operator-facing label for the "Add device" menu and adapter combobox."""

    family: AdapterFamily
    """Coarse family for UI grouping, colour cues, and discovery routing."""

    adapter_factory: Callable[..., Any]
    """Callable that returns the underlying adapter or camera instance.

    For device adapters: the runtime calls
    ``adapter_factory(name=..., **params)`` (or its ``from_params``
    classmethod when present) and uses the result directly.

    For cameras (``family`` in ``("camera_visible", "camera_ir")``): the
    runtime passes the factory to
    :func:`capa.runtime.camera_adapter.make_camera_adapter`, which
    invokes ``adapter_factory(spec=..., clock=..., **params)`` (or
    ``from_params``) and wraps the result in a
    :class:`CameraDeviceAdapter`."""

    params_model: type[BaseModel] | None = None
    """The adapter's typed parameter model (e.g. ``WatlowAdapterParams``).
    The form generator builds an auto-form against this. ``None`` for
    adapters whose ``params`` is still a free-form ``dict``."""

    supported_binding_sources: tuple[str, ...] = ()
    """Which :class:`SourceBinding` discriminator values are valid for
    channels reading from this adapter (``("watlow_parameter",)`` for
    Watlow). Used by the channel detail form to filter the binding-type
    combobox and by Layer-2 validation."""

    default_params: dict[str, Any] = field(default_factory=dict)
    """Default parameter dict for "Add device" — pre-fills the form so
    operators don't start from a fully-blank pane."""

    channel_templates: tuple[ChannelTemplate, ...] = ()
    """Pre-canned channels surfaced by "Add channel from template…"."""

    discoverable: bool = False
    """Whether the adapter exposes a ``discover()`` coroutine."""

    discoverable_reason: str | None = None
    """When :attr:`discoverable` is ``False``, an operator-facing
    one-line explanation surfaced by the DiscoveryDialog as a tooltip on
    a disabled row (item 3). ``None`` for adapters that simply
    have no business in the dialog (every sim adapter); set when the
    adapter *should* be scannable but isn't yet — e.g. Watlow is gated
    on watlowlib shipping ``find_devices()``."""

    handshake_available: bool = False
    """Whether the adapter exposes a ``handshake(params)`` coroutine that
    can verify a config without opening the worker pool."""

    capabilities: frozenset[Any] = field(default_factory=frozenset)
    """Static :class:`Capability` flags the adapter advertises.

    Some adapters compute capabilities at construction time (Alicat
    advertises additional flags when the device is a controller). The
    static set here is the *upper bound* that the Setup editor uses to
    check procedure-required capabilities; the runtime-actual set is
    still read from the constructed adapter instance."""


# ---------------------------------------------------------------------------
# Registry storage + dispatch.
# ---------------------------------------------------------------------------


ADAPTERS: dict[str, AdapterDescriptor] = {}
"""Adapter id → descriptor. Populated at import time by adapter modules
and at app start via :func:`load_plugin_descriptors`."""


def register(descriptor: AdapterDescriptor) -> None:
    """Add a descriptor to the registry.

    Idempotent: re-registering the same ``id`` overwrites, which keeps
    test isolation manageable. Real conflicts (two different adapters
    claiming the same id) are an authoring bug, not a runtime concern.
    """
    ADAPTERS[descriptor.id] = descriptor


def get_descriptor(adapter_id: str) -> AdapterDescriptor | None:
    """Look up a descriptor; ``None`` if none registered."""
    return ADAPTERS.get(adapter_id)


def require_descriptor(adapter_id: str) -> AdapterDescriptor:
    """Look up a descriptor or raise :class:`KeyError`.

    Used by call sites that must have a descriptor (the runtime adapter
    builder, the Setup editor's resolve step). Adapter modules register
    at import time, so a missing descriptor here is an authoring bug.

    When ``adapter_id`` is a dotted module path that hasn't been
    imported yet (built-in adapters that ``ensure_adapters_loaded``
    didn't cover; out-of-tree test fixtures whose path matches), the
    module is imported first to give its ``register(DESCRIPTOR)`` call
    a chance to populate the registry.
    """
    descriptor = ADAPTERS.get(adapter_id)
    if descriptor is not None:
        return descriptor
    if "." in adapter_id:
        try:
            importlib.import_module(adapter_id)
        except ImportError:
            pass
        else:
            descriptor = ADAPTERS.get(adapter_id)
            if descriptor is not None:
                return descriptor
    raise KeyError(
        f"no AdapterDescriptor registered for {adapter_id!r}; "
        "the adapter module must export a module-level DESCRIPTOR "
        "and call register(DESCRIPTOR) at import time"
    )


def all_for_family(family: AdapterFamily) -> tuple[AdapterDescriptor, ...]:
    return tuple(d for d in ADAPTERS.values() if d.family == family)


_LOADED_DESCRIPTOR_SETS: set[str] = set()


def load_plugin_descriptors() -> None:
    """Read ``capa.adapters`` / ``capa.cameras`` entry-point groups.

    Each entry point's target must be an
    :class:`AdapterDescriptor` instance (the plugin module exports
    ``DESCRIPTOR``) or a callable that returns one. Failures are logged
    but never raise: a broken plugin must not prevent the app from
    starting.

    Idempotent: safe to call multiple times.
    """
    if "plugins" in _LOADED_DESCRIPTOR_SETS:
        return
    _LOADED_DESCRIPTOR_SETS.add("plugins")
    for group in ("capa.adapters", "capa.cameras"):
        try:
            entry_points = importlib_metadata.entry_points(group=group)
        except Exception:  # pragma: no cover - importlib_metadata edge case
            continue
        for ep in entry_points:
            try:
                target = ep.load()
            except Exception:
                # risk: a broken plugin degrades to "not visible"
                # rather than crashing capa at startup. The Setup editor
                # will simply not offer the missing adapter.
                continue
            if isinstance(target, AdapterDescriptor):
                register(target)
            elif callable(target):
                try:
                    descriptor = target()
                except Exception:
                    continue
                if isinstance(descriptor, AdapterDescriptor):
                    register(descriptor)


# ---------------------------------------------------------------------------
# Built-in descriptor import — eager-loads adapter modules so their
# module-level ``register(DESCRIPTOR)`` calls populate the registry.
#
# Listed here rather than relying on import-side-effects elsewhere so
# the registry has a single, predictable state after first import.
# ---------------------------------------------------------------------------


_BUILTIN_ADAPTER_MODULES = (
    "capa.devices.watlow",
    "capa.devices.alicat",
    "capa.devices.sartorius",
    "capa.devices.nidaq",
    "capa.devices.sim.watlow_sim",
    "capa.devices.sim.alicat_sim",
    "capa.devices.sim.sartorius_sim",
    "capa.devices.sim.nidaq_polled_sim",
    "capa.devices.sim.nidaq_block_sim",
    "capa.devices.sim.flir_ir_sim",
    "capa.devices.camera.webcam",
)


def _import_builtins() -> None:
    """Import each built-in adapter module so its ``DESCRIPTOR`` registers.

    Failures are silent (e.g. nidaq library missing on a sim-only box) —
    only the adapters whose import succeeds contribute to the registry.
    """
    if "builtins" in _LOADED_DESCRIPTOR_SETS:
        return
    _LOADED_DESCRIPTOR_SETS.add("builtins")
    for module_path in _BUILTIN_ADAPTER_MODULES:
        try:
            importlib.import_module(module_path)
        except Exception:
            # Missing optional dep (e.g. no NI driver) shouldn't crash
            # registry init — only the adapters that *do* import contribute.
            continue


def ensure_adapters_loaded() -> None:
    """Eagerly load every built-in adapter and any plugin entry points.

    The runtime resolves adapter ids lazily (one descriptor lookup per
    declared device), but the Setup editor needs the full catalogue to
    populate the "Add device" / "Add camera" menus. Surfaces are
    expected to call this once on construction; it's idempotent and
    cheap on subsequent invocations.
    """
    _import_builtins()
    load_plugin_descriptors()


__all__ = [
    "ADAPTERS",
    "AdapterDescriptor",
    "AdapterFamily",
    "ChannelTemplate",
    "all_for_family",
    "ensure_adapters_loaded",
    "get_descriptor",
    "load_plugin_descriptors",
    "register",
    "require_descriptor",
]
